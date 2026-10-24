#!/usr/bin/env python3
"""Backend contract checks using temporary byte fixtures and a mocked synthesizer.

No server is started, no real output is edited, and no model or audio inference runs.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


JOB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB / "scripts"))
import synthesize
import voice_paths
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("local_voice_backend_test", JOB / "scripts" / "voice_app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
# A header-sized byte fixture, with no speech samples or audio generation.
WAV_FIXTURE = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 40


class Probe(app.Handler):
    def respond(self, code, data, content_type="application/json; charset=utf-8", head=False, extra_headers=None):
        # A client response must never hold the shared generation lock.
        if not app.LOCK.acquire(blocking=False):
            raise AssertionError("Socket response held the generation lock")
        app.LOCK.release()
        if isinstance(data, dict):
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.response = (code, data, head, extra_headers or {})

    def send_response(self, code, message=None):
        self.audio_code = code

    def send_header(self, name, value):
        self.audio_headers[name] = value

    def end_headers(self):
        pass


class FakeProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout = io.StringIO(stdout)
        self.returncode = returncode

    def wait(self):
        return self.returncode


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-backend-qa-")
        self.base = Path(self.temporary.name)
        self.outputs = self.base / "outputs"
        self.outputs.mkdir()
        (self.base / "models").mkdir()
        (self.base / "ref_voice.wav").write_bytes(WAV_FIXTURE)
        (self.base / "voice_profile.json").write_text(json.dumps({
            "name": "Test voice", "method": "reference", "model_repo": "test/model",
            "reference_text": "测试文稿", "reference_audio": "ref_voice.wav",
            "model_path": "models", "consent_confirmed": True,
        }), encoding="utf-8")
        self.patches = [
            mock.patch.object(app, "OUTPUTS", self.outputs),
            mock.patch.object(app, "DATA", self.base),
            mock.patch.object(app, "configuration_status", return_value=(True, "", 200)),
            mock.patch.object(app, "JOBS", {}),
            mock.patch.object(app, "CURRENT_JOB", None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def pair(self, filename="历史.wav", report=None, mtime=100):
        audio = app.OUTPUTS / filename
        audio.write_bytes(WAV_FIXTURE)
        data = {
            "status": "generated", "synthetic_audio": True,
            "text": "这是一个用于测试历史接口的本地配音文稿。", "duration_seconds": 2.5,
            "output": "/private/should-not-be-returned.wav",
            "reference": "/private/reference.wav", "model_path": "/private/model",
        }
        if report:
            data.update(report)
        audio.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
        os.utime(audio, (mtime, mtime))
        return audio

    def probe(self, path="/api/status", payload=None, headers=None):
        handler = object.__new__(Probe)
        handler.path = path
        handler.headers = {"Host": f"127.0.0.1:{app.PORT}", **(headers or {})}
        handler.close_connection = False
        handler.audio_headers = {}
        handler.wfile = io.BytesIO()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            handler.headers.update({
                "Origin": f"http://127.0.0.1:{app.PORT}",
                "X-Voice-Token": app.TOKEN,
                "Content-Type": "application/json", "Content-Length": str(len(body)),
            })
            handler.headers.update(headers or {})
            handler.rfile = io.BytesIO(body)
        return handler

    def test_history_public_schema_and_demo(self):
        self.pair(app.DEMO_NAME, {"text": "这" * 2000}, mtime=200)
        self.pair("较早.wav", mtime=100)
        handler = self.probe("/api/history")
        handler.do_GET()
        code, data, _, _ = handler.response
        self.assertEqual(code, 200)
        self.assertEqual(len(data["items"]), 2)
        item = data["items"][0]
        self.assertEqual(set(item), {
            "filename", "title", "text", "duration_seconds", "created_at", "output_url", "is_demo",
        })
        self.assertEqual(item["created_at"], 200)
        self.assertEqual(item["title"], "这" * 22)
        self.assertEqual(len(item["text"]), 2000)
        self.assertTrue(item["is_demo"])
        self.assertFalse(data["items"][1]["is_demo"])
        self.assertNotIn("/private", json.dumps(data))
        self.assertEqual(item["output_url"], app.output_url(app.DEMO_NAME))

    def test_history_bad_json_size_and_unpaired_records(self):
        self.pair("好.wav")
        bad = self.pair("坏.wav")
        bad.with_suffix(".json").write_bytes(b"{broken")
        oversized = self.pair("超大.wav", {"unused": "x" * 16000})
        self.assertGreater(oversized.with_suffix(".json").stat().st_size, app.MAX_RECORD_BYTES)
        self.pair("无JSON.wav").with_suffix(".json").unlink()
        (self.outputs / "无WAV.json").write_text("{}", encoding="utf-8")
        self.pair("非法.名称.wav")
        self.pair("重复.wav").with_suffix(".json").write_text(
            '{"status":"generated","synthetic_audio":true,"text":"正常","text":"伪造","duration_seconds":1}',
            encoding="utf-8",
        )
        self.pair("嵌套.wav").with_suffix(".json").write_text("[" * 1200 + "]" * 1200, encoding="utf-8")
        self.assertEqual([item["filename"] for item in app.read_history()], ["好.wav"])

    def test_history_invalid_fields_are_skipped(self):
        bad_reports = [
            {"text": " "}, {"text": 42}, {"text": "字" * 2001}, {"text": "\ud800"},
            {"duration_seconds": -1}, {"duration_seconds": True}, {"duration_seconds": "3"},
            {"duration_seconds": float("nan")}, {"duration_seconds": float("inf")},
            {"duration_seconds": 10 ** 500}, {"status": "running"}, {"synthetic_audio": False},
        ]
        for index, report in enumerate(bad_reports):
            self.pair(f"bad-{index}.wav", report)
        self.pair("零秒.wav", {"duration_seconds": 0})
        self.assertEqual([item["filename"] for item in app.read_history()], ["零秒.wav"])

    def test_history_sorting_and_fifty_item_limit(self):
        for index in range(55):
            self.pair(f"history-{index}.wav", mtime=index + 10)
        items = app.read_history()
        self.assertEqual(len(items), 50)
        self.assertEqual([item["created_at"] for item in items], list(range(64, 14, -1)))

    def test_history_rejects_paths_symlinks_and_special_files(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.wav").write_bytes(WAV_FIXTURE)
        (outside / "secret.json").write_text('{}', encoding="utf-8")
        self.pair("wav-link.wav").unlink()
        (self.outputs / "wav-link.wav").symlink_to(outside / "secret.wav")
        metadata = self.pair("json-link.wav").with_suffix(".json")
        metadata.unlink()
        metadata.symlink_to(outside / "secret.json")
        directory = self.pair("directory.wav").with_suffix(".json")
        directory.unlink()
        directory.mkdir()
        fifo = self.pair("fifo.wav").with_suffix(".json")
        fifo.unlink()
        os.mkfifo(fifo)
        wav_fifo = self.outputs / "wav-fifo.wav"
        os.mkfifo(wav_fifo)
        self.assertEqual(app.read_history(), [])
        descriptor = app.open_output_directory()
        try:
            for name in ("../secret.wav", "a/secret.wav", "x.wav/other.wav", "%2e%2e.wav"):
                self.assertIsNone(app.history_item(descriptor, name))
                with self.assertRaises(FileNotFoundError):
                    app.open_output_audio(name)
        finally:
            os.close(descriptor)
        with mock.patch.object(app, "OUTPUTS", self.base / "linked-output"):
            app.OUTPUTS.symlink_to(outside, target_is_directory=True)
            self.assertEqual(app.read_history(), [])

    def test_directory_fd_survives_output_directory_replacement(self):
        self.pair("原目录.wav")
        outside = self.base / "outside"
        outside.mkdir()
        with mock.patch.object(app, "OUTPUTS", outside):
            self.pair("外部目录.wav")
        original_open = os.open
        def replaced_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == self.outputs and flags & os.O_DIRECTORY:
                self.outputs.rename(self.base / "original-outputs")
                self.outputs.symlink_to(outside, target_is_directory=True)
            return descriptor
        with mock.patch.object(app.os, "open", side_effect=replaced_open):
            self.assertEqual([item["filename"] for item in app.read_history()], ["原目录.wav"])

    def test_progress_filters_messages_and_validates_counters(self):
        clean = app.parse_progress(json.dumps({
            "stage": "generating", "part": 2, "total": 3, "message": "/private/raw log",
            "reference": "/private/reference.wav", "status": "done",
        }))
        self.assertEqual(clean, {
            "stage": "generating", "part": 2, "total": 3, "message": app.PROGRESS_MESSAGES["generating"],
        })
        for stage in ("loading_model", "checking"):
            self.assertEqual(app.parse_progress(json.dumps({"stage": stage}))["stage"], stage)
        bad = ["raw model log", "{bad", "[]", '{"stage": "unknown"}', '{"stage": "complete"}',
               '{"stage": []}', '{"stage": "generating"}',
               '{"stage":"checking","unused":NaN}', "{" + " " * 16000 + "}"]
        for part, total in ((0, 2), (-1, 2), (3, 2), (True, 2), (1, True), (1.5, 2), (1, 2001), ("1", 2)):
            bad.append(json.dumps({"stage": "generating", "part": part, "total": total}))
        for line in bad:
            self.assertIsNone(app.parse_progress(line), line[:100])

    def test_public_job_text_elapsed_and_private_fields(self):
        job = {"job_id": "a" * 32, "status": "running", "text": "合法文稿", "created_at": 1,
               "_created_monotonic": 10, "_finished_monotonic": 12.34,
               "reference": "/private/ref.wav", "_logs": "private", "output": "/private/out.wav"}
        public = app.public_job(job)
        self.assertEqual(public["elapsed_seconds"], 2.3)
        self.assertEqual(public["text"], "合法文稿")
        self.assertNotIn("/private", json.dumps(public))
        job["text"] = "\ud800"
        self.assertNotIn("text", app.public_job(job))

    def test_status_and_existing_job_routes_keep_contract(self):
        self.pair(app.DEMO_NAME)
        job_id = "a" * 32
        app.JOBS[job_id] = {"job_id": job_id, "status": "queued", "message": "等待", "created_at": 1}
        app.CURRENT_JOB = job_id
        handler = self.probe()
        handler.do_GET()
        code, status, _, _ = handler.response
        self.assertEqual(code, 200)
        self.assertEqual(status["service"], "personal-voice-local-v1")
        self.assertEqual(status["profile_id"], app.PROFILE_ID)
        self.assertEqual(status["csrf_token"], app.TOKEN)
        self.assertEqual(status["max_chars"], 2000)
        self.assertEqual(status["ui_version"], "2.1")
        self.assertTrue(status["busy"])
        self.assertEqual(status["current_job"]["job_id"], job_id)
        self.assertEqual(status["demo_url"], app.output_url(app.DEMO_NAME))
        handler = self.probe("/api/jobs/" + job_id)
        handler.do_GET()
        self.assertEqual(handler.response[0], 200)
        handler = self.probe("/api/jobs/not-a-job")
        handler.do_GET()
        self.assertEqual(handler.response[0], 404)

    def test_host_origin_and_token_boundaries(self):
        for path in ("/api/status", "/api/history", "/api/jobs/" + "a" * 32):
            for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
                handler = self.probe(path, headers=headers)
                handler.do_GET()
                self.assertEqual(handler.response[0], 403)
        for headers in ({"X-Voice-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Origin": ""}):
            handler = self.probe("/api/generate", {"text": "文稿"}, headers)
            handler.do_POST()
            self.assertEqual(handler.response[0], 403)
        self.assertEqual(app.JOBS, {})

    def test_generate_rejects_surrogate_and_preserves_busy_contract(self):
        for text in ("\ud800", " ", "字" * 2001):
            handler = self.probe("/api/generate", {"text": text})
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
        job_id = "b" * 32
        app.JOBS[job_id] = {"job_id": job_id, "status": "running"}
        app.CURRENT_JOB = job_id
        handler = self.probe("/api/generate", {"text": "文稿"})
        handler.do_POST()
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.response[1]["job_id"], job_id)

    def test_generate_rejects_deep_or_nonstandard_json(self):
        bodies = [
            '{"text":"文稿","unused":' + "[" * 1100 + "0" + "]" * 1100 + "}",
            '{"text":"文稿","unused":true}',
            '{"text":"文稿","unused":NaN}',
            '{"text":"文稿","text":"另一个文稿"}',
        ]
        for body in bodies:
            handler = self.probe("/api/generate", {"text": "文稿"})
            raw = body.encode("utf-8")
            handler.rfile = io.BytesIO(raw)
            handler.headers["Content-Length"] = str(len(raw))
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)
        self.assertEqual(app.JOBS, {})

    def test_generate_registration_uses_local_time_without_running_model(self):
        fixed = datetime(2026, 10, 8, 18, 19, 20, tzinfo=timezone.utc)
        original_temporary = tempfile.NamedTemporaryFile
        def local_temporary(**kwargs):
            return original_temporary(**kwargs)
        with mock.patch.object(app, "datetime") as dates, mock.patch.object(app.threading, "Thread") as thread, \
                mock.patch.object(app.tempfile, "NamedTemporaryFile", side_effect=local_temporary):
            dates.now.return_value.astimezone.return_value = fixed
            handler = self.probe("/api/generate", {"text": "  文稿  "})
            handler.do_POST()
            self.assertEqual(handler.response[0], 202)
            job_id = handler.response[1]["job_id"]
            self.assertEqual(app.JOBS[job_id]["text"], "文稿")
            self.assertEqual(app.JOBS[job_id]["created_at"], fixed.timestamp())
            self.assertTrue(thread.call_args.kwargs["args"][2].startswith("20261008-181920-"))
            dates.now.assert_called_once_with()
            thread.return_value.start.assert_called_once()

    def test_mocked_generation_updates_only_valid_progress_and_checks_completion(self):
        self.pair("生成.wav")
        text_path = self.base / "input.txt"
        text_path.write_text("文稿", encoding="utf-8")
        job_id = "c" * 32
        app.JOBS[job_id] = {"job_id": job_id, "status": "queued", "text": "文稿", "_created_monotonic": 1}
        lines = '\n'.join([
            "private model stderr", '{"stage":"loading_model","message":"/secret/reference"}',
            '{"stage":"generating","part":1,"total":2}',
            '{"stage":"generating","part":-1,"total":2}',
            "x" * 16001 + '{"stage":"generating","part":2,"total":2}',
            '{"stage":"checking"}', '{"stage":"complete","output":"/secret/output"}', "",
        ])
        updates = []
        update = app.update_job
        def recorded_update(identifier, **changes):
            updates.append(changes)
            update(identifier, **changes)
        with mock.patch.object(app, "PYTHON", Path(__file__)), mock.patch.object(app, "SYNTHESIZER", Path(__file__)), \
                mock.patch.object(app.subprocess, "Popen", return_value=FakeProcess(lines)) as process, \
                mock.patch.object(app, "update_job", side_effect=recorded_update):
            app.generate(job_id, text_path, "生成.wav")
        self.assertEqual([change.get("stage") for change in updates], ["starting", "loading_model", "generating", "checking", "complete"])
        self.assertEqual(app.JOBS[job_id]["status"], "done")
        self.assertEqual(app.JOBS[job_id]["duration_seconds"], 2.5)
        self.assertFalse(text_path.exists())
        self.assertNotIn("/secret", json.dumps(app.public_job(app.JOBS[job_id])))
        process.assert_called_once()

    def test_fake_complete_cannot_override_process_failure(self):
        text_path = self.base / "input.txt"
        text_path.write_text("文稿", encoding="utf-8")
        job_id = "d" * 32
        app.JOBS[job_id] = {"job_id": job_id, "status": "queued"}
        with mock.patch.object(app, "PYTHON", Path(__file__)), mock.patch.object(app, "SYNTHESIZER", Path(__file__)), \
                mock.patch.object(app.subprocess, "Popen", return_value=FakeProcess('{"stage":"complete"}\n/private/log\n', 2)):
            app.generate(job_id, text_path, "不存在.wav")
        self.assertEqual(app.JOBS[job_id]["status"], "error")
        self.assertNotIn("/private", app.JOBS[job_id]["error"])

    def test_generation_environment_failures_finish_and_hide_internal_paths(self):
        for failure_point in ("safe_output", "Popen"):
            text_path = self.base / "input.txt"
            text_path.write_text("文稿", encoding="utf-8")
            job_id = "e" * 32
            app.JOBS[job_id] = {"job_id": job_id, "status": "queued"}
            failing_object = app if failure_point == "safe_output" else app.subprocess
            with mock.patch.object(app, "PYTHON", Path(__file__)), mock.patch.object(app, "SYNTHESIZER", Path(__file__)), \
                    mock.patch.object(failing_object, failure_point, side_effect=PermissionError("/private/runtime/python")):
                app.generate(job_id, text_path, "生成.wav")
            self.assertEqual(app.JOBS[job_id]["status"], "error")
            self.assertFalse(text_path.exists())
            self.assertNotIn("/private", json.dumps(app.public_job(app.JOBS[job_id])))

    def test_audio_head_range_and_path_guards_remain(self):
        self.pair("音频.wav")
        handler = self.probe(headers={"Range": "bytes=0-1"})
        handler.serve_audio("音频.wav", "", False)
        self.assertEqual(handler.audio_code, 206)
        self.assertEqual(handler.wfile.getvalue(), WAV_FIXTURE[:2])
        handler = self.probe(headers={"Range": "bytes=0-1"})
        handler.serve_audio("音频.wav", "", True)
        self.assertEqual(handler.audio_code, 206)
        self.assertEqual(handler.wfile.getvalue(), b"")
        handler = self.probe()
        handler.serve_audio("../secret.wav", "", False)
        self.assertEqual(handler.response[0], 404)

    def test_client_disconnect_does_not_cancel_registered_generation(self):
        handler = self.probe("/api/generate", {"text": "文稿"})
        original_temporary = tempfile.NamedTemporaryFile
        def local_temporary(**kwargs):
            return original_temporary(**kwargs)
        def disconnected_response(*args, **kwargs):
            raise BrokenPipeError("Client closed the socket")
        handler.respond = disconnected_response
        with mock.patch.object(app.threading, "Thread") as thread, \
                mock.patch.object(app.tempfile, "NamedTemporaryFile", side_effect=local_temporary), \
                mock.patch.object(app.BaseHTTPRequestHandler, "handle", side_effect=handler.do_POST):
            handler.handle()
            thread.return_value.start.assert_called_once()
        self.assertTrue(handler.close_connection)
        self.assertIn(app.CURRENT_JOB, app.JOBS)
        self.assertEqual(app.JOBS[app.CURRENT_JOB]["status"], "queued")
        for error in (ConnectionResetError(), TimeoutError()):
            with mock.patch.object(app.BaseHTTPRequestHandler, "finish", side_effect=error):
                handler.finish()
        server = object.__new__(app.LocalVoiceHTTPServer)
        with mock.patch.object(app.sys, "exc_info", return_value=(BrokenPipeError, BrokenPipeError(), None)), \
                mock.patch.object(app.ThreadingHTTPServer, "handle_error") as parent_error:
            server.handle_error(None, ("127.0.0.1", 1))
            parent_error.assert_not_called()



class PortableCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="local-voice-core-")
        self.data = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def profile(self, **changes):
        (self.data / "models" / "qwen3-base").mkdir(parents=True, exist_ok=True)
        (self.data / "ref_voice.wav").write_bytes(WAV_FIXTURE)
        value = {
            "name": "Local authorized voice", "method": "Local reference-conditioned synthesis",
            "reference_text": "Synthetic fixture transcript.", "reference_audio": "ref_voice.wav",
            "model_path": "models/qwen3-base", "model_repo": "example/model",
            "lang_code": "chinese", "consent_confirmed": True,
        }
        value.update(changes)
        (self.data / "voice_profile.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_data_dir_cli_environment_default_and_source_boundaries(self):
        with mock.patch.dict(os.environ, {"LOCAL_VOICE_DATA_DIR": str(self.data)}):
            self.assertEqual(voice_paths.resolve_data_dir(), self.data)
            self.assertEqual(voice_paths.resolve_data_dir(self.data / "other"), self.data / "other")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(voice_paths.resolve_data_dir(), voice_paths.DEFAULT_DATA_DIR.resolve())
        for forbidden in (voice_paths.SKILL_ROOT, voice_paths.SKILL_ROOT / "private"):
            with self.assertRaises(ValueError):
                voice_paths.resolve_data_dir(forbidden)
        linked = self.data / "source-link"
        linked.symlink_to(voice_paths.SKILL_ROOT, target_is_directory=True)
        with self.assertRaises(ValueError):
            voice_paths.resolve_data_dir(linked / "private")

    def test_profile_requires_explicit_consent_and_existing_local_assets(self):
        with self.assertRaises(FileNotFoundError):
            synthesize.load_profile(self.data)
        self.profile(consent_confirmed=False)
        with self.assertRaises(ValueError):
            synthesize.load_profile(self.data)
        self.profile()
        profile, model, reference = synthesize.load_profile(self.data)
        self.assertTrue(profile["consent_confirmed"])
        self.assertEqual(model, self.data / "models" / "qwen3-base")
        self.assertEqual(reference, self.data / "ref_voice.wav")
        reference.unlink()
        with self.assertRaises(FileNotFoundError):
            synthesize.load_profile(self.data)

    def test_profile_rejects_paths_and_links_outside_data(self):
        with tempfile.TemporaryDirectory(prefix="local-voice-outside-") as external:
            outside = Path(external).resolve()
            for field in ("reference_audio", "model_path"):
                self.profile(**{field: str(outside / "asset")})
                with self.assertRaises(ValueError):
                    synthesize.load_profile(self.data)
            self.profile()
            (self.data / "models" / "qwen3-base" / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                synthesize.load_profile(self.data)

    def test_profile_rejects_duplicate_invalid_json_and_symlink(self):
        profile = self.data / "voice_profile.json"
        for content in ('{"consent_confirmed":true,"consent_confirmed":true}', '{"bad":NaN}', "[]" ):
            profile.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                synthesize.load_profile(self.data)
        profile.unlink()
        real = self.data / "real.json"
        real.write_text("{}", encoding="utf-8")
        profile.symlink_to(real)
        with self.assertRaises(ValueError):
            synthesize.load_profile(self.data)

    def test_profile_rejects_special_file_without_blocking(self):
        os.mkfifo(self.data / "voice_profile.json")
        with self.assertRaises(ValueError):
            synthesize.load_profile(self.data)

    def test_output_target_rejects_escape_overwrite_and_symlink(self):
        directory = self.data / "outputs"
        directory.mkdir()
        target = directory / "new.wav"
        self.assertEqual(synthesize.output_target(self.data, target), (directory, target))
        for bad in (self.data / "outside.wav", directory / "bad.mp3", directory / "bad.name.wav"):
            with self.assertRaises(ValueError):
                synthesize.output_target(self.data, bad)
        target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            synthesize.output_target(self.data, target)
        target.unlink()
        target.symlink_to(self.data / "missing.wav")
        with self.assertRaises(FileExistsError):
            synthesize.output_target(self.data, target)

    def test_exclusive_fd_output_and_json_never_overwrite(self):
        directory = self.data / "outputs"
        directory.mkdir()
        def writer(handle, _audio, _rate, **_kwargs):
            handle.write(WAV_FIXTURE)
        report = {"status": "generated", "synthetic_audio": True}
        synthesize.save_outputs(directory, "test.wav", [], 24000, report, writer=writer)
        self.assertEqual((directory / "test.wav").read_bytes(), WAV_FIXTURE)
        self.assertEqual(json.loads((directory / "test.json").read_text()), report)
        self.assertEqual((directory / "test.wav").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            synthesize.save_outputs(directory, "test.wav", [], 24000, report, writer=writer)
        with self.assertRaises(ValueError):
            synthesize.save_outputs(directory, "../escape.wav", [], 24000, report, writer=writer)
        with self.assertRaises(ValueError):
            synthesize.save_outputs(directory, "nan.wav", [], 24000, {"value": float("nan")}, writer=writer)
        self.assertFalse((directory / "nan.json").exists())

    def test_chunks_preserve_content_and_cap_lengths(self):
        text = "a" * 350 + "。Line two!\n" + "b" * 180
        parts = synthesize.chunks(text)
        self.assertTrue(all(0 < len(part) <= 150 for part in parts))
        self.assertEqual("".join(parts), text.replace("\n", ""))
        with self.assertRaises(ValueError):
            synthesize.chunks(text, 0)

    def test_configuration_is_loopback_only_port_aware_and_stably_isolated(self):
        prior_data, prior_port = app.DATA, app.PORT
        try:
            app.configure(self.data, 18766)
            identity = app.PROFILE_ID
            self.assertEqual(app.PYTHON, self.data / ".venv" / "bin" / "python")
            self.assertEqual(app.ALLOWED_ORIGINS, {"http://127.0.0.1:18766", "http://localhost:18766"})
            app.configure(self.data, 18767)
            self.assertEqual(app.PROFILE_ID, identity)
            app.configure(self.data / "other", 18766)
            self.assertNotEqual(app.PROFILE_ID, identity)
            for port in (0, -1, 65536, True):
                with self.assertRaises(ValueError):
                    app.configure(self.data, port)
            with self.assertRaises(ValueError):
                app.configure(self.data, 18766, "0.0.0.0")
        finally:
            app.configure(prior_data, prior_port)

    def test_cli_help_ignores_invalid_data_environment(self):
        environment = {**os.environ, "LOCAL_VOICE_DATA_DIR": str(JOB / "private")}
        result = subprocess.run(
            [sys.executable, "-B", str(JOB / "scripts" / "voice_app.py"), "--help"],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--data-dir", result.stdout)

    def test_explicit_cli_data_overrides_invalid_environment(self):
        selected = self.data / "selected"
        environment = {**os.environ, "LOCAL_VOICE_DATA_DIR": str(JOB / "private")}
        code = (
            "import json, sys; from unittest import mock; import voice_app; "
            "server = mock.patch.object(voice_app, 'LocalVoiceHTTPServer'); "
            "server.start(); voice_app.main(['--data-dir', sys.argv[1]]); "
            "print(json.dumps({'data': str(voice_app.DATA)}))"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(selected)],
            cwd=JOB / "scripts", env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])["data"], str(selected))

    def test_invalid_data_environment_is_rejected_without_cli_override(self):
        environment = {**os.environ, "LOCAL_VOICE_DATA_DIR": str(JOB / "private")}
        result = subprocess.run(
            [sys.executable, "-B", str(JOB / "scripts" / "voice_app.py")],
            env=environment, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the skill source directory", result.stderr)

    def test_unconfigured_profile_is_readable_and_generation_is_blocked(self):
        probe_factory = BackendTests()
        with mock.patch.object(app, "DATA", self.data), mock.patch.object(app, "JOBS", {}), mock.patch.object(app, "CURRENT_JOB", None):
            handler = probe_factory.probe()
            handler.do_GET()
            self.assertEqual(handler.response[0], 200)
            self.assertFalse(handler.response[1]["ready"])
            handler = probe_factory.probe("/api/generate", {"text": "test"})
            with mock.patch.object(app.threading, "Thread") as thread:
                handler.do_POST()
                self.assertEqual(handler.response[0], 409)
                thread.assert_not_called()
            self.profile(consent_confirmed=False)
            handler = probe_factory.probe("/api/generate", {"text": "test"})
            handler.do_POST()
            self.assertEqual(handler.response[0], 400)

    def test_modules_do_not_import_the_inference_runtime(self):
        self.assertNotIn("mlx.core", sys.modules)
        self.assertNotIn("mlx_audio.tts.utils", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
