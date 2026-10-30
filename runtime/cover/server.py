"""Local song library and isolated singing voice conversion service."""

import asyncio
import contextlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Annotated

import httpx
import numpy as np
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from public_proxy import PublicProxy, validate_url
from transcribe_song import (
    ASR_ASSETS,
    LANGUAGES,
    MAX_TIMELINE_BYTES,
    TIMELINE_VERSION,
    inspect_source,
    validate_timeline,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SONGS = DATA / "songs"
OUTPUTS = DATA / "covers"
TEMP = ROOT / "tmp"
SEED = ROOT / "seed-vc"
BREEZE = Path(r"D:\Echok\breeze-tts")
BREEZE_URL = "http://127.0.0.1:7860"
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
MAX_SECONDS = 600
MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_PHRASE_BOUNDARIES = 128
SONG_METADATA_FIELDS = (
    "source_url",
    "title",
    "track",
    "artist",
    "album",
    "uploader",
    "source_duration",
    "language",
)
INPUT_FORMATS = {
    ".mp3": "mp3",
    ".m4a": "mov",
    ".mp4": "mov",
    ".aac": "aac",
    ".ogg": "ogg",
    ".opus": "ogg",
    ".wav": "wav",
    ".flac": "flac",
    ".webm": "matroska",
}
DOWNLOAD_LOCK = threading.Lock()
COVER_LOCK = threading.Lock()


class SongUploadBodyLimit:
    """Bound multipart receiving before Starlette spools uploaded file parts."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path", "").rstrip("/") != "/songs/upload"
        ):
            await self.app(scope, receive, send)
            return
        limit = MAX_AUDIO_BYTES + 64 * 1024
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                    if declared < 0:
                        raise ValueError("Negative content length")
                except ValueError:
                    await JSONResponse(
                        {"detail": "上传请求长度无效。"}, status_code=400
                    )(scope, receive, send)
                    return
                if declared > limit:
                    await JSONResponse(
                        {"detail": "上传音频超过 200 MiB 限制。"}, status_code=413
                    )(scope, receive, send)
                    return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # The local multipart parser closes spooled files on BaseException.
                    raise HTTPException(413, "上传请求超过 200 MiB 音频及表单限制。")
            return message

        await self.app(scope, bounded_receive, send)


app = FastAPI(title="Breeze Cover")
app.add_middleware(SongUploadBodyLimit)

for directory in (SONGS, OUTPUTS, TEMP):
    directory.mkdir(parents=True, exist_ok=True)


def remove_directory(path: Path, root: Path, *, ignore_errors: bool = False) -> None:
    target = path.resolve()
    parent = root.resolve()
    if target == parent or not target.is_relative_to(parent):
        raise ValueError("Cleanup target is outside the expected directory")
    shutil.rmtree(target, ignore_errors=ignore_errors)


def worker_env() -> dict[str, str]:
    return {
        **os.environ,
        "TEMP": str(TEMP),
        "TMP": str(TEMP),
        "NUMBA_CACHE_DIR": str(TEMP / "numba"),
        "MPLCONFIGDIR": str(TEMP / "matplotlib"),
        "TORCH_HOME": str(ROOT / "cache" / "torch"),
        "HF_HOME": str(ROOT / "cache" / "huggingface"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUTF8": "1",
    }


@app.get("/health")
def health():
    missing = [
        name
        for name in ("demucs", "yt_dlp", "torch", "torchaudio", "soundfile")
        if importlib.util.find_spec(name) is None
    ]
    if not (SEED / "inference.py").is_file():
        missing.append("Seed-VC")
    if not (
        ROOT / "cache" / "torch" / "hub" / "checkpoints" / "955717e8-8726e21a.th"
    ).is_file():
        missing.append("Demucs model")
    marker = ROOT / "seed-ready.json"
    try:
        manifest = json.loads(marker.read_text(encoding="utf-8"))
        models = manifest["models"]
        if manifest.get("version") != 1 or not isinstance(models, list) or not models:
            raise ValueError("Invalid model manifest")
        for model in models:
            path = (ROOT / model["path"]).resolve()
            if (
                not path.is_relative_to(ROOT)
                or not path.is_file()
                or path.stat().st_size != model["size"]
            ):
                raise ValueError("Missing or incomplete model")
    except (OSError, KeyError, TypeError, ValueError):
        missing.append("Seed-VC models")
    return {"ready": not missing, "error": ", ".join(missing)}


def probe(path: Path, input_format: str = "wav") -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            input_format,
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError("Audio file is invalid or has no duration") from exc
    if result.returncode or not 0 < duration <= MAX_SECONDS:
        raise ValueError(f"Audio must be no longer than {MAX_SECONDS} seconds")
    return duration


def select_song_segment(
    song: Path, target: Path, start_seconds: float, duration_seconds: float
) -> tuple[Path, float, float, str]:
    """Select a PCM excerpt by position or rolling energy, preserving source audio."""
    if (
        not math.isfinite(start_seconds)
        or not math.isfinite(duration_seconds)
        or (start_seconds < 0 and start_seconds != -1)
        or not 0 <= duration_seconds <= MAX_SECONDS
    ):
        raise ValueError("Invalid start or duration")
    with wave.open(str(song), "rb") as source:
        rate = source.getframerate()
        total = source.getnframes()
        if (
            source.getcomptype() != "NONE"
            or source.getsampwidth() != 2
            or source.getnchannels() != 2
            or rate != 44100
            or not 0 < total <= MAX_SECONDS * rate
        ):
            raise ValueError("Song must be a valid normalized PCM WAV")
        if start_seconds >= 0 and start_seconds >= total / rate:
            raise ValueError("Start position is beyond the end of the song")
        if duration_seconds == 0:
            return song, 0.0, total / rate, "full"

        length = min(max(1, round(duration_seconds * rate)), total)
        if start_seconds >= 0:
            start = min(round(start_seconds * rate), total - 1)
            selection = "manual"
        else:
            selection = "energy"
            start = 0
            if length < total:
                energies = []
                for offset in range(0, total, rate):
                    count = min(rate, total - offset)
                    block = source.readframes(count)
                    if len(block) != count * 4:
                        raise ValueError("Song WAV is incomplete")
                    samples = np.frombuffer(block, dtype="<i2").astype(np.float64)
                    energies.append(float(np.mean(samples * samples)))
                window = math.ceil(length / rate)
                last_start = (total - length) // rate
                margin = min(10, last_start // 4)
                candidates = range(margin, last_start - margin + 1)
                cumulative = np.concatenate(([0.0], np.cumsum(energies)))
                best = max(
                    candidates, key=lambda i: cumulative[i + window] - cumulative[i]
                )
                start = best * rate

        length = min(length, total - start)
        source.setpos(start)
        with wave.open(str(target), "wb") as clip:
            clip.setparams(source.getparams())
            remaining = length
            while remaining:
                count = min(rate, remaining)
                block = source.readframes(count)
                if len(block) != count * 4:
                    raise ValueError("Song WAV is incomplete")
                clip.writeframesraw(block)
                remaining -= count
        return target, start / rate, length / rate, selection


def find_phrase_boundaries(vocals: Path, output_duration: float) -> list[float]:
    """Find candidate pauses in converted vocals, relative to the output start.

    A candidate is at least 300 ms of low vocal energy between audible regions.
    These are acoustic cues, not a guarantee of lyric or sentence boundaries.
    """
    import soundfile as sf

    with sf.SoundFile(str(vocals)) as audio:
        rate = audio.samplerate
        if rate <= 0 or audio.frames <= 0 or audio.channels <= 0:
            return []
        frame_size = max(1, round(rate * 0.02))
        frame_seconds = frame_size / rate
        limit = min(audio.frames, round(output_duration * rate), MAX_SECONDS * rate)
        levels = []
        while limit >= frame_size:
            count = min(limit // frame_size, 500) * frame_size
            samples = audio.read(count, dtype="float32", always_2d=True)
            if len(samples) != count or not np.isfinite(samples).all():
                return []
            frames = samples.reshape(-1, frame_size, audio.channels).astype(np.float64)
            levels.extend(np.sqrt(np.mean(frames * frames, axis=(1, 2))).tolist())
            limit -= count
    if not levels:
        return []
    rms = np.asarray(levels)
    reference = float(np.percentile(rms, 90))
    if reference < 0.002:
        return []
    quiet = rms <= max(0.001, reference * 0.1)
    changes = np.diff(np.concatenate(([False], quiet, [False])).astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    candidates = []
    for start, end in zip(starts, ends, strict=True):
        midpoint = (start + end) * frame_seconds / 2
        if (
            start > 0
            and end < len(rms)
            and (end - start) * frame_seconds >= 0.3
            and 1 <= midpoint <= output_duration - 1
        ):
            candidates.append(((end - start) * frame_seconds, round(midpoint, 3)))
    # Keep coverage across long songs before filling the bounded header with strong pauses.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    bucket_seconds = max(5.0, output_duration / MAX_PHRASE_BOUNDARIES)
    buckets = {}
    for candidate in candidates:
        buckets.setdefault(int(candidate[1] // bucket_seconds), candidate[1])
    selected = set(buckets.values())
    for _, midpoint in candidates:
        if len(selected) >= MAX_PHRASE_BOUNDARIES:
            break
        selected.add(midpoint)
    return sorted(selected)


async def run_process(args, *, cwd, request: Request, timeout: int, env=None):
    with tempfile.TemporaryFile(mode="w+b", dir=TEMP) as log:
        process = await asyncio.create_subprocess_exec(
            *map(str, args),
            cwd=str(cwd),
            env=env or worker_env(),
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        started = time.monotonic()
        try:
            while process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    if await request.is_disconnected():
                        raise ConnectionError("Client disconnected")
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("Operation timed out")
            if process.returncode:
                log.seek(0, os.SEEK_END)
                size = log.tell()
                log.seek(max(0, size - 4000))
                detail = log.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Process failed ({process.returncode}): {detail}")
        finally:
            if process.returncode is None:

                def stop_tree():
                    try:
                        parent = psutil.Process(process.pid)
                        children = parent.children(recursive=True)
                    except psutil.NoSuchProcess:
                        return
                    for child in reversed(children):
                        with contextlib.suppress(psutil.NoSuchProcess):
                            child.terminate()
                    with contextlib.suppress(psutil.NoSuchProcess):
                        parent.terminate()
                    _, alive = psutil.wait_procs(children + [parent], timeout=10)
                    for child in alive:
                        with contextlib.suppress(psutil.NoSuchProcess):
                            child.kill()
                    _, alive = psutil.wait_procs(alive, timeout=10)
                    if alive:
                        raise RuntimeError(
                            "Unable to terminate the conversion process tree"
                        )

                cleanup = asyncio.create_task(asyncio.to_thread(stop_tree))
                cancellation = None
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as exc:
                        cancellation = exc
                cleanup.result()
                await process.wait()
                if cancellation is not None:
                    raise cancellation


@app.get("/songs")
def list_songs():
    songs = []
    for info_path in SONGS.glob("*/info.json"):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if (info_path.parent / info["file"]).is_file():
                songs.append(song_public_info(info))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return songs


def song_public_info(info: dict) -> dict:
    """Return public song metadata without exposing internal filenames."""
    public = {
        key: info[key]
        for key in ("id", "name", "duration", *SONG_METADATA_FIELDS)
        if key in info
    }
    source_type = info.get("source_type")
    public["source_type"] = (
        source_type
        if source_type in ("url", "upload", "local")
        else ("url" if info.get("source_url") else "local")
    )
    public["persistent"] = True
    return public


def read_source_metadata(path: Path) -> dict:
    """Read only useful text fields from bounded downloader metadata."""
    try:
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {
            key: " ".join(value.split())[:256]
            for key in ("title", "track", "artist", "album", "uploader")
            if isinstance((value := data.get(key)), str) and value.strip()
        }
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Could not read downloader metadata")
        return {}


async def normalize_song_audio(
    source: Path, target: Path, input_format: str, request: Request
) -> tuple[float, float]:
    """Decode a bounded local audio source, keeping the stored PCM reusable."""
    source_duration = probe(source, input_format)
    await run_process(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            input_format,
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-t",
            str(MAX_SECONDS + 1),
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            target,
        ],
        cwd=ROOT,
        request=request,
        timeout=600,
    )
    duration = probe(target)
    _, pcm_duration = inspect_source(target)
    if abs(duration - pcm_duration) > 0.1:
        raise ValueError("Normalized WAV duration is inconsistent")
    duration = pcm_duration
    return source_duration, duration


def store_song_audio(
    normalized: Path, name: str, duration: float, metadata: dict
) -> dict:
    """Commit a new permanent library entry; never replace an existing song."""
    song_id = uuid.uuid4().hex
    destination = SONGS / song_id
    destination.mkdir()
    try:
        target = destination / "source.wav"
        os.replace(normalized, target)
        info = {
            **metadata,
            "id": song_id,
            "name": name,
            "duration": duration,
            "file": target.name,
        }
        pending = destination / "info.json.tmp"
        pending.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        os.replace(pending, destination / "info.json")
    except BaseException:
        remove_directory(destination, SONGS)
        raise
    return song_public_info(info)


@app.post("/songs")
async def add_song(request: Request):
    if not DOWNLOAD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another download is in progress")
    proxy = None
    temporary = None
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        name = str(data.get("name") or "").strip()
        url = str(data.get("url") or "").strip()
        artist = data.get("artist", "")
        if not name or len(name) > 160:
            raise ValueError("Song name must contain 1-160 characters")
        if (
            not isinstance(artist, str)
            or len(artist) > 160
            or any(ord(c) < 32 for c in artist)
        ):
            raise ValueError("Artist must be text with at most 160 characters")
        artist = artist.strip()
        for song in list_songs():
            if song["name"].casefold() == name.casefold():
                existing_artist = str(song.get("artist") or "").strip()
                if (
                    artist
                    and existing_artist
                    and " ".join(artist.casefold().split())
                    != " ".join(existing_artist.casefold().split())
                ):
                    raise HTTPException(
                        409,
                        "已存在同名但歌手不同的音源，请使用包含歌手的新名称；未覆盖已有音源。",
                    )
                return {**song, "reused": True}
        validate_url(url)
        proxy = PublicProxy()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        temporary = Path(tempfile.mkdtemp(prefix="download-", dir=TEMP))
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        args = [
            PYTHON,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-cookies",
            "--write-info-json",
            "--proxy",
            proxy_url,
            "--downloader",
            "native",
            "--max-filesize",
            str(MAX_AUDIO_BYTES),
            "--match-filter",
            f"duration <=? {MAX_SECONDS}",
            "-f",
            "bestaudio/best",
            "-o",
            str(temporary / "source.%(ext)s"),
            "--",
            url,
        ]
        await run_process(args, cwd=ROOT, request=request, timeout=1200)
        files = [
            p
            for p in temporary.glob("source.*")
            if p.is_file() and p.suffix.lower() in INPUT_FORMATS
        ]
        if len(files) != 1 or files[0].stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("Download did not produce one supported audio file")
        input_format = INPUT_FORMATS.get(files[0].suffix.lower())
        if input_format is None:
            raise ValueError("Downloaded format is not a supported audio file")
        source_metadata = read_source_metadata(temporary / "source.info.json")
        if artist:
            source_metadata["artist"] = artist
        normalized = temporary / "normalized.wav"
        source_duration, duration = await normalize_song_audio(
            files[0], normalized, input_format, request
        )
        return store_song_audio(
            normalized,
            name,
            duration,
            {
                "source_url": url,
                "source_type": "url",
                "source_duration": source_duration,
                **source_metadata,
            },
        )
    except (ValueError, OSError, RuntimeError, TimeoutError, wave.Error) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if proxy:
            proxy.shutdown()
            proxy.server_close()
        if temporary:
            remove_directory(temporary, TEMP, ignore_errors=True)
        DOWNLOAD_LOCK.release()


@app.post("/songs/upload")
async def upload_song(
    request: Request,
    name: Annotated[str, Form()],
    audio: Annotated[UploadFile, File()],
    artist: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "",
):
    """Import a local upload permanently without contacting a media website."""
    if not DOWNLOAD_LOCK.acquire(blocking=False):
        await audio.close()
        raise HTTPException(409, "另一个歌曲收录任务正在进行，请稍后重试。")
    temporary = None
    try:
        name, artist, language = name.strip(), artist.strip(), language.strip().lower()
        if not name or len(name) > 160 or "\0" in name:
            raise ValueError("歌曲名称须为 1–160 个字符。")
        if len(artist) > 160 or "\0" in artist:
            raise ValueError("歌手信息最多 160 个字符。")
        if language not in LANGUAGES:
            raise ValueError("语言只支持自动、中文、英文、日文或韩文。")
        if any(
            str(song.get("name", "")).casefold() == name.casefold()
            for song in list_songs()
        ):
            raise HTTPException(409, "已存在同名歌曲，请修改歌曲名称；未覆盖已有音源。")
        suffix = Path(audio.filename or "").suffix.lower()
        input_format = INPUT_FORMATS.get(suffix)
        if input_format is None:
            raise ValueError(
                "不支持此音频扩展名，请上传 MP3、WAV、M4A、AAC、FLAC、OGG、OPUS、MP4 或 WebM。"
            )
        declared_size = getattr(audio, "size", None)
        if isinstance(declared_size, int) and declared_size > MAX_AUDIO_BYTES:
            raise HTTPException(413, "上传音频超过 200 MiB 限制。")
        temporary = Path(tempfile.mkdtemp(prefix="upload-", dir=TEMP))
        source = temporary / ("upload" + suffix)
        size = 0
        with source.open("wb") as output:
            while chunk := await audio.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_AUDIO_BYTES:
                    raise HTTPException(413, "上传音频超过 200 MiB 限制。")
                output.write(chunk)
        if not size:
            raise ValueError("上传音频为空。")
        normalized = temporary / "normalized.wav"
        source_duration, duration = await normalize_song_audio(
            source, normalized, input_format, request
        )
        metadata = {
            "source_type": "upload",
            "source_duration": source_duration,
            "title": name,
            "language": language,
        }
        if artist:
            metadata["artist"] = artist
        return store_song_audio(normalized, name, duration, metadata)
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError, TimeoutError, wave.Error) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        try:
            await audio.close()
        finally:
            try:
                if temporary:
                    remove_directory(temporary, TEMP, ignore_errors=True)
            finally:
                DOWNLOAD_LOCK.release()


@app.delete("/songs/{song_id}")
async def delete_song(song_id: str, request: Request):
    """Delete only the explicitly selected permanent library entry."""
    if not re.fullmatch(r"[0-9a-f]{32}", song_id):
        raise HTTPException(400, "歌曲 ID 无效。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "删除请求过大。")
        body.extend(chunk)
    try:
        data = json.loads(body) if body else {}
        if not isinstance(data, dict) or set(data) - {"expected_name"}:
            raise ValueError("删除请求格式无效。")
        expected_name = data.get("expected_name")
        if "expected_name" in data and (
            not isinstance(expected_name, str)
            or not expected_name
            or len(expected_name) > 160
        ):
            raise ValueError("待删除歌曲名称无效。")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not DOWNLOAD_LOCK.acquire(blocking=False):
        raise HTTPException(409, "歌曲收录任务正在进行，请完成后再删除。")
    cover_locked = False
    try:
        cover_locked = COVER_LOCK.acquire(blocking=False)
        if not cover_locked:
            raise HTTPException(409, "翻唱或音频识别正在进行，请完成后再删除。")
        if SONGS.is_symlink() or SONGS.is_junction():
            raise ValueError("歌曲库目录为链接，无法安全删除。")
        root = SONGS.resolve()
        entry = SONGS / song_id
        if entry.is_symlink() or entry.is_junction():
            raise ValueError("歌曲目录为链接，无法安全删除。")
        if not entry.is_dir():
            raise HTTPException(404, "没有找到待删除歌曲。")
        folder = entry.resolve()
        if folder == root or folder.parent != root or folder.name != song_id:
            raise ValueError("歌曲目录超出允许范围。")
        identity = folder.stat()
        info_path = folder / "info.json"
        if (
            info_path.is_symlink()
            or info_path.is_junction()
            or not info_path.is_file()
            or info_path.resolve().parent != folder
            or info_path.stat().st_size > 65536
        ):
            raise ValueError("歌曲信息文件无效，无法确认删除目标。")
        info_identity = info_path.stat()
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if (
            not isinstance(info, dict)
            or info.get("id") != song_id
            or info.get("file") != "source.wav"
            or not isinstance(info.get("name"), str)
            or not info["name"]
            or len(info["name"]) > 160
        ):
            raise ValueError("歌曲信息与删除目标不一致。")
        if expected_name is not None and info["name"] != expected_name:
            raise HTTPException(409, "歌曲名称已变化，请刷新列表并重新确认删除。")
        for child in folder.iterdir():
            if (
                child.is_symlink()
                or child.is_junction()
                or not child.is_file()
                or child.resolve().parent != folder
            ):
                raise ValueError("歌曲目录包含异常目录或链接，未执行删除。")
        current = folder.stat()
        current_info = info_path.stat()
        if (
            entry.is_symlink()
            or entry.is_junction()
            or entry.resolve() != folder
            or (current.st_dev, current.st_ino, current.st_ctime_ns)
            != (identity.st_dev, identity.st_ino, identity.st_ctime_ns)
            or (
                current_info.st_size,
                current_info.st_mtime_ns,
                current_info.st_ctime_ns,
            )
            != (
                info_identity.st_size,
                info_identity.st_mtime_ns,
                info_identity.st_ctime_ns,
            )
        ):
            raise HTTPException(409, "歌曲目录在确认期间发生变化，未执行删除。")
        remove_directory(folder, root)
        return {"deleted": True, "id": song_id, "name": info["name"]}
    except HTTPException:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        if cover_locked:
            COVER_LOCK.release()
        DOWNLOAD_LOCK.release()


def timeline_song_source(song_id: str) -> tuple[Path, Path]:
    """Resolve only an actual normalized source inside the requested library entry."""
    if not re.fullmatch(r"[0-9a-f]{32}", song_id):
        raise ValueError("歌曲 ID 无效。")
    root = SONGS.resolve()
    entry = SONGS / song_id
    folder = entry.resolve()
    if entry.is_symlink() or folder.parent != root or not folder.is_dir():
        raise ValueError("没有找到已收录歌曲。")
    info_path = folder / "info.json"
    if (
        info_path.is_symlink()
        or not info_path.is_file()
        or info_path.resolve().parent != folder
        or info_path.stat().st_size > 65536
    ):
        raise ValueError("歌曲信息文件无效。")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if (
        not isinstance(info, dict)
        or info.get("id") != song_id
        or info.get("file") != "source.wav"
    ):
        raise ValueError("歌曲信息或源文件路径无效。")
    source = folder / "source.wav"
    if source.is_symlink() or source.resolve().parent != folder or not source.is_file():
        raise ValueError("歌曲源文件无效。")
    return folder, source


@app.post("/songs/{song_id}/timeline")
async def song_timeline(song_id: str, request: Request):
    """Build CPU timestamps without pausing TTS or sharing memory with AstrBot."""
    if not COVER_LOCK.acquire(blocking=False):
        raise HTTPException(409, "歌曲翻唱或音频识别任务正在进行，请稍后重试。")
    temporary = None
    cache_temporary = None
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise ValueError("音频时间轴请求过大。")
            body.extend(chunk)
        data = json.loads(body)
        if not isinstance(data, dict) or set(data) - {"language"}:
            raise ValueError("音频时间轴请求格式无效。")
        language = data.get("language", "")
        if not isinstance(language, str) or language not in LANGUAGES:
            raise ValueError("识别语言只支持自动、中、英、日、韩。")
        folder, source = timeline_song_source(song_id)
        identity, duration = await asyncio.to_thread(inspect_source, source)
        cache = folder / f"timeline-{language or 'auto'}.json"
        if cache.is_symlink() or cache.resolve().parent != folder:
            raise ValueError("时间轴缓存路径无效。")
        if cache.is_file() and cache.stat().st_size <= MAX_TIMELINE_BYTES:
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    isinstance(saved, dict)
                    and saved.get("version") == TIMELINE_VERSION
                    and saved.get("language_hint") == language
                    and saved.get("source_identity") == identity
                ):
                    return validate_timeline(saved["timeline"], duration, language)
            except (OSError, ValueError, KeyError, TypeError):
                logging.getLogger(__name__).warning(
                    "Ignoring invalid song timeline cache"
                )
        model = ROOT / "weights" / "whisper"
        if any(not (model / name).is_file() for name in ASR_ASSETS):
            raise HTTPException(
                503, "本地 Whisper 识别资源不完整，请补齐模型配置和 tokenizer。"
            )
        temporary = Path(tempfile.mkdtemp(prefix="timeline-", dir=TEMP))
        output = temporary / "timeline.json"
        env = {
            **worker_env(),
            "CUDA_VISIBLE_DEVICES": "",
            "TOKENIZERS_PARALLELISM": "false",
        }
        await run_process(
            [
                PYTHON,
                ROOT / "transcribe_song.py",
                "--source",
                source,
                "--model",
                model,
                "--language",
                language,
                "--output",
                output,
            ],
            cwd=ROOT,
            request=request,
            timeout=900,
            env=env,
        )
        if not output.is_file() or output.stat().st_size > MAX_TIMELINE_BYTES:
            raise ValueError("本地音频时间轴输出缺失或过大。")
        result = validate_timeline(
            json.loads(output.read_text(encoding="utf-8")), duration, language
        )
        if await asyncio.to_thread(inspect_source, source) != (identity, duration):
            raise ValueError("歌曲源文件在识别期间发生变化，未保存时间轴，请重试。")
        saved = {
            "version": TIMELINE_VERSION,
            "language_hint": language,
            "source_identity": identity,
            "timeline": result,
        }
        encoded = json.dumps(saved, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_TIMELINE_BYTES:
            raise ValueError("时间轴缓存过大。")
        cache_temporary = folder / f".timeline-{uuid.uuid4().hex}.tmp"
        cache_temporary.write_bytes(encoded)
        os.replace(cache_temporary, cache)
        return result
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(
            504, "本地歌词识别超过 900 秒，请指定手动时间或使用整首翻唱。"
        ) from exc
    except (ValueError, OSError, KeyError, TypeError, wave.Error) as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        try:
            if cache_temporary is not None:
                cache_temporary.unlink(missing_ok=True)
            if temporary is not None:
                remove_directory(temporary, TEMP, ignore_errors=True)
        finally:
            COVER_LOCK.release()


def listener_pid() -> int | None:
    matches = [
        c.pid
        for c in psutil.net_connections(kind="tcp")
        if c.status == psutil.CONN_LISTEN and c.laddr.port == 7860
    ]
    if len(matches) > 1:
        raise RuntimeError("Multiple processes listen on Breeze port")
    return matches[0] if matches else None


def inspect_breeze() -> dict:
    pid = listener_pid()
    if pid is None:
        raise RuntimeError("Breeze service is not listening")
    process = psutil.Process(pid)
    args = process.cmdline()
    cwd = Path(process.cwd()).resolve()
    if (
        cwd != BREEZE.resolve()
        or "-m" not in args
        or args[args.index("-m") + 1 : args.index("-m") + 2] != ["breeze_infer.api"]
    ):
        raise RuntimeError("Port 7860 does not belong to the expected Breeze service")
    if not Path(process.exe()).name.lower().startswith("python"):
        raise RuntimeError("Unexpected Breeze executable")
    return {
        "pid": pid,
        "created": process.create_time(),
        "args": args,
        "cwd": str(cwd),
        "env": process.environ(),
    }


async def stop_breeze(client: httpx.AsyncClient, snapshot: dict) -> None:
    lease_started = time.monotonic()
    response = await client.post(f"{BREEZE_URL}/internal/cover-drain")
    if response.status_code == 409:
        raise HTTPException(409, "Breeze is currently synthesizing; try again later")
    if response.status_code != 200:
        raise RuntimeError(
            "Breeze is missing the safe drain endpoint; restart it with the updated server"
        )
    lease = response.json()
    token = lease["token"]
    try:
        current = await asyncio.to_thread(inspect_breeze)
        if (
            current["pid"] != snapshot["pid"]
            or current["created"] != snapshot["created"]
            or current["args"] != snapshot["args"]
        ):
            raise RuntimeError("Breeze process changed while acquiring its lease")
        if lease.get("expires_in", 0) < 30 or time.monotonic() - lease_started > 20:
            raise RuntimeError("Breeze drain lease is too close to expiring")
        process = psutil.Process(snapshot["pid"])
        process.terminate()
        snapshot["stop_initiated"] = True
        await asyncio.to_thread(process.wait, 15)
    finally:
        if not snapshot.get("stop_initiated"):
            with contextlib.suppress(Exception):
                await client.post(
                    f"{BREEZE_URL}/internal/cover-drain/release", data={"token": token}
                )


async def restore_breeze(snapshot: dict, client: httpx.AsyncClient) -> None:
    pid = await asyncio.to_thread(listener_pid)
    if pid is not None:
        if pid == snapshot["pid"]:
            process = psutil.Process(pid)
            if process.create_time() == snapshot["created"]:
                try:
                    if (
                        await client.get(f"{BREEZE_URL}/health", timeout=2)
                    ).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
        raise RuntimeError("Port 7860 is already occupied; Breeze was not restarted")
    try:
        old_process = psutil.Process(snapshot["pid"])
        if (
            old_process.create_time() == snapshot["created"]
            and old_process.is_running()
        ):
            raise RuntimeError(
                "Previous Breeze process is still running; refusing a duplicate restart"
            )
    except psutil.NoSuchProcess:
        pass
    log_path = ROOT / "breeze-restored.log"
    with log_path.open("ab") as log:
        process = await asyncio.to_thread(
            subprocess.Popen,
            snapshot["args"],
            cwd=snapshot["cwd"],
            env=snapshot["env"],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    for _ in range(180):
        if process.poll() is not None:
            raise RuntimeError(
                f"Breeze restart exited with code {process.returncode}; see {log_path}"
            )
        try:
            response = await client.get(f"{BREEZE_URL}/health", timeout=1)
            if (
                response.status_code == 200
                and await asyncio.to_thread(listener_pid) == process.pid
            ):
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise RuntimeError(f"Breeze did not become healthy; see {log_path}")


@app.post("/cover")
async def cover(
    request: Request,
    song_id: Annotated[str, Form()],
    ref_audio: Annotated[UploadFile, File()],
    start_seconds: Annotated[float, Form()] = -1,
    duration_seconds: Annotated[float, Form()] = 30,
):
    if not COVER_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another cover is in progress")
    temporary = None
    snapshot = None
    output = OUTPUTS / f"{uuid.uuid4().hex}.wav"
    completed = False
    try:
        temporary = Path(tempfile.mkdtemp(prefix="cover-", dir=TEMP))
        if not re.fullmatch(r"[0-9a-f]{32}", song_id):
            raise ValueError("Invalid song ID")
        folder = SONGS / song_id
        info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
        song = folder / Path(info["file"]).name
        if not song.is_file() or info["id"] != song_id:
            raise ValueError("Song not found")
        try:
            song, selected_start, selected_duration, selection = select_song_segment(
                song, temporary / "segment.wav", start_seconds, duration_seconds
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        reference = temporary / "reference.wav"
        data = await ref_audio.read(MAX_REFERENCE_BYTES + 1)
        if len(data) > MAX_REFERENCE_BYTES:
            raise ValueError("Reference audio is too large")
        reference.write_bytes(data)
        with wave.open(str(reference), "rb") as wav:
            if (
                wav.getcomptype() != "NONE"
                or wav.getnframes() <= 0
                or wav.getframerate() <= 0
            ):
                raise ValueError("Reference must be a nonempty PCM WAV")
            if not 1 <= wav.getnframes() / wav.getframerate() <= 30:
                raise ValueError("Reference audio must be between 1 and 30 seconds")
            if (
                len(wav.readframes(wav.getnframes()))
                != wav.getnframes() * wav.getnchannels() * wav.getsampwidth()
            ):
                raise ValueError("Reference WAV is incomplete")
        if not health()["ready"]:
            raise RuntimeError("Cover models are not ready: " + health()["error"])
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            try:
                snapshot = await asyncio.to_thread(inspect_breeze)
                await stop_breeze(client, snapshot)
                separated = temporary / "separated"
                await run_process(
                    [
                        PYTHON,
                        ROOT / "run_demucs.py",
                        "-n",
                        "htdemucs",
                        "--two-stems",
                        "vocals",
                        "-d",
                        "cuda",
                        "-o",
                        separated,
                        song,
                    ],
                    cwd=ROOT,
                    request=request,
                    timeout=1800,
                )
                stems = separated / "htdemucs" / song.stem
                vocals, backing = stems / "vocals.wav", stems / "no_vocals.wav"
                converted = temporary / "converted"
                converted.mkdir()
                await run_process(
                    [
                        PYTHON,
                        "inference.py",
                        "--source",
                        vocals,
                        "--target",
                        reference,
                        "--output",
                        converted,
                        "--checkpoint",
                        ROOT / "weights" / "svc.pth",
                        "--config",
                        ROOT / "weights" / "svc_config.yml",
                        "--f0-condition",
                        "True",
                        "--auto-f0-adjust",
                        "False",
                        "--diffusion-steps",
                        "30",
                        "--fp16",
                        "True",
                    ],
                    cwd=SEED,
                    request=request,
                    timeout=1800,
                )
                voices = list(converted.glob("*.wav"))
                if len(voices) != 1 or not backing.is_file():
                    raise RuntimeError(
                        "Voice conversion did not produce one vocal track"
                    )
                await run_process(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        voices[0],
                        "-i",
                        backing,
                        "-filter_complex",
                        "[0:a]volume=1[v];[1:a]volume=0.6[b];[v][b]amix=inputs=2:duration=longest:dropout_transition=0:normalize=1",
                        "-ar",
                        "44100",
                        "-ac",
                        "2",
                        "-c:a",
                        "pcm_s16le",
                        output,
                    ],
                    cwd=ROOT,
                    request=request,
                    timeout=600,
                )
                probe(output)
                if output.stat().st_size > MAX_OUTPUT_BYTES:
                    raise RuntimeError("Cover exceeds the maximum output size")
                with wave.open(str(output), "rb") as wav:
                    if (
                        wav.getnchannels(),
                        wav.getsampwidth(),
                        wav.getframerate(),
                        wav.getcomptype(),
                    ) != (2, 2, 44100, "NONE") or len(
                        wav.readframes(wav.getnframes())
                    ) != wav.getnframes() * 4:
                        raise RuntimeError("Generated WAV is incomplete")
                    output_duration = wav.getnframes() / wav.getframerate()
                try:
                    phrase_boundaries = find_phrase_boundaries(
                        voices[0], output_duration
                    )
                except (ImportError, OSError, RuntimeError, ValueError) as exc:
                    logging.getLogger(__name__).warning(
                        "Could not inspect converted vocals for phrase pauses: %s", exc
                    )
                    phrase_boundaries = []
            finally:
                if snapshot is not None and snapshot.get("stop_initiated"):
                    restoration = asyncio.create_task(restore_breeze(snapshot, client))
                    cancellation = None
                    while not restoration.done():
                        try:
                            await asyncio.shield(restoration)
                        except asyncio.CancelledError as exc:
                            cancellation = exc
                    restoration.result()
                    if cancellation is not None:
                        raise cancellation
        completed = True
        return FileResponse(
            output,
            media_type="audio/wav",
            filename="cover.wav",
            headers={
                "X-Cover-Start-Seconds": str(selected_start),
                "X-Cover-Duration-Seconds": str(selected_duration),
                "X-Cover-Selection": selection,
                "X-Cover-Phrase-Boundaries": json.dumps(
                    phrase_boundaries, separators=(",", ":")
                ),
            },
            background=BackgroundTask(output.unlink, missing_ok=True),
        )
    except HTTPException:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        TimeoutError,
        wave.Error,
    ) as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(500, str(exc)) from exc
    finally:
        try:
            if not completed:
                output.unlink(missing_ok=True)
            if temporary is not None:
                remove_directory(temporary, TEMP, ignore_errors=True)
        finally:
            COVER_LOCK.release()
