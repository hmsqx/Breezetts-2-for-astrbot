"""Create an approximate lyric timeline on CPU, in a short-lived subprocess."""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
import unicodedata
import wave
from pathlib import Path

MAX_SECONDS = 600
MAX_TIMELINE_BYTES = 512 * 1024
MAX_SEGMENTS = 300
TIMELINE_VERSION = 1
TIMESTAMP_EPSILON = 0.0001
LANGUAGES = {
    "": None,
    "zh": "chinese",
    "en": "english",
    "ja": "japanese",
    "ko": "korean",
}
ASR_ASSETS = (
    "model.safetensors",
    "config.json",
    "preprocessor_config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "normalizer.json",
    "merges.txt",
    "vocab.json",
)


def inspect_source(path: Path) -> tuple[dict, float]:
    """Validate normalized PCM and fingerprint the actual file, without inference."""
    before = path.stat()
    if not path.is_file() or not 44 <= before.st_size <= 200 * 1024 * 1024:
        raise ValueError("歌曲源文件无效或过大。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        header = stream.read(12)
        if (
            header[:4] != b"RIFF"
            or header[8:] != b"WAVE"
            or struct.unpack_from("<I", header, 4)[0] + 8 != before.st_size
        ):
            raise ValueError("歌曲 WAV 文件不完整。")
        digest.update(header)
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    with wave.open(str(path), "rb") as source:
        frames, rate = source.getnframes(), source.getframerate()
        if (
            source.getnchannels(),
            source.getsampwidth(),
            rate,
            source.getcomptype(),
        ) != (2, 2, 44100, "NONE"):
            raise ValueError("歌曲必须是已收录的 44.1kHz 双声道 PCM WAV。")
        duration = frames / rate
        if not 0 < duration <= MAX_SECONDS:
            raise ValueError("歌曲时长无效，最多支持 600 秒。")
        source.setpos(frames - 1)
        if len(source.readframes(1)) != 4:
            raise ValueError("歌曲 WAV 文件不完整。")
    after = path.stat()
    fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_ino", "st_dev")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError("歌曲源文件在读取期间发生变化，请重试。")
    identity = {field: getattr(after, field) for field in fields}
    identity["sha256"] = digest.hexdigest()
    return identity, duration


def validate_timeline(payload: dict, duration: float, language_hint: str = "") -> dict:
    """Reject unusable timestamps and obvious repetition; never invent boundaries."""
    if not isinstance(payload, dict) or payload.get("source") != "local_whisper":
        raise ValueError("本地音频时间轴格式无效。")
    actual = payload.get("duration")
    if (
        type(actual) not in (int, float)
        or not math.isfinite(actual)
        or abs(actual - duration) > 0.01
    ):
        raise ValueError("本地时间轴与歌曲时长不一致。")
    language = payload.get("language")
    if (
        not isinstance(language, str)
        or language not in LANGUAGES
        or (language_hint and language != language_hint)
    ):
        raise ValueError("本地时间轴的语言信息无效。")
    items = payload.get("segments")
    if not isinstance(items, list) or not 4 <= len(items) <= MAX_SEGMENTS:
        raise ValueError("未识别到足够的完整歌词句，无法可靠选择副歌；可指定手动时间。")
    segments, previous_end, previous_text, repeated, total_chars = [], 0.0, "", 0, 0
    repeated_start, repeated_chars, distinct_text = 0.0, 0, set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("本地时间轴包含无效段落。")
        start, end, text = item.get("start"), item.get("end"), item.get("text")
        if (
            type(start) not in (int, float)
            or type(end) not in (int, float)
            or not math.isfinite(start)
            or not math.isfinite(end)
        ):
            raise ValueError(
                f"音频识别第{index}段时间戳缺失或不是有限数值"
                f"（start={repr(start)[:40]}, end={repr(end)[:40]}）。"
            )
        boundary = (
            f"第{index}段 start={start:.6f}, end={end:.6f}, "
            f"previous_end={previous_end:.6f}, duration={duration:.6f}"
        )
        if end <= start:
            raise ValueError(
                f"音频识别段落为零时长或倒序（{boundary}），不能补造时间。"
            )
        if start < -TIMESTAMP_EPSILON or end > duration + TIMESTAMP_EPSILON:
            raise ValueError(f"音频识别时间戳超出音源范围（{boundary}）。")
        if start < previous_end - TIMESTAMP_EPSILON:
            raise ValueError(f"音频识别段落明显重叠（{boundary}），不能直接裁掉歌词。")
        # Only reconcile floating-point noise, far below Whisper's 20 ms token grid.
        start, end = max(0.0, start, previous_end), min(duration, end)
        if end <= start:
            raise ValueError(f"音频识别段落没有有效时长（{boundary}）。")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("音频识别歌词为空或过长。")
        text = " ".join(text.split())
        key = "".join(
            char
            for char in unicodedata.normalize("NFKC", text).casefold()
            if char.isalnum()
        )
        if not key or any(unicodedata.category(char) == "Cc" for char in text):
            raise ValueError("音频识别歌词无效。")
        if key == previous_text and start - previous_end <= 1:
            repeated += 1
            repeated_chars += len(key)
        else:
            repeated, repeated_start, repeated_chars = 1, start, len(key)
        # Refrains legitimately repeat; reject a loop only with implausibly dense text.
        if (repeated >= 3 and repeated_chars / (end - repeated_start) > 20) or (
            len(key) >= 24
            and re.search(r"(.{2,40})\1{4,}", key)
            and len(key) / (end - start) > 20
        ):
            raise ValueError(
                "音频识别出现异常密集的重复内容，无法确认可靠歌词时间轴；可指定手动时间。"
            )
        distinct_text.add(key)
        total_chars += len(text)
        if total_chars > 60000:
            raise ValueError("音频识别内容过长。")
        segments.append({"start": float(start), "end": float(end), "text": text})
        previous_end, previous_text = end, key
    if total_chars < 20:
        raise ValueError("音频识别内容过少，无法可靠判断完整副歌；可指定手动时间。")
    if len(distinct_text) < 2:
        raise ValueError(
            "音频识别始终重复同一句，无法确认完整歌词时间轴；可指定手动时间。"
        )
    result = {
        "duration": duration,
        "segments": segments,
        "language": language,
        "source": "local_whisper",
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_TIMELINE_BYTES:
        raise ValueError("本地音频时间轴过大。")
    return result


def decode_generated_segments(
    output, tokenizer, duration: float, language_hint: str
) -> dict:
    """Keep generate()'s absolute offsets, including skipped non-vocal windows."""
    batches = output.get("segments") if isinstance(output, dict) else None
    if not isinstance(batches, list) or len(batches) != 1:
        raise ValueError("未获得有效的音频识别段落。")
    chunks = batches[0]
    if not isinstance(chunks, list) or len(chunks) > MAX_SEGMENTS:
        raise ValueError("未获得有效的音频识别段落。")
    segments = []
    for index, item in enumerate(chunks, 1):
        if not isinstance(item, dict) or any(
            key not in item for key in ("start", "end", "tokens")
        ):
            raise ValueError(f"音频识别第{index}段没有返回完整时间戳。")
        text = tokenizer.decode(item["tokens"], skip_special_tokens=True)
        if not isinstance(text, str):
            raise ValueError(f"音频识别第{index}段没有返回有效文本。")
        # Timestamp/EOS-only decoder segments contain no lyric to place on a timeline.
        if not text.strip():
            continue
        try:
            if isinstance(item["start"], bool) or isinstance(item["end"], bool):
                raise ValueError
            start, end = float(item["start"]), float(item["end"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"音频识别第{index}段时间戳不是有效数值。") from exc
        # Whisper timestamps have 20 ms precision; clamp only final rounding noise.
        if duration < end <= duration + 0.02:
            end = duration
        segments.append({"start": start, "end": end, "text": text})
    return validate_timeline(
        {
            "duration": duration,
            "segments": segments,
            "language": language_hint,
            "source": "local_whisper",
        },
        duration,
        language_hint,
    )


def transcribe(source_path: Path, model_path: Path, language_hint: str) -> dict:
    """Use local Whisper segment timestamps, not word alignment or fixed output cuts."""
    if language_hint not in LANGUAGES:
        raise ValueError("识别语言只支持自动、中、英、日、韩。")
    if any(not (model_path / name).is_file() for name in ASR_ASSETS):
        raise ValueError("本地 Whisper 识别资源不完整，请补齐模型配置和 tokenizer。")
    _, duration = inspect_source(source_path)
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import numpy as np
    import torch
    import torchaudio
    from transformers import AutoProcessor, WhisperForConditionalGeneration

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    with wave.open(str(source_path), "rb") as source:
        raw = source.readframes(source.getnframes())
        samples = (
            np.frombuffer(raw, dtype="<i2")
            .reshape(-1, 2)
            .mean(axis=1, dtype=np.float32)
            / 32768.0
        )
    del raw
    audio = torchaudio.functional.resample(
        torch.from_numpy(samples), 44100, 16000
    ).numpy()
    del samples
    processor = AutoProcessor.from_pretrained(str(model_path), local_files_only=True)
    model = (
        WhisperForConditionalGeneration.from_pretrained(
            str(model_path),
            local_files_only=True,
            torch_dtype=torch.float32,
            use_safetensors=True,
        )
        .to("cpu")
        .eval()
    )
    features = processor.feature_extractor(
        audio,
        sampling_rate=16000,
        return_tensors="pt",
        truncation=False,
        padding="longest",
        return_attention_mask=True,
    )
    options = {
        "task": "transcribe",
        "do_sample": False,
        "num_beams": 1,
        "condition_on_prev_tokens": False,
        "temperature": 0.0,
        "no_speech_threshold": 0.6,
        "logprob_threshold": -1.0,
        "compression_ratio_threshold": 2.4,
    }
    if language_hint:
        options["language"] = LANGUAGES[language_hint]
    with torch.inference_mode():
        output = model.generate(
            **features,
            return_timestamps=True,
            return_segments=True,
            **options,
        )
    return decode_generated_segments(
        output, processor.tokenizer, duration, language_hint
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", default="", choices=LANGUAGES)
    args = parser.parse_args()
    try:
        result = transcribe(args.source, args.model, args.language)
        args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except (
        ValueError,
        OSError,
        RuntimeError,
        ImportError,
        TypeError,
        KeyError,
        wave.Error,
    ) as exc:
        print(f"ERROR: 本地音频时间轴生成失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
