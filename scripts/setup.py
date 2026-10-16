#!/usr/bin/env python3
"""Check or explicitly install the isolated local voice runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from voice_paths import resolve_data_dir


MODEL_REPO = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
MODEL_REVISION = "1eccf1cb2519b5a4e8a95b5f0544f3303568164f"
SKILL_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS = SKILL_DIR / "requirements.txt"
MODEL_MARKER = ".local_voice_model.json"


def required_versions() -> dict[str, str]:
    versions = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            versions[name] = version
    return versions


def default_python() -> str:
    return shutil.which("python3.12") or sys.executable


def interpreter_info(executable: str) -> dict:
    code = (
        "import json,platform,sys; print(json.dumps({"
        "'version':list(sys.version_info[:3]),'machine':platform.machine(),"
        "'system':platform.system(),'macos':platform.mac_ver()[0]}))"
    )
    result = subprocess.run(
        [executable, "-c", code], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def platform_problems(info: dict) -> list[str]:
    problems = []
    if info.get("system") != "Darwin" or info.get("machine") != "arm64":
        problems.append("The runtime requires Apple Silicon macOS (Darwin arm64).")
    if tuple(info.get("version", [])) < (3, 12):
        problems.append("Python 3.12 or newer is required; choose it with --python.")
    macos = str(info.get("macos", ""))
    if info.get("system") == "Darwin" and (not macos or int(macos.split(".")[0]) < 14):
        problems.append("macOS 14 or newer is required for the pinned Metal runtime.")
    return problems


def model_files_present(model_dir: Path) -> bool:
    return (
        (model_dir / "config.json").is_file()
        and (model_dir / "tokenizer_config.json").is_file()
        and any(model_dir.glob("*.safetensors"))
    )


def installed_versions(python: Path, wanted: dict[str, str]) -> dict:
    if not python.is_file():
        return {}
    code = (
        "import importlib.metadata as m,json,sys; result={}; "
        "exec('for name in json.loads(sys.argv[1]):\\n'"
        "' try: result[name]=m.version(name)\\n'"
        "' except m.PackageNotFoundError: result[name]=None'); "
        "print(json.dumps(result))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code, json.dumps(list(wanted))],
            check=True, capture_output=True, text=True,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return {}


def runtime_environment(data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["HF_HOME"] = str(data_dir / ".cache" / "huggingface")
    env["HF_HUB_CACHE"] = str(data_dir / ".cache" / "huggingface" / "hub")
    env["HUGGINGFACE_HUB_CACHE"] = env["HF_HUB_CACHE"]
    env["PIP_CACHE_DIR"] = str(data_dir / ".cache" / "pip")
    env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    env.pop("PYTHONPATH", None)
    return env


def ensure_private_directory(data_dir: Path, path: Path) -> None:
    if data_dir.resolve() not in path.resolve().parents:
        raise ValueError("Runtime and model directories must remain inside the private data directory.")


def install_runtime(data_dir: Path, python: str) -> None:
    venv_dir = data_dir / ".venv"
    ensure_private_directory(data_dir, venv_dir)
    ensure_private_directory(data_dir, data_dir / ".cache")
    venv_python = venv_dir / "bin" / "python"
    if venv_dir.exists() and not venv_python.is_file():
        raise ValueError("The existing .venv is incomplete; move it aside before retrying.")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = runtime_environment(data_dir)
    if not venv_python.is_file():
        subprocess.run([python, "-m", "venv", str(venv_dir)], check=True, env=env)
    else:
        problems = platform_problems(interpreter_info(str(venv_python)))
        if problems:
            raise ValueError("Existing virtual environment: " + " ".join(problems))
    env["PIP_REQUIRE_VIRTUALENV"] = "true"
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        check=True, env=env,
    )


def download_model(data_dir: Path) -> None:
    ensure_private_directory(data_dir, data_dir / ".venv")
    ensure_private_directory(data_dir, data_dir / ".cache")
    python = data_dir / ".venv" / "bin" / "python"
    if not python.is_file():
        raise ValueError("Install the private runtime with --install before downloading the model.")
    problems = platform_problems(interpreter_info(str(python)))
    if problems:
        raise ValueError("Existing virtual environment: " + " ".join(problems))
    models_dir = data_dir / "models"
    ensure_private_directory(data_dir, models_dir)
    target = models_dir / "qwen3-base"
    expected = {"repo_id": MODEL_REPO, "revision": MODEL_REVISION}
    if target.exists() or target.is_symlink():
        marker = target / MODEL_MARKER
        if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")) == expected and model_files_present(target):
            print("The pinned model is already present; no download needed.", file=sys.stderr)
            return
        raise ValueError("A model directory already exists without a verified matching revision; it will not be overwritten.")
    models_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".qwen3-download-", dir=models_dir) as staging:
        staged_model = Path(staging) / "model"
        code = (
            "from huggingface_hub import snapshot_download; import sys; "
            "snapshot_download(repo_id=sys.argv[1],revision=sys.argv[2],"
            "local_dir=sys.argv[3],token=False)"
        )
        subprocess.run(
            [str(python), "-c", code, MODEL_REPO, MODEL_REVISION, str(staged_model)],
            check=True, env=runtime_environment(data_dir),
        )
        if not model_files_present(staged_model):
            raise ValueError("The download is incomplete: model, tokenizer or weights are missing.")
        (staged_model / MODEL_MARKER).write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        if target.exists():
            raise ValueError("The model target appeared during download; it will not be overwritten.")
        staged_model.rename(target)


def make_report(data_dir: Path, python: str) -> dict:
    wanted = required_versions()
    try:
        info = interpreter_info(python)
        problems = platform_problems(info)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        info = {}
        problems = ["Cannot inspect the selected Python: " + str(error)]
    venv_python = data_dir / ".venv" / "bin" / "python"
    venv_scoped = data_dir.resolve() in (data_dir / ".venv").resolve().parents
    installed = installed_versions(venv_python, wanted) if venv_scoped else {}
    if not venv_scoped:
        problems.append("The existing virtual environment points outside the private data directory.")
    dependencies_ready = bool(installed) and all(installed.get(k) == v for k, v in wanted.items())
    model_dir = data_dir / "models" / "qwen3-base"
    profile_ready = (data_dir / "voice_profile.json").is_file() and (data_dir / "ref_voice.wav").is_file()
    return {
        "data_dir": str(data_dir),
        "python": python,
        "interpreter": info,
        "platform_problems": problems,
        "runtime_python": str(venv_python),
        "required_versions": wanted,
        "installed_versions": installed,
        "dependencies_ready": dependencies_ready,
        "model_present": model_files_present(model_dir),
        "model_repo": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "reference_present": profile_ready,
        "ffmpeg": shutil.which("ffmpeg"),
        "ready": not problems and dependencies_ready and model_files_present(model_dir) and profile_ready,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read-only readiness report (the default).")
    mode.add_argument("--plan", action="store_true", help="Show a dry run without changing files or downloading.")
    parser.add_argument("--install", action="store_true", help="Create/update DATA/.venv and install pinned dependencies (network).")
    parser.add_argument("--download-model", action="store_true", help="Download the pinned public model anonymously (network).")
    parser.add_argument("--python", default=default_python(), help="Python 3.12+ executable used to create the private venv.")
    parser.add_argument("--data-dir", help="Private data directory; defaults to LOCAL_VOICE_DATA_DIR or the macOS app-support directory.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check and (args.install or args.download_model):
        parser.error("--check cannot be combined with installation or download flags")
    try:
        data_dir = resolve_data_dir(args.data_dir)
        report = make_report(data_dir, args.python)
        if args.plan:
            report["plan"] = [
                "Create/update the private venv and install requirements.txt only with --install.",
                "Download the pinned public model only with --download-model (token=False).",
                "Prepare an authorized recording separately with prepare_reference.py.",
            ]
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if not args.install and not args.download_model:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 1
        if report["platform_problems"]:
            raise ValueError(" ".join(report["platform_problems"]))
        if args.install:
            install_runtime(data_dir, args.python)
        if args.download_model:
            download_model(data_dir)
        print(json.dumps(make_report(data_dir, args.python), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Setup error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
