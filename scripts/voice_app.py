#!/usr/bin/env python3
"""A local-only text-to-speech panel for this voice profile."""

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from voice_paths import DEFAULT_DATA_DIR, resolve_data_dir
from synthesize import load_profile, voice_fingerprint
from voice_catalog import load_catalog, valid_voice_id, valid_voice_name


ROOT = Path(__file__).resolve().parents[1]
# Environment resolution belongs in configure(), after CLI arguments are parsed.
DATA = DEFAULT_DATA_DIR.expanduser().resolve()
OUTPUTS = DATA / "outputs"
PYTHON = DATA / ".venv" / "bin" / "python"
SYNTHESIZER = ROOT / "scripts" / "synthesize.py"
PANEL = ROOT / "assets" / "panel.html"
HOST, PORT = "127.0.0.1", 8766
SERVICE = "personal-voice-local-v1"
PROFILE_ID = hashlib.sha256(str(DATA).encode()).hexdigest()[:12]
TOKEN = secrets.token_urlsafe(32)
MAX_CHARS = 2000
MAX_RECORD_BYTES = 16000
DEMO_NAME = "demo.wav"
PROGRESS_MESSAGES = {
    "loading_model": "正在加载本地声音模型。",
    "generating": "正在生成 AI 配音。",
    "checking": "正在检查音频并导出。",
}
CLIENT_DISCONNECT_ERRORS = (ConnectionError, TimeoutError)
PUBLIC_JOB_FIELDS = {
    "job_id", "status", "message", "created_at", "started_at", "finished_at",
    "output_url", "filename", "error", "text", "elapsed_seconds", "stage",
    "part", "total", "duration_seconds", "voice_id", "voice_name",
}
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}
WAV_NAME = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fff]+\.wav\Z")
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
LOCK = threading.Lock()
JOBS = {}
CURRENT_JOB = None


def output_url(filename):
    return "/outputs/" + quote(filename, safe="")


def safe_output(filename):
    if not WAV_NAME.fullmatch(filename) or OUTPUTS.is_symlink():
        return None
    candidate = OUTPUTS / filename
    if candidate.is_symlink() or candidate.resolve().parent != OUTPUTS.resolve():
        return None
    return candidate


def open_output_directory():
    return os.open(OUTPUTS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def open_regular_output(directory_fd, filename):
    # NONBLOCK makes special files such as FIFOs safe to reject after opening.
    descriptor = os.open(
        filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Not a regular output file")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def open_output_audio(filename):
    """Anchor reads to the real output directory, including during a rename."""
    if not WAV_NAME.fullmatch(filename):
        raise FileNotFoundError("Invalid output filename")
    directory_fd = open_output_directory()
    try:
        audio_fd = open_regular_output(directory_fd, filename)
    finally:
        os.close(directory_fd)
    return os.fdopen(audio_fd, "rb")


def reject_duplicate_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def reject_json_constant(_value):
    raise ValueError("Invalid JSON number")


def strict_json(data):
    return json.loads(
        data, object_pairs_hook=reject_duplicate_fields, parse_constant=reject_json_constant
    )


def valid_text(text):
    if not isinstance(text, str) or not 0 < len(text) <= MAX_CHARS or not text.strip():
        return False
    try:
        text.encode("utf-8")
    except UnicodeError:
        return False
    return True


def finite_nonnegative(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def history_item(directory_fd, filename):
    """Read only validated public fields from a WAV/JSON pair under one directory FD."""
    if not WAV_NAME.fullmatch(filename):
        return None
    try:
        with os.fdopen(open_regular_output(directory_fd, filename), "rb") as audio:
            audio_info = os.fstat(audio.fileno())
            header = audio.read(12)
            if audio_info.st_size <= 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
        metadata_name = filename[:-4] + ".json"
        with os.fdopen(open_regular_output(directory_fd, metadata_name), "rb") as metadata:
            if not 0 < os.fstat(metadata.fileno()).st_size <= MAX_RECORD_BYTES:
                return None
            raw = metadata.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return None
        report = strict_json(raw.decode("utf-8"))
        if not isinstance(report, dict) or report.get("status") != "generated" or report.get("synthetic_audio") is not True:
            return None
        text, duration = report.get("text"), report.get("duration_seconds")
        if not valid_text(text) or not finite_nonnegative(duration) or not math.isfinite(audio_info.st_mtime):
            return None
        item = {
            "filename": filename,
            "title": text[:22],
            "text": text,
            "duration_seconds": duration,
            "created_at": audio_info.st_mtime,
            "output_url": output_url(filename),
            "is_demo": filename == DEMO_NAME,
        }
        if valid_voice_id(report.get("voice_id")) and valid_voice_name(report.get("voice_name")):
            item.update(voice_id=report["voice_id"], voice_name=report["voice_name"])
        return item
    except (OSError, ValueError, UnicodeError, OverflowError, RecursionError):
        return None


def read_history():
    try:
        directory_fd = open_output_directory()
    except OSError:
        return []
    try:
        items = []
        for filename in os.listdir(directory_fd):
            item = history_item(directory_fd, filename)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: (item["created_at"], item["filename"]), reverse=True)
        return items[:50]
    except OSError:
        return []
    finally:
        os.close(directory_fd)


def demo_available():
    try:
        with open_output_audio(DEMO_NAME) as audio:
            return os.fstat(audio.fileno()).st_size > 44
    except OSError:
        return False


def parse_progress(line):
    """Do not trust the subprocess's free-form messages or other JSON fields."""
    if len(line) > MAX_RECORD_BYTES or not line.lstrip().startswith("{"):
        return None
    try:
        progress = strict_json(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(progress, dict):
        return None
    stage = progress.get("stage")
    if not isinstance(stage, str) or stage not in PROGRESS_MESSAGES:
        return None
    part, total = progress.get("part"), progress.get("total")
    if stage == "generating":
        if type(part) is not int or type(total) is not int or not 1 <= part <= total <= MAX_CHARS:
            return None
    else:
        part, total = None, None
    return {"stage": stage, "part": part, "total": total, "message": PROGRESS_MESSAGES[stage]}


def public_job(job):
    result = {key: value for key, value in job.items() if key in PUBLIC_JOB_FIELDS}
    if "text" in result and not valid_text(result["text"]):
        del result["text"]
    began = job.get("_created_monotonic")
    if began is not None:
        end = job.get("_finished_monotonic", time.monotonic())
        result["elapsed_seconds"] = round(max(0.0, end - began), 1)
    return result


def update_job(job_id, **changes):
    with LOCK:
        JOBS[job_id].update(changes)


class VoiceGenerationError(RuntimeError):
    """An intentional error message suitable for display in the local panel."""


def generate(job_id, text_path, filename, voice_id=None, voice_fingerprint=None):
    try:
        update_job(job_id, status="running", stage="starting", message="正在启动本地声音生成。", started_at=time.time())
        target = safe_output(filename)
        if target is None:
            raise VoiceGenerationError("输出目录不可用。")
        if not PYTHON.is_file() or not SYNTHESIZER.is_file():
            raise VoiceGenerationError("声音生成环境尚未准备好，请稍后再试。")
        command = [str(PYTHON), str(SYNTHESIZER), "--data-dir", str(DATA),
                   "--text-file", str(text_path), "--output", str(target)]
        if voice_id is not None:
            command.extend(["--voice", voice_id])
        if voice_fingerprint is not None:
            command.extend(["--expected-voice-fingerprint", voice_fingerprint])
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUNBUFFERED": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        )
        with process.stdout:
            discard_line = False
            while True:
                line = process.stdout.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                complete_line = line.endswith("\n")
                if discard_line or len(line) > MAX_RECORD_BYTES:
                    discard_line = not complete_line
                    continue
                progress = parse_progress(line)
                if progress is not None:
                    update_job(job_id, **progress)
        returncode = process.wait()
        if returncode:
            raise VoiceGenerationError(f"声音生成程序退出，错误代码 {returncode}。请重试。")
        # Only serve a real WAV created inside this job's output folder.
        target = safe_output(filename)
        if target is None:
            raise VoiceGenerationError("生成程序没有输出有效音频，请重试。")
        with open_output_audio(filename) as audio:
            info = os.fstat(audio.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 44:
                raise VoiceGenerationError("生成程序没有输出有效音频，请重试。")
            header = audio.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise VoiceGenerationError("生成的文件不是有效 WAV 音频。")
        directory_fd = open_output_directory()
        try:
            record = history_item(directory_fd, filename)
        finally:
            os.close(directory_fd)
        result_details = {"duration_seconds": record["duration_seconds"]} if record else {}
        if record and "voice_id" in record:
            result_details.update(voice_id=record["voice_id"], voice_name=record["voice_name"])
        update_job(
            job_id,
            status="done",
            stage="complete",
            message="生成完成。这是 AI 合成音频。",
            output_url=output_url(filename),
            filename=filename,
            finished_at=time.time(),
            _finished_monotonic=time.monotonic(),
            **result_details,
        )
    except Exception as error:
        message = str(error) if isinstance(error, VoiceGenerationError) else "本地生成暂时不可用，请稍后重试。"
        update_job(job_id, status="error", stage="error", message="生成失败。", error=message, finished_at=time.time(), _finished_monotonic=time.monotonic())
    finally:
        try:
            text_path.unlink(missing_ok=True)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def handle(self):
        try:
            super().handle()
        except CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True

    def log_message(self, *_args):
        # Do not log user text or request data.
        pass

    def common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; media-src 'self'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )

    def respond(self, code, data, content_type="application/json; charset=utf-8", head=False, extra_headers=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def host_allowed(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self.close_connection = True
            self.respond(403, {"error": "只允许从本机配音面板访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:
            self.close_connection = True
            self.respond(403, {"error": "不允许其他网页访问本服务。"})
            return False
        return True

    def do_HEAD(self):
        self.read_request(head=True)

    def do_GET(self):
        self.read_request()

    def read_request(self, head=False):
        if not self.host_allowed():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path in {"/", "/panel.html", "/配音面板.html"}:
            try:
                self.respond(200, PANEL.read_bytes(), "text/html; charset=utf-8", head=head)
            except OSError:
                self.respond(500, {"error": "配音面板文件不可用。"}, head=head)
        elif path == "/favicon.ico":
            self.respond(204, b"", head=head)
        elif path == "/api/status":
            with LOCK:
                job = public_job(JOBS[CURRENT_JOB]) if CURRENT_JOB else None
            ready, setup_message, _ = configuration_status()
            default_voice, voices = voice_options()
            self.respond(200, {
                "ready": ready,
                "setup_required": not ready,
                "setup_message": setup_message,
                "service": SERVICE,
                "profile_id": PROFILE_ID,
                "csrf_token": TOKEN,
                "max_chars": MAX_CHARS,
                "ui_version": "2.1",
                "voices": voices,
                "default_voice": default_voice,
                "busy": bool(job and job["status"] in {"queued", "running"}),
                "current_job": job,
                "demo_url": output_url(DEMO_NAME) if demo_available() else None,
            }, head=head)
        elif path == "/api/history":
            self.respond(200, {"items": read_history()}, head=head)
        elif path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            with LOCK:
                job = public_job(JOBS[job_id]) if JOB_ID.fullmatch(job_id) and job_id in JOBS else None
            self.respond(200 if job else 404, job or {"error": "没有找到这次生成任务。"}, head=head)
        elif path.startswith("/outputs/"):
            self.serve_audio(path[len("/outputs/"):], parsed.query, head)
        else:
            self.respond(404, {"error": "没有找到这个页面。"}, head=head)

    def serve_audio(self, filename, query, head):
        candidate = safe_output(filename)
        if candidate is None:
            self.respond(404, {"error": "没有找到这段音频。"}, head=head)
            return
        try:
            audio = open_output_audio(filename)
        except OSError:
            self.respond(404, {"error": "没有找到这段音频。"}, head=head)
            return
        with audio:
            info = os.fstat(audio.fileno())
            if not stat.S_ISREG(info.st_mode):
                self.respond(404, {"error": "没有找到这段音频。"}, head=head)
                return
            size, start, end, code = info.st_size, 0, info.st_size - 1, 200
            request_range = self.headers.get("Range")
            if request_range:
                match = re.fullmatch(r"bytes=([0-9]{0,19})-([0-9]{0,19})", request_range)
                if not match or not any(match.groups()) or not size:
                    self.respond(416, {"error": "音频范围无效。"}, head=head, extra_headers={"Content-Range": f"bytes */{size}"})
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix = int(last)
                    start = max(0, size - suffix)
                if start > end or start >= size:
                    self.respond(416, {"error": "音频范围无效。"}, head=head, extra_headers={"Content-Range": f"bytes */{size}"})
                    return
                code = 206
            self.send_response(code)
            self.common_headers()
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if parse_qs(query).get("download") == ["1"]:
                self.send_header("Content-Disposition", f"attachment; filename=voice.wav; filename*=UTF-8''{quote(filename, safe='')}")
            self.end_headers()
            if not head:
                audio.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = audio.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)

    def do_POST(self):
        global CURRENT_JOB
        if not self.host_allowed():
            return
        supplied_token = self.headers.get("X-Voice-Token", "").encode("utf-8")
        if self.headers.get("Origin") not in ALLOWED_ORIGINS or not secrets.compare_digest(supplied_token, TOKEN.encode("ascii")):
            self.close_connection = True
            self.respond(403, {"error": "请刷新本机配音面板后再试。"})
            return
        if urlsplit(self.path).path != "/api/generate":
            self.close_connection = True
            self.respond(404, {"error": "没有找到这个操作。"})
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("请求格式不正确。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16000 or self.headers.get("Transfer-Encoding"):
                raise ValueError("输入太长或请求格式不正确。")
            payload = strict_json(self.rfile.read(length).decode("utf-8"))
            if (not isinstance(payload, dict) or "text" not in payload
                    or set(payload) - {"text", "voice_id"} or not isinstance(payload["text"], str)):
                raise ValueError("请输入要配音的文字。")
            requested_voice = payload.get("voice_id")
            if "voice_id" in payload and not valid_voice_id(requested_voice):
                raise ValueError("音色编号无效。")
            text = payload["text"].strip()
            if not valid_text(text):
                raise ValueError("请输入要配音的文字。")
        except (ValueError, UnicodeError, RecursionError):
            self.close_connection = True
            self.respond(400, {"error": "请输入 1–2000 字的文字，使用正确的请求格式。"})
            return
        ready, setup_message, setup_code = configuration_status(requested_voice)
        if not ready:
            self.respond(setup_code, {"error": setup_message})
            return
        try:
            profile, model, reference = load_profile(DATA, requested_voice)
            selected_voice = profile["voice_id"]
            fingerprint = voice_fingerprint(profile, model, reference)
        except (OSError, ValueError):
            self.respond(409, {"error": "所选音色不可用，请刷新后重新选择。"})
            return
        rejected = None
        with LOCK:
            if CURRENT_JOB and JOBS[CURRENT_JOB]["status"] in {"queued", "running"}:
                rejected = (409, {"error": "上一段配音还在生成，请等待完成。", "job_id": CURRENT_JOB})
            elif OUTPUTS.is_symlink():
                rejected = (500, {"error": "输出目录不可用。"})
            else:
                try:
                    OUTPUTS.mkdir(mode=0o700, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".local-voice-", suffix=".txt", dir=DATA, delete=False) as temporary:
                        temporary.write(text)
                        text_path = Path(temporary.name)
                except OSError:
                    rejected = (500, {"error": "暂时无法保存配音任务，请重试。"})
                if rejected is None:
                    job_id = uuid.uuid4().hex
                    created = datetime.now().astimezone()
                    filename = created.strftime("%Y%m%d-%H%M%S-") + job_id[:10] + ".wav"
                    JOBS[job_id] = {
                        "job_id": job_id,
                        "status": "queued",
                        "stage": "queued",
                        "part": None,
                        "total": None,
                        "message": "配音任务已开始。",
                        "text": text,
                        "voice_id": selected_voice,
                        "voice_name": profile["voice_name"],
                        "created_at": created.timestamp(),
                        "_created_monotonic": time.monotonic(),
                    }
                    CURRENT_JOB = job_id
                    while len(JOBS) > 32:
                        oldest = next(iter(JOBS))
                        if oldest == CURRENT_JOB:
                            break
                        del JOBS[oldest]
        if rejected is not None:
            self.respond(*rejected)
            return
        threading.Thread(target=generate, args=(job_id, text_path, filename, selected_voice, fingerprint), daemon=True).start()
        self.respond(202, {"job_id": job_id, "status": "queued"})


class LocalVoiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], CLIENT_DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


def configure(data_dir=None, port=8766, host="127.0.0.1"):
    """Configure paths without creating directories or loading the model."""
    global DATA, OUTPUTS, PYTHON, HOST, PORT, PROFILE_ID, ALLOWED_HOSTS, ALLOWED_ORIGINS
    if host != "127.0.0.1":
        raise ValueError("Only the IPv4 loopback host 127.0.0.1 is allowed.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be an integer between 1 and 65535.")
    DATA = resolve_data_dir(data_dir)
    OUTPUTS = DATA / "outputs"
    PYTHON = DATA / ".venv" / "bin" / "python"
    HOST, PORT = host, port
    PROFILE_ID = hashlib.sha256(str(DATA).encode()).hexdigest()[:12]
    ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    ALLOWED_ORIGINS = {f"http://{host}" for host in ALLOWED_HOSTS}


def configuration_status(voice_id=None):
    """Expose a friendly setup state while keeping the unconfigured panel readable."""
    try:
        load_profile(DATA, voice_id)
    except FileNotFoundError as error:
        return False, str(error), 409
    except ValueError as error:
        return False, str(error), 400
    except OSError:
        return False, "无法读取本地声音配置，请先完成设置。", 409
    if not PYTHON.is_file():
        return False, "本地声音运行环境尚未准备好，请先完成设置。", 409
    return True, "", 200


def voice_options():
    """Return only display fields; each voice has independent readiness."""
    try:
        catalog = load_catalog(DATA)
    except (OSError, ValueError):
        return None, []
    options = []
    for entry in catalog["voices"]:
        ready, message, _ = configuration_status(entry["id"])
        options.append({"id": entry["id"], "name": entry["name"],
                        "ready": ready, "setup_message": message})
    return catalog["default_voice"], options


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local AI voice panel on loopback only.")
    parser.add_argument("--data-dir", help="Private data directory (LOCAL_VOICE_DATA_DIR or macOS Application Support by default).")
    parser.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    configure(args.data_dir, args.port, args.host)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = LocalVoiceHTTPServer((HOST, PORT), Handler)
    print(f"扎提skill · 本地 AI 配音面板：http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
