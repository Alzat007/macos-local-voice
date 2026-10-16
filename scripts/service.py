#!/usr/bin/env python3
"""Explicitly manage the local voice panel as a per-user macOS launchd job."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys

from voice_paths import resolve_data_dir


LABEL = "org.macos-local-voice.server"
APP_SCRIPT = Path(__file__).resolve().parent / "voice_app.py"
LAUNCHCTL = "/bin/launchctl"


def service_paths(data_dir: Path) -> tuple[Path, Path]:
    home = Path.home().resolve()
    agents = home / "Library" / "LaunchAgents"
    if home not in agents.resolve().parents:
        raise ValueError("The user LaunchAgents directory points outside the user's home directory.")
    return agents / (LABEL + ".plist"), data_dir / "logs"


def build_plist(data_dir: Path, port: int) -> dict:
    _, logs = service_paths(data_dir)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(data_dir / ".venv" / "bin" / "python"), str(APP_SCRIPT),
                             "--data-dir", str(data_dir), "--host", "127.0.0.1", "--port", str(port)],
        "WorkingDirectory": str(data_dir),
        "EnvironmentVariables": {"LOCAL_VOICE_DATA_DIR": str(data_dir), "PYTHONNOUSERSITE": "1"},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "Umask": 63,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "service.stdout.log"),
        "StandardErrorPath": str(logs / "service.stderr.log"),
    }


def current_target() -> tuple[str, str]:
    domain = "gui/" + str(os.getuid())
    return domain, domain + "/" + LABEL


def is_loaded(target: str) -> bool:
    return subprocess.run([LAUNCHCTL, "print", target], capture_output=True, text=True).returncode == 0


def own_plist(path: Path) -> dict | None:
    if path.is_symlink():
        raise ValueError("The service plist is a symbolic link; it will not be changed.")
    if not path.exists():
        return None
    with path.open("rb") as handle:
        config = plistlib.load(handle)
    if config.get("Label") != LABEL:
        raise ValueError("The existing plist does not belong to this service.")
    return config


def manage(args) -> dict:
    data_dir = resolve_data_dir(args.data_dir)
    plist_path, logs = service_paths(data_dir)
    config = build_plist(data_dir, args.port)
    action = next((name for name in ("install", "start", "stop", "uninstall") if getattr(args, name)), "status")
    domain, target = current_target()
    report = {"action": action, "label": LABEL, "plist_path": str(plist_path), "data_dir": str(data_dir)}
    if args.dry_run:
        report.update({"dry_run": True, "plist": config, "launchd_domain": domain,
                       "notes": "Install writes the user plist; start loads it. Stop/uninstall target only this service. Private data is retained."})
        return report
    if platform.system() != "Darwin":
        raise ValueError("launchd management requires macOS; use --dry-run on other systems.")
    existing = own_plist(plist_path)
    if action == "status":
        report.update({"installed": existing is not None, "loaded": is_loaded(target)})
        return report
    if action == "install":
        if existing is not None:
            if existing == config:
                report["result"] = "already installed"
                return report
            raise ValueError("An existing service configuration differs; uninstall it explicitly before installing a replacement.")
        if data_dir not in (data_dir / ".venv").resolve().parents or logs.is_symlink() or data_dir not in logs.resolve().parents:
            raise ValueError("Runtime and log directories must remain inside the private data directory.")
        for filename in ("service.stdout.log", "service.stderr.log"):
            if (logs / filename).is_symlink():
                raise ValueError("Service log files must not be symbolic links.")
        if not (data_dir / ".venv" / "bin" / "python").is_file() or not APP_SCRIPT.is_file():
            raise ValueError("Install the private runtime first and keep the skill source at its installed path.")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        with plist_path.open("xb") as handle:
            plistlib.dump(config, handle)
        report["result"] = "installed; use --start to start now, or it starts at next login"
        return report
    if action == "start":
        if existing is None:
            raise ValueError("No service is installed. Use --install before --start.")
        if existing != config:
            raise ValueError("The installed data directory, source path or port differs; use matching arguments or explicitly reinstall.")
        command = [LAUNCHCTL, "kickstart", target] if is_loaded(target) else [LAUNCHCTL, "bootstrap", domain, str(plist_path)]
        subprocess.run(command, check=True)
        report["result"] = "started"
        return report
    if is_loaded(target):
        subprocess.run([LAUNCHCTL, "bootout", target], check=True)
    if action == "uninstall" and existing is not None:
        plist_path.unlink()
    report["result"] = "uninstalled; private data retained" if action == "uninstall" else "stopped"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true", help="Write only this user's login-service plist; does not start it now.")
    action.add_argument("--start", action="store_true", help="Load/start the explicitly installed service.")
    action.add_argument("--stop", action="store_true", help="Unload this service, retaining its login configuration.")
    action.add_argument("--status", action="store_true", help="Read-only installed/loaded status (default).")
    action.add_argument("--uninstall", action="store_true", help="Unload and remove only this service's plist; retain all private data.")
    parser.add_argument("--data-dir", help="The private data directory used by the service.")
    parser.add_argument("--port", type=int, default=8766, help="Loopback HTTP port (default: 8766).")
    parser.add_argument("--dry-run", action="store_true", help="Describe the action and generated plist; do not write or call launchctl.")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        print(json.dumps(manage(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.CalledProcessError) as error:
        print("Service error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
