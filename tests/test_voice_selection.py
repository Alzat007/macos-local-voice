#!/usr/bin/env python3
"""Multi-voice contracts with temporary fixtures; never runs model inference."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
sys.dont_write_bytecode = True
import synthesize
import voice_catalog
import test_core as core

app = core.app


class VoiceFixtures:
    def create_profile(self, directory, name="Fixture voice"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "models" / "qwen3-base").mkdir(parents=True, exist_ok=True)
        (directory / "ref_voice.wav").write_bytes(core.WAV_FIXTURE)
        profile = {
            "name": name,
            "method": "local-reference-fixture",
            "reference_text": "Private fixture reference transcript.",
            "reference_audio": "ref_voice.wav",
            "model_path": "models/qwen3-base",
            "model_repo": "example/model",
            "lang_code": "chinese",
            "consent_confirmed": True,
        }
        (directory / "voice_profile.json").write_text(json.dumps(profile), encoding="utf-8")
        return profile

    def create_catalog(self, **changes):
        value = {
            "version": 1,
            "default_voice": "A",
            "voices": [
                {"id": "A", "name": "原音色", "data_dir": "."},
                {"id": "B", "name": "新音色", "data_dir": "variants/b"},
            ],
        }
        value.update(changes)
        (self.data / "voices.json").write_text(json.dumps(value), encoding="utf-8")
        return value


class CatalogTests(VoiceFixtures, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-selection-")
        self.data = Path(self.temporary.name).resolve()
        self.variant = self.data / "variants" / "b"
        self.create_profile(self.data)
        self.create_profile(self.variant)

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_catalog_preserves_single_voice_and_no_writes(self):
        before = (self.data / "voice_profile.json").read_bytes()
        catalog = voice_catalog.load_catalog(self.data)
        self.assertEqual(len(catalog["voices"]), 1)
        selected = voice_catalog.resolve_voice(self.data)
        self.assertEqual(selected["id"], catalog["default_voice"])
        self.assertEqual(selected["directory"], self.data)
        profile, model, reference = synthesize.load_profile(self.data)
        self.assertEqual(reference, self.data / "ref_voice.wav")
        self.assertEqual(model, self.data / "models" / "qwen3-base")
        self.assertEqual(profile["voice_id"], selected["id"])
        self.assertFalse((self.data / "voices.json").exists())
        self.assertEqual((self.data / "voice_profile.json").read_bytes(), before)

    def test_selection_changes_reference_without_changing_output_or_default(self):
        self.create_catalog()
        before = {path: path.read_bytes() for path in (
            self.data / "voices.json", self.data / "voice_profile.json", self.data / "ref_voice.wav",
        )}
        self.assertEqual(voice_catalog.resolve_voice(self.data)["id"], "A")
        selected = voice_catalog.resolve_voice(self.data, "B")
        self.assertEqual(selected["directory"], self.variant)
        profile, model, reference = synthesize.load_profile(self.data, "B")
        self.assertEqual(profile["voice_id"], "B")
        self.assertEqual(profile["voice_name"], "新音色")
        self.assertEqual(reference, self.variant / "ref_voice.wav")
        self.assertEqual(model, self.variant / "models" / "qwen3-base")
        directory, output = synthesize.output_target(self.data, self.data / "outputs" / "new.wav")
        self.assertEqual(directory, self.data / "outputs")
        self.assertEqual(output.parent, self.data / "outputs")
        self.assertEqual(voice_catalog.resolve_voice(self.data)["id"], "A")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_unknown_voice_never_falls_back(self):
        self.create_catalog()
        for unknown in ("C", "../B", "", 42, ["B"]):
            with self.subTest(voice=unknown), self.assertRaises(ValueError):
                voice_catalog.resolve_voice(self.data, unknown)

    def test_catalog_structure_and_duplicate_fields_are_rejected(self):
        documents = [
            "[]", '{"version":1,"version":1}', '{"version":NaN}',
            "[" * 1200 + "]" * 1200,
            json.dumps({"version": 1, "default_voice": "A", "voices": []}),
            json.dumps({"version": 1, "default_voice": "missing", "voices": [
                {"id": "A", "name": "A", "data_dir": "."}]}),
            json.dumps({"version": 1, "default_voice": "A", "voices": [
                {"id": "A", "name": "A", "data_dir": "."},
                {"id": "A", "name": "duplicate", "data_dir": "variants/b"}]}),
            json.dumps({"version": 1, "default_voice": "A", "voices": [
                {"id": "A", "name": "x" * 1_000_001, "data_dir": "."}]}),
        ]
        for content in documents:
            with self.subTest(length=len(content)):
                (self.data / "voices.json").write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    voice_catalog.load_catalog(self.data)

    def test_catalog_file_symlink_and_fifo_are_rejected(self):
        real = self.data / "catalog-copy.json"
        self.create_catalog()
        (self.data / "voices.json").rename(real)
        (self.data / "voices.json").symlink_to(real)
        with self.assertRaises(ValueError):
            voice_catalog.load_catalog(self.data)
        (self.data / "voices.json").unlink()
        os.mkfifo(self.data / "voices.json")
        with self.assertRaises(ValueError):
            voice_catalog.load_catalog(self.data)

    def test_catalog_paths_cannot_escape_the_private_root(self):
        with tempfile.TemporaryDirectory(prefix="voice-selection-outside-") as external:
            outside = Path(external).resolve()
            (self.data / "escape").symlink_to(outside, target_is_directory=True)
            for bad in (str(self.variant), "../outside", "variants/../../outside", "escape", True):
                with self.subTest(path=bad):
                    self.create_catalog(voices=[{"id": "A", "name": "A", "data_dir": bad}])
                    with self.assertRaises(ValueError):
                        voice_catalog.resolve_voice(self.data, "A")

    def test_selected_reference_still_requires_consent_and_local_assets(self):
        self.create_catalog()
        profile = self.create_profile(self.variant)
        profile["consent_confirmed"] = False
        (self.variant / "voice_profile.json").write_text(json.dumps(profile), encoding="utf-8")
        with self.assertRaises(ValueError):
            synthesize.load_profile(self.data, "B")
        self.assertEqual(synthesize.load_profile(self.data, "A")[0]["voice_id"], "A")
        self.create_profile(self.variant)
        (self.variant / "ref_voice.wav").unlink()
        with self.assertRaises(FileNotFoundError):
            synthesize.load_profile(self.data, "B")

    def test_fingerprint_detects_reference_and_transcript_changes(self):
        self.create_catalog()
        profile, model, reference = synthesize.load_profile(self.data, "B")
        fingerprint = synthesize.voice_fingerprint(profile, model, reference)
        self.assertRegex(fingerprint, r"\A[a-f0-9]{64}\Z")
        self.assertEqual(fingerprint, synthesize.voice_fingerprint(profile, model, reference))
        reference.write_bytes(core.WAV_FIXTURE + b"changed fixture")
        self.assertNotEqual(fingerprint, synthesize.voice_fingerprint(profile, model, reference))
        reference.write_bytes(core.WAV_FIXTURE)
        changed = {**profile, "reference_text": "Different fixture reference transcript."}
        self.assertNotEqual(fingerprint, synthesize.voice_fingerprint(changed, model, reference))

    def test_cli_stale_fingerprint_fails_before_runtime_or_output(self):
        self.create_catalog()
        output = self.data / "outputs" / "must-not-exist.wav"
        with self.assertRaises(ValueError):
            synthesize.main([
                "--data-dir", str(self.data), "--voice", "B", "--text", "测试文稿",
                "--expected-voice-fingerprint", "0" * 64, "--output", str(output),
            ])
        self.assertFalse(output.exists())
        self.assertNotIn("mlx.core", sys.modules)


class VoiceBackendTests(VoiceFixtures, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-selection-api-")
        self.data = Path(self.temporary.name).resolve()
        self.variant = self.data / "variants" / "b"
        self.create_profile(self.data)
        self.create_profile(self.variant)
        self.create_catalog()
        self.outputs = self.data / "outputs"
        self.outputs.mkdir()
        self.python = self.data / ".venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        for field, value in (("DATA", self.data), ("OUTPUTS", self.outputs), ("PYTHON", self.python),
                             ("JOBS", {}), ("CURRENT_JOB", None)):
            patch = mock.patch.object(app, field, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def probe(self, path="/api/status", payload=None):
        return core.BackendTests().probe(path, payload)

    def pair(self, filename, **changes):
        audio = self.outputs / filename
        audio.write_bytes(core.WAV_FIXTURE)
        report = {
            "status": "generated", "synthetic_audio": True,
            "text": "测试文稿", "duration_seconds": 2.5,
            "reference": str(self.variant / "ref_voice.wav"),
            "model_path": str(self.variant / "models" / "qwen3-base"),
        }
        report.update(changes)
        audio.with_suffix(".json").write_text(json.dumps(report), encoding="utf-8")

    def test_status_exposes_labels_and_readiness_without_private_assets(self):
        handler = self.probe()
        handler.do_GET()
        code, status, _, _ = handler.response
        self.assertEqual(code, 200)
        self.assertEqual(status["default_voice"], "A")
        voices = {item["id"]: item for item in status["voices"]}
        self.assertEqual(set(voices), {"A", "B"})
        self.assertEqual(voices["B"]["name"], "新音色")
        self.assertTrue(voices["A"]["ready"])
        self.assertTrue(voices["B"]["ready"])
        serialized = json.dumps(status)
        for private in (str(self.data), "reference_text", "reference_audio", "model_path"):
            self.assertNotIn(private, serialized)

    def test_unavailable_variant_does_not_block_valid_default(self):
        (self.variant / "ref_voice.wav").unlink()
        self.assertTrue(app.configuration_status("A")[0])
        self.assertFalse(app.configuration_status("B")[0])
        handler = self.probe()
        handler.do_GET()
        status = handler.response[1]
        self.assertTrue(status["ready"])
        voices = {item["id"]: item for item in status["voices"]}
        self.assertTrue(voices["A"]["ready"])
        self.assertFalse(voices["B"]["ready"])

    def test_post_binds_voice_and_fingerprint_to_the_job(self):
        expected = synthesize.voice_fingerprint(*synthesize.load_profile(self.data, "B"))
        with mock.patch.object(app.threading, "Thread") as thread:
            handler = self.probe("/api/generate", {"text": "测试文稿", "voice_id": "B"})
            handler.do_POST()
            self.assertEqual(handler.response[0], 202)
            job_id = handler.response[1]["job_id"]
            job = app.JOBS[job_id]
            self.assertEqual(job["voice_id"], "B")
            self.assertEqual(job["voice_name"], "新音色")
            arguments = thread.call_args.kwargs["args"]
            self.assertEqual(arguments[3], "B")
            self.assertEqual(arguments[4], expected)
            self.create_catalog(default_voice="B")
            self.assertEqual(arguments[3], "B")
            self.assertEqual(app.JOBS[job_id]["voice_id"], "B")
            self.assertNotIn(str(self.data), json.dumps(app.public_job(job)))
            thread.return_value.start.assert_called_once()

    def test_old_post_uses_default_and_unknown_voice_is_not_queued(self):
        with mock.patch.object(app.threading, "Thread") as thread:
            for voice in ("missing", "../B", "", 1, ["B"]):
                handler = self.probe("/api/generate", {"text": "测试文稿", "voice_id": voice})
                handler.do_POST()
                self.assertEqual(handler.response[0], 400)
            thread.assert_not_called()
            self.assertEqual(app.JOBS, {})
            handler = self.probe("/api/generate", {"text": "测试文稿"})
            handler.do_POST()
            self.assertEqual(handler.response[0], 202)
            self.assertEqual(app.JOBS[handler.response[1]["job_id"]]["voice_id"], "A")

    def test_generate_keeps_output_root_and_passes_bound_voice(self):
        self.pair("selection.wav", voice_id="B", voice_name="新音色")
        text_path = self.data / "request.txt"
        text_path.write_text("测试文稿", encoding="utf-8")
        job_id = "b" * 32
        app.JOBS[job_id] = {"job_id": job_id, "status": "queued", "voice_id": "B", "voice_name": "新音色"}
        fingerprint = synthesize.voice_fingerprint(*synthesize.load_profile(self.data, "B"))
        with mock.patch.object(app.subprocess, "Popen", return_value=core.FakeProcess("")) as process:
            app.generate(job_id, text_path, "selection.wav", "B", fingerprint)
        arguments = process.call_args.args[0]
        self.assertEqual(arguments[0], str(self.python))
        self.assertEqual(arguments[arguments.index("--data-dir") + 1], str(self.data))
        self.assertEqual(arguments[arguments.index("--voice") + 1], "B")
        self.assertEqual(arguments[arguments.index("--expected-voice-fingerprint") + 1], fingerprint)
        self.assertEqual(arguments[arguments.index("--output") + 1], str(self.outputs / "selection.wav"))
        self.assertEqual(app.JOBS[job_id]["status"], "done")
        self.assertFalse(text_path.exists())

    def test_history_preserves_new_labels_without_guessing_old_voices(self):
        self.pair("old.wav")
        self.pair("new.wav", voice_id="B", voice_name="新音色")
        items = {item["filename"]: item for item in app.read_history()}
        self.assertNotIn("voice_id", items["old.wav"])
        self.assertNotIn("voice_name", items["old.wav"])
        self.assertEqual(items["new.wav"]["voice_id"], "B")
        self.assertEqual(items["new.wav"]["voice_name"], "新音色")
        self.assertNotIn(str(self.data), json.dumps(items))
        self.pair("bad-label.wav", voice_id="../escape", voice_name="bad")
        invalid = next((item for item in app.read_history() if item["filename"] == "bad-label.wav"), None)
        if invalid is not None:
            self.assertNotIn("voice_id", invalid)
            self.assertNotIn("voice_name", invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
