#!/usr/bin/env python3
"""Offline reference-conditioned voice synthesis using explicitly authorized audio."""

import argparse
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from voice_paths import resolve_data_dir


MAX_CHARS = 2000
MAX_PROFILE_BYTES = 65536
WAV_NAME = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fff]+\.wav\Z")


def progress(stage, **data):
    print(json.dumps({"stage": stage, **data}, ensure_ascii=False, allow_nan=False), flush=True)


def chunks(text, limit=150):
    """Retain sentence boundaries when possible and cap each inference request."""
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("Chunk length must be a positive integer.")
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    result, current = [], ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) > limit and current:
            result.append(current)
            current = ""
        while len(sentence) > limit:
            if current:
                result.append(current)
                current = ""
            result.append(sentence[:limit])
            sentence = sentence[limit:]
        current += sentence
    if current:
        result.append(current)
    return result


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate voice profile field.")
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError("Invalid voice profile number.")


def private_asset(data_dir, value):
    """Only resolve model/reference assets inside this data directory."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Voice profile has an invalid local asset path.")
    candidate = Path(value).expanduser()
    try:
        resolved = (candidate if candidate.is_absolute() else data_dir / candidate).resolve()
    except (OSError, RuntimeError):
        raise ValueError("Voice profile has an invalid local asset path.") from None
    if data_dir not in resolved.parents:
        raise ValueError("Voice model and reference must remain inside the private data directory.")
    return resolved


def load_profile(data_dir):
    """Validate configuration and consent; this function never imports MLX."""
    data_dir = resolve_data_dir(data_dir)
    profile_path = data_dir / "voice_profile.json"
    if profile_path.is_symlink():
        raise ValueError("Voice profile must be a regular file in the private data directory.")
    try:
        descriptor = os.open(profile_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Voice profile must be a regular JSON file.")
            raw = handle.read(MAX_PROFILE_BYTES + 1)
    except FileNotFoundError:
        raise FileNotFoundError("请先完成本地设置，并确认参考声音的使用授权。") from None
    if not raw or len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("Voice profile is empty or too large.")
    try:
        profile = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_number)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Voice profile is not valid JSON.") from None
    if not isinstance(profile, dict) or profile.get("consent_confirmed") is not True:
        raise ValueError("请先明确确认参考声音已获得使用授权，再生成配音。")
    for field in ("name", "method", "model_repo", "reference_text"):
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Voice profile requires a nonempty {field} field.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError(f"Voice profile has invalid {field} text.") from None
    if len(profile["reference_text"]) > MAX_CHARS:
        raise ValueError("Reference transcript is too long.")
    language = profile.get("lang_code", "chinese")
    if not isinstance(language, str) or not language.strip() or len(language) > 32:
        raise ValueError("Voice profile has an invalid lang_code field.")
    profile["lang_code"] = language
    model = private_asset(data_dir, profile.get("model_path"))
    reference = private_asset(data_dir, profile.get("reference_audio"))
    if not model.is_dir() or not reference.is_file():
        raise FileNotFoundError("本地模型或已授权的原声参考尚未准备好，请先完成设置。")
    # A model directory must not contain links to assets outside the private data.
    for asset in model.rglob("*"):
        if asset.is_symlink():
            private_asset(data_dir, str(asset))
    return profile, model, reference


def output_target(data_dir, requested):
    directory = data_dir / "outputs"
    if directory.is_symlink():
        raise ValueError("拒绝使用符号链接作为输出目录。")
    output = Path(requested).expanduser().absolute()
    if output.parent.resolve() != directory or not WAV_NAME.fullmatch(output.name):
        raise ValueError("输出必须为私有数据 outputs 目录中的 WAV 文件。")
    if output.is_symlink() or output.exists() or output.with_suffix(".json").exists() or output.with_suffix(".json").is_symlink():
        raise FileExistsError("拒绝覆盖已有配音。")
    return directory, output


def save_outputs(directory, filename, audio, sample_rate, report, writer=None):
    """Anchor writes to a directory FD, creating each file exclusively (0600)."""
    if not WAV_NAME.fullmatch(filename):
        raise ValueError("Invalid output filename.")
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if writer is None:
        import soundfile as sf
        writer = sf.write
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        metadata_name = Path(filename).with_suffix(".json").name
        metadata_fd = os.open(metadata_name, create, 0o600, dir_fd=directory_fd)
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as metadata:
            audio_fd = os.open(filename, create, 0o600, dir_fd=directory_fd)
            with os.fdopen(audio_fd, "wb") as handle:
                writer(handle, audio, sample_rate, format="WAV", subtype="PCM_16")
            metadata.write(serialized)
    finally:
        os.close(directory_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="用已授权的原声参考生成本地 AI 配音；不上传声音、不变调。")
    parser.add_argument("--data-dir", help="Private data directory; defaults to LOCAL_VOICE_DATA_DIR or macOS Application Support.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file", type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New .wav file inside DATA_DIR/outputs.")
    args = parser.parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)
    text = args.text if args.text is not None else args.text_file.expanduser().read_text(encoding="utf-8")
    text = text.strip()
    if not text or len(text) > MAX_CHARS:
        raise ValueError("文案须为 1–2000 字。")
    text.encode("utf-8")
    directory, output = output_target(data_dir, args.output)
    profile, model_path, reference = load_profile(data_dir)
    # Heavy runtime imports happen only after --help and local configuration checks.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    began = time.monotonic()
    progress("loading_model", message="正在加载本地模型")
    model = load_model(model_path)
    mx.random.seed(42)
    pieces = chunks(text)
    samples, sample_rate = [], int(model.sample_rate)
    if sample_rate < 1:
        raise RuntimeError("模型采样率无效。")
    reference_audio, reference_rate = sf.read(reference, dtype="float32")
    if reference_rate != sample_rate or reference_audio.ndim != 1 or not reference_audio.size or not np.isfinite(reference_audio).all():
        raise RuntimeError("参考音频须为与模型采样率一致的有效单声道音频。")
    for index, piece in enumerate(pieces):
        progress("generating", part=index + 1, total=len(pieces), message="正在生成 AI 配音")
        generated = []
        for result in model.generate(text=piece, lang_code=profile["lang_code"],
                ref_audio=str(reference), ref_text=profile["reference_text"],
                temperature=0.7, top_p=0.95, repetition_penalty=1.05,
                max_tokens=1800, stream=False, verbose=False):
            mx.eval(result.audio)
            generated.append(np.asarray(result.audio.astype(mx.float32)).reshape(-1))
        if not generated:
            raise RuntimeError("模型没有返回有效音频。")
        if samples:
            samples.append(np.zeros(round(sample_rate * 0.25), dtype=np.float32))
        samples.extend(generated)
    audio = np.concatenate(samples).astype(np.float32)
    if not np.isfinite(audio).all() or len(audio) < sample_rate:
        raise RuntimeError("生成音频未通过有效性检查。")
    raw_peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < 1e-5 or raw_peak < 1e-4:
        raise RuntimeError("生成结果近似静音。")
    # Amplitude only: retain the generated timbre, pitch, rate, and duration.
    gain = min(10 ** (-18 / 20) / rms, 0.89 / raw_peak)
    audio *= gain
    if not np.isfinite(audio).all():
        raise RuntimeError("生成音频包含无效采样。")
    progress("checking", message="正在检查音频并导出")
    ref_embedding = np.asarray(model.extract_speaker_embedding(mx.array(reference_audio)).astype(mx.float32)).reshape(-1)
    output_embedding = np.asarray(model.extract_speaker_embedding(mx.array(audio)).astype(mx.float32)).reshape(-1)
    denominator = float(np.linalg.norm(ref_embedding) * np.linalg.norm(output_embedding))
    if not math.isfinite(denominator) or denominator <= 0:
        raise RuntimeError("声音嵌入检查不可用。")
    cosine = float(np.dot(ref_embedding, output_embedding) / denominator)
    if not math.isfinite(cosine):
        raise RuntimeError("声音嵌入检查未返回有效值。")
    report = {
        "status": "generated", "synthetic_audio": True, "output": str(output),
        "text": text, "sample_rate": sample_rate, "channels": 1,
        "duration_seconds": len(audio) / sample_rate, "finite_samples": True,
        "peak_dbfs": float(20 * np.log10(np.max(np.abs(audio)))),
        "speaker_embedding_cosine": cosine,
        "speaker_similarity_is_not_a_human_listening_verdict": True,
        "human_listening": "not_performed", "method": profile["method"],
        "reference": str(reference), "model_repo": profile["model_repo"],
        "model_path": str(model_path), "offline_generation": True, "voice_uploaded": False,
        "elapsed_seconds": time.monotonic() - began,
    }
    save_outputs(directory, output.name, audio, sample_rate, report, writer=sf.write)
    progress("complete", output=str(output), duration=report["duration_seconds"],
        speaker_embedding_cosine=cosine, elapsed_seconds=report["elapsed_seconds"])


if __name__ == "__main__":
    main()
