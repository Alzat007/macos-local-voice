"""Portable checks: no downloads, package installs, media tools or launchd calls."""

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name):
    spec = importlib.util.spec_from_file_location("voice_test_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_script("setup")
reference = load_script("prepare_reference")
service = load_script("service")
SUPPORTED = {"version": [3, 12, 0], "machine": "arm64", "system": "Darwin", "macos": "14.0"}


class SetupTests(unittest.TestCase):
    def test_plan_is_portable_and_does_not_create_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "new data"
            with patch.object(setup, "interpreter_info", return_value={"version": [3, 11], "machine": "x86_64", "system": "Linux"}), patch.object(setup.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
                result = setup.main(["--plan", "--install", "--download-model", "--data-dir", str(data)])
            self.assertEqual(result, 0)
            self.assertFalse(data.exists())
            run.assert_not_called()
            self.assertTrue(json.loads(output.getvalue())["platform_problems"])

    def test_check_missing_runtime_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "not created"
            with patch.object(setup, "interpreter_info", return_value=SUPPORTED), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main(["--check", "--data-dir", str(data)]), 1)
            self.assertFalse(data.exists())

    def test_unsupported_install_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "not created"
            with patch.object(setup, "interpreter_info", return_value={"version": [3, 12], "machine": "x86_64", "system": "Darwin", "macos": "14.0"}), patch.object(setup.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup.main(["--install", "--data-dir", str(data)]), 1)
            self.assertFalse(data.exists())
            run.assert_not_called()

    def test_install_uses_private_venv_and_requirements(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "private data"
            with patch.object(setup.subprocess, "run") as run:
                setup.install_runtime(data, "/a/python3.12")
            calls = [item.args[0] for item in run.call_args_list]
            self.assertEqual(calls[0], ["/a/python3.12", "-m", "venv", str(data / ".venv")])
            self.assertEqual(calls[1], [str(data / ".venv/bin/python"), "-m", "pip", "install", "-r", str(setup.REQUIREMENTS)])
            self.assertEqual(run.call_args_list[1].kwargs["env"]["PIP_REQUIRE_VIRTUALENV"], "true")

    def test_install_rejects_runtime_symlink_outside_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, other = base / "data", base / "other"
            data.mkdir()
            other.mkdir()
            (data / ".venv").symlink_to(other, target_is_directory=True)
            with patch.object(setup.subprocess, "run") as run, self.assertRaises(ValueError):
                setup.install_runtime(data, sys.executable)
            run.assert_not_called()

    def test_model_download_is_pinned_anonymous_and_staged(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            runtime = data / ".venv/bin/python"
            runtime.parent.mkdir(parents=True)
            runtime.touch()

            def fake_download(command, **kwargs):
                self.assertIn("token=False", command[2])
                self.assertEqual(command[3:5], [setup.MODEL_REPO, setup.MODEL_REVISION])
                self.assertEqual(kwargs["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
                staged = Path(command[5])
                staged.mkdir()
                for name in ("config.json", "tokenizer_config.json", "model.safetensors"):
                    (staged / name).write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            with patch.object(setup, "interpreter_info", return_value=SUPPORTED), patch.object(setup.subprocess, "run", side_effect=fake_download) as run:
                setup.download_model(data)
                self.assertTrue(setup.model_files_present(data / "models/qwen3-base"))
                setup.download_model(data)
            self.assertEqual(run.call_count, 1)
            marker = json.loads((data / "models/qwen3-base" / setup.MODEL_MARKER).read_text())
            self.assertEqual(marker["revision"], setup.MODEL_REVISION)

    def test_existing_unverified_model_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            runtime = data / ".venv/bin/python"
            runtime.parent.mkdir(parents=True)
            runtime.touch()
            model = data / "models/qwen3-base"
            model.mkdir(parents=True)
            with patch.object(setup, "interpreter_info", return_value=SUPPORTED), patch.object(setup.subprocess, "run") as run, self.assertRaises(ValueError):
                setup.download_model(data)
            run.assert_not_called()

    def test_dependency_inventory_runs_with_stdlib_only(self):
        versions = setup.installed_versions(Path(sys.executable), {"a-package-that-does-not-exist": "0"})
        self.assertEqual(versions, {"a-package-that-does-not-exist": None})


class ReferenceTests(unittest.TestCase):
    def make_args(self, base):
        source, transcript = base / "input media.wav", base / "verified.txt"
        source.write_bytes(b"unchanged source")
        transcript.write_text("Exact verified reference.\n", encoding="utf-8")
        return argparse.Namespace(source=str(source), text_file=str(transcript), start=1.0,
                                  duration=6.0, name="Example voice", ffmpeg=None,
                                  data_dir=str(base / "private data"), consent_own_voice=True)

    def fake_ffmpeg(self, command, **kwargs):
        self.assertIn("-n", command)
        self.assertIn("-nostdin", command)
        with wave.open(command[-1], "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\x01\x00" * 24000 * 6)
        return subprocess.CompletedProcess(command, 0)

    def test_explicit_consent_and_no_network_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            with patch.object(reference.shutil, "which", return_value="/public/ffmpeg"), patch.object(reference.subprocess, "run", side_effect=self.fake_ffmpeg):
                result = reference.prepare_reference(args)
            profile = json.loads(Path(result["profile"]).read_text())
            self.assertEqual(profile["reference_text"], "Exact verified reference.\n")
            self.assertTrue(profile["consent_confirmed"])
            self.assertFalse(profile["uploaded_to_third_party"])
            self.assertEqual(profile["reference_audio"], "ref_voice.wav")
            self.assertEqual(Path(args.source).read_bytes(), b"unchanged source")
            self.assertEqual(Path(result["profile"]).stat().st_mode & 0o777, 0o600)

    def test_transcript_line_endings_are_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            Path(args.text_file).write_bytes(b"Verified reference.\r\n")
            with patch.object(reference.shutil, "which", return_value="ffmpeg"), patch.object(reference.subprocess, "run", side_effect=self.fake_ffmpeg):
                result = reference.prepare_reference(args)
            profile = json.loads(Path(result["profile"]).read_text())
            self.assertEqual(profile["reference_text"], "Verified reference.\r\n")

    def test_voice_name_is_bounded_before_any_reference_is_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            for name in ("", "   ", "x" * 121):
                args.name = name
                with patch.object(reference.subprocess, "run") as run, self.assertRaises(ValueError):
                    reference.prepare_reference(args)
                run.assert_not_called()
                self.assertFalse(Path(args.data_dir).exists())
            args.name = "x" * 120
            with patch.object(reference.shutil, "which", return_value="ffmpeg"), patch.object(reference.subprocess, "run", side_effect=self.fake_ffmpeg):
                result = reference.prepare_reference(args)
            self.assertEqual(json.loads(Path(result["profile"]).read_text())["name"], "x" * 120)

    def test_all_zero_pcm_reference_does_not_create_voice_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))

            def silent_ffmpeg(command, **kwargs):
                with wave.open(command[-1], "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(b"\0\0" * 24000 * 6)
                return subprocess.CompletedProcess(command, 0)

            with patch.object(reference.shutil, "which", return_value="ffmpeg"), patch.object(reference.subprocess, "run", side_effect=silent_ffmpeg), self.assertRaisesRegex(ValueError, "entirely silent"):
                reference.prepare_reference(args)
            self.assertFalse((Path(args.data_dir) / "voice_profile.json").exists())
            self.assertFalse((Path(args.data_dir) / "ref_voice.wav").exists())
            self.assertEqual(Path(args.source).read_bytes(), b"unchanged source")

    def test_no_consent_or_invalid_interval_does_not_run_media_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            args.consent_own_voice = False
            with patch.object(reference.subprocess, "run") as run, self.assertRaises(ValueError):
                reference.prepare_reference(args)
            run.assert_not_called()
            self.assertFalse(Path(args.data_dir).exists())
        for value in ("nan", "inf", "-1", "0"):
            with self.assertRaises(argparse.ArgumentTypeError):
                reference.positive_seconds(value)

    def test_existing_reference_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            data = Path(args.data_dir)
            data.mkdir()
            audio = data / "ref_voice.wav"
            audio.write_bytes(b"keep original")
            with patch.object(reference.subprocess, "run") as run, self.assertRaises(ValueError):
                reference.prepare_reference(args)
            run.assert_not_called()
            self.assertEqual(audio.read_bytes(), b"keep original")

    def test_failed_conversion_does_not_create_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(Path(temporary))
            with patch.object(reference.shutil, "which", return_value="ffmpeg"), patch.object(reference.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["ffmpeg"])), self.assertRaises(subprocess.CalledProcessError):
                reference.prepare_reference(args)
            self.assertFalse((Path(args.data_dir) / "voice_profile.json").exists())
            self.assertFalse((Path(args.data_dir) / "ref_voice.wav").exists())


class ServiceTests(unittest.TestCase):
    def test_dry_run_never_writes_or_calls_launchctl(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "private data"
            args = service.build_parser().parse_args(["--install", "--dry-run", "--data-dir", str(data)])
            with patch.object(service.subprocess, "run") as run:
                report = service.manage(args)
            self.assertTrue(report["dry_run"])
            self.assertFalse(data.exists())
            run.assert_not_called()
            self.assertIn("127.0.0.1", report["plist"]["ProgramArguments"])
            self.assertIs(report["plist"]["KeepAlive"], True)
            self.assertEqual(report["plist"]["ThrottleInterval"], 10)
            self.assertEqual(report["plist"]["Umask"], 63)

    def test_plist_roundtrip_handles_spaces_and_special_characters(self):
        config = service.build_plist(Path("/private/example user & voice"), 8766)
        self.assertEqual(plistlib.loads(plistlib.dumps(config)), config)
        self.assertEqual(config["ProgramArguments"][0], "/private/example user & voice/.venv/bin/python")

    def test_install_and_uninstall_only_own_plist_keep_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, home = base / "private data", base / "fake home"
            python = data / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            install = service.build_parser().parse_args(["--install", "--data-dir", str(data)])
            uninstall = service.build_parser().parse_args(["--uninstall", "--data-dir", str(data)])
            with patch.object(service.Path, "home", return_value=home), patch.object(service.platform, "system", return_value="Darwin"), patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
                report = service.manage(install)
                run.assert_not_called()
                plist = Path(report["plist_path"])
                self.assertTrue(plist.exists())
                service.manage(uninstall)
                self.assertFalse(plist.exists())
            self.assertTrue(python.exists())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(all(command[1] == "print" for command in commands))

    def test_stop_uses_bootout_for_own_keepalive_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data, home = base / "private data", base / "fake home"
            stop = service.build_parser().parse_args(["--stop", "--data-dir", str(data)])
            with patch.object(service.Path, "home", return_value=home), patch.object(service.platform, "system", return_value="Darwin"), patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                report = service.manage(stop)
            target = service.current_target()[1]
            self.assertEqual(report["result"], "stopped")
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [[service.LAUNCHCTL, "print", target], [service.LAUNCHCTL, "bootout", target]])


if __name__ == "__main__":
    unittest.main()
