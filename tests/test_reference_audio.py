"""Peak protection with synthetic samples; optional local FFmpeg integration."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
import wave


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_reference as reference


FFMPEG = shutil.which(os.environ.get("FFMPEG_BINARY", "ffmpeg"))


def write_floats(path, samples):
    with path.open("wb") as output:
        for sample in samples:
            output.write(struct.pack("<f", sample))


def read_pcm16(path):
    with wave.open(str(path), "rb") as audio:
        parameters = (audio.getnchannels(), audio.getframerate(), audio.getsampwidth())
        samples = [item[0] for item in struct.iter_unpack("<h", audio.readframes(audio.getnframes()))]
    return parameters, samples


def write_float_wave(path, samples, rate=48000):
    """IEEE-float WAV fixture may intentionally contain samples beyond full scale."""
    raw = b"".join(struct.pack("<f", sample) for sample in samples)
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt "
                     + struct.pack("<I", len(fmt)) + fmt + b"data"
                     + struct.pack("<I", len(raw)) + raw)


class PeakProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-reference-peaks-")
        self.root = Path(self.temporary.name)
        self.decoded = self.root / "decoded.f32"
        self.output = self.root / "reference.wav"

    def tearDown(self):
        self.temporary.cleanup()

    def test_over_full_scale_is_attenuated_without_clipping_or_changing_dynamics(self):
        samples = [2.4, -2.4, 1.2, -0.6, 0.0] * 4800
        write_floats(self.decoded, samples)
        result = reference.protect_pcm16(self.decoded, self.output, 1.0)
        parameters, pcm = read_pcm16(self.output)
        self.assertEqual(parameters, (1, 24000, 2))
        self.assertEqual(len(pcm), len(samples))
        self.assertGreater(result["decoded_peak_dbfs"], 7.5)
        self.assertLess(result["applied_gain_db"], -8.5)
        self.assertAlmostEqual(result["output_peak_dbfs"], -1.0, places=3)
        self.assertTrue(all(-32768 < sample < 32767 for sample in pcm))
        self.assertLessEqual(abs(pcm[0] - 2 * pcm[2]), 1)
        self.assertLessEqual(abs(pcm[1] - 4 * pcm[3]), 2)
        self.assertEqual(pcm[4], 0)

    def test_ordinary_reference_keeps_its_level(self):
        samples = [0.5, -0.25, 0.125, 0.0] * 6000
        write_floats(self.decoded, samples)
        result = reference.protect_pcm16(self.decoded, self.output, 1.0)
        _, pcm = read_pcm16(self.output)
        self.assertEqual(result["applied_gain_db"], 0.0)
        self.assertEqual(pcm[:4], [16384, -8192, 4096, 0])

    def test_positive_full_scale_is_protected_before_integer_quantization(self):
        write_floats(self.decoded, [1.0, -1.0] * 12000)
        result = reference.protect_pcm16(self.decoded, self.output, 1.0)
        _, pcm = read_pcm16(self.output)
        self.assertAlmostEqual(result["applied_gain_db"], -1.0, places=6)
        self.assertLess(max(abs(sample) for sample in pcm), 32767)

    def test_invalid_float_and_incomplete_decode_do_not_create_output(self):
        for sample in (float("nan"), float("inf"), float("-inf")):
            write_floats(self.decoded, [0.25, sample])
            with self.assertRaisesRegex(ValueError, "invalid samples"):
                reference.protect_pcm16(self.decoded, self.output, 1.0)
            self.assertFalse(self.output.exists())
        self.decoded.write_bytes(b"\x00\x01")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            reference.protect_pcm16(self.decoded, self.output, 1.0)
        self.assertFalse(self.output.exists())

    def test_missing_duration_and_silence_fail_before_profile_can_be_created(self):
        write_floats(self.decoded, [0.25] * 2400)
        with self.assertRaisesRegex(ValueError, "full requested reference interval"):
            reference.protect_pcm16(self.decoded, self.output, 1.0)
        self.assertFalse(self.output.exists())
        write_floats(self.decoded, [0.0] * 24000)
        with self.assertRaisesRegex(ValueError, "entirely silent"):
            reference.protect_pcm16(self.decoded, self.output, 1.0)
        self.assertFalse(self.output.exists())


@unittest.skipUnless(FFMPEG, "Set FFMPEG_BINARY or install FFmpeg for synthetic audio integration checks.")
class ReferenceFFmpegTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="voice-reference-ffmpeg-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic-float.wav"
        self.transcript = self.root / "verified.txt"
        self.transcript.write_text("Synthetic test fixture; no human voice.", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, start=0, duration=1):
        args = argparse.Namespace(source=str(self.source), text_file=str(self.transcript),
                                  start=start, duration=duration, name="Synthetic fixture",
                                  ffmpeg=FFMPEG, data_dir=str(self.root / "private"),
                                  consent_own_voice=True)
        return reference.prepare_reference(args)

    def test_real_decoder_preserves_overrange_until_gain_is_applied(self):
        rate = 48000
        write_float_wave(self.source, [2.4 * math.sin(2 * math.pi * 440 * index / rate)
                                       for index in range(rate)])
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.prepare()
        parameters, pcm = read_pcm16(Path(result["reference"]))
        preparation = result["reference_preparation"]
        self.assertEqual(parameters, (1, 24000, 2))
        self.assertEqual(len(pcm), 24000)
        self.assertGreater(preparation["decoded_peak_dbfs"], 7)
        self.assertLess(preparation["applied_gain_db"], -8)
        self.assertAlmostEqual(preparation["output_peak_dbfs"], -1, places=3)
        self.assertTrue(all(-32768 < sample < 32767 for sample in pcm))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
        profile = json.loads(Path(result["profile"]).read_text())
        self.assertEqual(profile["reference_preparation"], preparation)
        self.assertTrue(profile["consent_confirmed"])
        self.assertFalse(profile["uploaded_to_third_party"])
        self.assertEqual(Path(result["reference"]).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.prepare()

    def test_peak_outside_selected_interval_does_not_lower_reference(self):
        rate = 48000
        write_float_wave(self.source, [(0.25 if index < rate else 2.4)
                                       * math.sin(2 * math.pi * 440 * index / rate)
                                       for index in range(rate * 2)])
        result = self.prepare(start=0.2, duration=0.6)
        preparation = result["reference_preparation"]
        self.assertEqual(preparation["applied_gain_db"], 0)
        self.assertLess(preparation["output_peak_dbfs"], -11)
        self.assertAlmostEqual(result["duration_seconds"], 0.6, places=3)


if __name__ == "__main__":
    unittest.main()
