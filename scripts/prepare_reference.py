#!/usr/bin/env python3
"""Create a local reference from an authorized recording and verified transcript."""

from __future__ import annotations

import argparse
from array import array
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from voice_paths import resolve_data_dir


MODEL_REPO = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
REFERENCE_RATE = 24000
PCM16_MAX = 32767 / 32768
PROTECTED_PEAK = 10 ** (-1 / 20)


def float_blocks(path):
    """Read little-endian float PCM in bounded blocks, rejecting invalid samples."""
    with path.open("rb") as audio:
        while raw := audio.read(65536):
            if len(raw) % 4:
                raise ValueError("The decoded reference audio is incomplete.")
            samples = [item[0] for item in struct.iter_unpack("<f", raw)]
            if not all(math.isfinite(sample) for sample in samples):
                raise ValueError("The decoded reference audio contains invalid samples.")
            yield samples


def protect_pcm16(decoded, destination, expected_duration):
    """Quantize float PCM only after measuring peaks; do not compress dynamics."""
    peak, frames = 0.0, 0
    for samples in float_blocks(decoded):
        frames += len(samples)
        peak = max(peak, max(abs(sample) for sample in samples))
    duration = frames / REFERENCE_RATE
    if duration <= 0 or abs(duration - expected_duration) > 0.15:
        raise ValueError("The recording does not contain the full requested reference interval.")
    if peak == 0:
        raise ValueError("The reference audio is entirely silent; select a recording with audible speech.")
    gain = PROTECTED_PEAK / peak if peak > PCM16_MAX else 1.0
    output_peak = 0
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setframerate(REFERENCE_RATE)
        audio.setsampwidth(2)
        for samples in float_blocks(decoded):
            quantized = array("h", (round(sample * gain * 32768) for sample in samples))
            output_peak = max(output_peak, max(abs(sample) for sample in quantized))
            if sys.byteorder != "little":
                quantized.byteswap()
            audio.writeframesraw(quantized.tobytes())
    if output_peak == 0:
        raise ValueError("The reference audio is entirely silent after PCM conversion; select a louder recording.")
    return {
        "decoded_peak_dbfs": 20 * math.log10(peak),
        "applied_gain_db": 20 * math.log10(gain),
        "output_peak_dbfs": 20 * math.log10(output_peak / 32768),
        "peak_protection": "fixed_attenuation_when_pcm16_would_clip",
    }


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a finite number of seconds.") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Duration must be finite and greater than zero.")
    return number


def start_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use a finite number of seconds.") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("Start must be finite and zero or greater.")
    return number


def prepare_reference(args) -> dict:
    if not args.consent_own_voice:
        raise ValueError("Confirm ownership or explicit voice-use authorization with --consent-own-voice.")
    data_dir = resolve_data_dir(args.data_dir)
    source = Path(args.source).expanduser().resolve(strict=True)
    transcript_path = Path(args.text_file).expanduser().resolve(strict=True)
    if not source.is_file() or not transcript_path.is_file():
        raise ValueError("The recording and transcript must both be existing files.")
    transcript = transcript_path.read_bytes().decode("utf-8")
    if not transcript.strip() or len(transcript) > 2000:
        raise ValueError("The verified UTF-8 reference transcript must contain 1–2000 characters.")
    if args.name is not None and (not 1 <= len(args.name) <= 120 or not args.name.strip()):
        raise ValueError("The private voice name must contain 1–120 characters and cannot be blank.")
    reference = data_dir / "ref_voice.wav"
    profile_path = data_dir / "voice_profile.json"
    if reference.exists() or profile_path.exists() or reference.is_symlink() or profile_path.is_symlink():
        raise ValueError("A voice reference or profile already exists; no files were overwritten.")
    ffmpeg = shutil.which(args.ffmpeg or "ffmpeg")
    if not ffmpeg:
        raise ValueError("FFmpeg was not found. Install it separately or provide --ffmpeg.")
    if not math.isfinite(args.start) or args.start < 0 or not math.isfinite(args.duration) or args.duration <= 0:
        raise ValueError("Start and duration must be finite, with start >= 0 and duration > 0.")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    profile = {
        "name": args.name or "Local voice",
        "method": "qwen3_tts_base_reference",
        "reference_audio": "ref_voice.wav",
        "reference_text": transcript,
        "model_path": "models/qwen3-base",
        "model_repo": MODEL_REPO,
        "lang_code": "chinese",
        "consent_confirmed": True,
        "voice_usage_basis": "User confirmed ownership or explicit authorization for this voice and local synthesis.",
        "uploaded_to_third_party": False,
    }
    with tempfile.TemporaryDirectory(prefix=".reference-", dir=data_dir) as staging:
        decoded_audio = Path(staging) / "decoded.f32"
        staged_audio = Path(staging) / "reference.wav"
        subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
             "-ss", str(args.start), "-i", str(source), "-t", str(args.duration),
             "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(REFERENCE_RATE),
             "-c:a", "pcm_f32le", "-f", "f32le", str(decoded_audio)],
            check=True,
        )
        preparation = protect_pcm16(decoded_audio, staged_audio, args.duration)
        profile["reference_preparation"] = preparation
        with wave.open(str(staged_audio), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if audio.getnchannels() != 1 or audio.getframerate() != 24000 or audio.getsampwidth() != 2:
                raise ValueError("FFmpeg did not produce mono 24 kHz PCM16 audio.")
            if duration <= 0 or abs(duration - args.duration) > 0.15:
                raise ValueError("The recording does not contain the full requested reference interval.")
            has_nonzero_sample = False
            frames_read = 0
            while True:
                samples = audio.readframes(4096)
                if not samples:
                    break
                frames_read += len(samples) // 2
                has_nonzero_sample = has_nonzero_sample or any(samples)
            if frames_read != audio.getnframes():
                raise ValueError("The reference audio is incomplete.")
            if not has_nonzero_sample:
                raise ValueError("The reference audio is entirely silent; select a recording with audible speech.")
        created_reference = False
        created_profile = False
        try:
            # Hard-linking in the same private directory refuses an existing target.
            os.link(staged_audio, reference)
            created_reference = True
            reference.chmod(0o600)
            descriptor = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created_profile = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as profile_file:
                json.dump(profile, profile_file, ensure_ascii=False, indent=2)
                profile_file.write("\n")
        except BaseException:
            if created_profile:
                profile_path.unlink()
            if created_reference:
                reference.unlink()
            raise
    return {"profile": str(profile_path), "reference": str(reference), "duration_seconds": duration,
            "reference_preparation": preparation}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Read-only source recording or video.")
    parser.add_argument("--start", type=start_seconds, default=0.0, help="Reference start in seconds.")
    parser.add_argument("--duration", type=positive_seconds, required=True, help="Reference duration in seconds; 6–15 seconds is recommended.")
    parser.add_argument("--text-file", required=True, help="UTF-8 text, manually verified to exactly match the selected recording.")
    parser.add_argument("--name", help="Private display name for the voice (1–120 characters).")
    parser.add_argument("--ffmpeg", help="FFmpeg executable (otherwise search PATH).")
    parser.add_argument("--data-dir", help="Private data directory, outside the skill source.")
    parser.add_argument("--consent-own-voice", action="store_true", required=True, help="Confirm this is your voice or you have explicit authorization to clone it.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare_reference(args)
        if not 6 <= args.duration <= 15:
            print("A clear 6–15 second reference is recommended for voice matching.", file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, wave.Error, subprocess.CalledProcessError) as error:
        print("Reference error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
