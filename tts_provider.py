"""Breeze speech provider and PCM/WAV handling; no plugin entry registration."""

import asyncio
import io
import os
import struct
import time
import uuid
import wave
from pathlib import Path

import httpx
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from . import settings

SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2
CHANNELS = 1
MIN_PCM_BYTES = 1000
MAX_COVER_BYTES = 128 * 1024 * 1024
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
DEFAULT_RETRIES = 3
RETRY_BASE_DELAY = 1.0


def pcm_to_wav(pcm: bytes) -> bytes:
    if len(pcm) % SAMPLE_WIDTH:
        pcm = pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH]
    data_size = len(pcm)
    byte_rate = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
    block_align = CHANNELS * SAMPLE_WIDTH
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        byte_rate,
        block_align,
        SAMPLE_WIDTH * 8,
        b"data",
        data_size,
    )
    return header + pcm


def _cleanup_old_files(keep: int = 50, max_age_seconds: float = 86400.0) -> None:
    temp_dir = Path(get_astrbot_temp_path())
    if not temp_dir.is_dir():
        return
    now = time.time()
    try:
        files = sorted(
            temp_dir.glob("breeze_tts_*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for index, path in enumerate(files):
        try:
            if index >= keep and now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _get_breeze_provider(context: Context | None = None) -> "ProviderBreezeTTS | None":
    """获取 Breeze TTS Provider 实例。

    优先从 context 获取当前启用的 TTS Provider；如果未启用或不是 Breeze，
    则从插件配置中创建一个新实例。返回前会同步插件配置到 Provider。
    """
    cfg = settings._load_plugin_config()

    # 1. 尝试从 context 获取
    if context is not None:
        try:
            p = context.get_using_tts_provider()
            if (
                isinstance(p, TTSProvider)
                and p.provider_config.get("type") == "breeze_tts"
            ):
                _sync_provider_from_config(p, cfg)
                return p
        except Exception:
            pass

    # 2. 从插件配置创建独立实例
    try:
        if cfg:
            return ProviderBreezeTTS(cfg, {})
    except Exception as e:
        logger.warning(f"[BreezeTTS] 从配置创建 Provider 失败: {e}")
    return None


def _sync_provider_from_config(tts_provider: "ProviderBreezeTTS", cfg: dict) -> None:
    """将字典配置同步到 Provider 实例"""
    if "api_base" in cfg:
        tts_provider.api_base = (
            str(cfg.get("api_base") or "").strip().rstrip("/")
            or "http://127.0.0.1:7860"
        )
    if cfg.get("instruction") is not None:
        tts_provider.instruction = str(cfg.get("instruction", "")).strip()
    if cfg.get("cfg_scale") is not None:
        tts_provider.cfg_scale = min(
            10.0,
            max(1.0, settings._as_float(cfg.get("cfg_scale"), tts_provider.cfg_scale)),
        )
    if cfg.get("seed") is not None:
        tts_provider.seed = settings._as_int(cfg.get("seed"), tts_provider.seed)
    if cfg.get("timeout") is not None:
        tts_provider.timeout = settings._as_float(
            cfg.get("timeout"), tts_provider.timeout
        )
    if cfg.get("max_retries") is not None:
        tts_provider.max_retries = max(
            1, settings._as_int(cfg.get("max_retries"), tts_provider.max_retries)
        )
    if cfg.get("ref_audio_path") is not None:
        tts_provider.ref_audio_path = str(cfg.get("ref_audio_path", "")).strip()
    if cfg.get("ref_text") is not None:
        tts_provider.ref_text = str(cfg.get("ref_text", "")).strip()
    if cfg.get("clone_use_instruction") is not None:
        tts_provider.clone_use_instruction = bool(cfg.get("clone_use_instruction"))


class ProviderBreezeTTS(TTSProvider):
    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        cfg = {**provider_config, **settings._load_plugin_config()}
        self.api_base = (
            str(cfg.get("api_base") or "").strip().rstrip("/")
            or "http://127.0.0.1:7860"
        )
        self.instruction = str(cfg.get("instruction") or "").strip()
        self.cfg_scale = min(
            10.0, max(1.0, settings._as_float(cfg.get("cfg_scale"), 4.0))
        )
        self.seed = settings._as_int(cfg.get("seed"), 42)
        self.timeout = settings._as_float(cfg.get("timeout"), DEFAULT_TIMEOUT)
        self.max_retries = max(
            1, settings._as_int(cfg.get("max_retries"), DEFAULT_RETRIES)
        )
        self.ref_audio_path = str(cfg.get("ref_audio_path") or "").strip()
        self.ref_text = str(cfg.get("ref_text") or "").strip()
        self.clone_use_instruction = bool(cfg.get("clone_use_instruction", False))
        self.set_model("breeze-tts-2")

    def _build_payload(
        self, text: str
    ) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]] | None]:
        # Breeze 的 cfg_scale 依赖模板是否带 negative prompt：
        #   tts_plain / ref_clone_tata      -> 无 negative prompt，cfg 必须为 1.0
        #   tts_instruction / ref_edit_tata -> 有 negative prompt，cfg 可用
        # 因此只有「会带 instruction」时才启用 CFG，否则强制 1.0，避免服务端 500。
        has_ref = bool(self.ref_audio_path)
        if has_ref and not os.path.isfile(self.ref_audio_path):
            raise ValueError(f"参考音频文件不存在: {self.ref_audio_path}")
        if has_ref and not self.ref_text:
            raise ValueError("音色克隆需要提供参考音频对应的文本")
        if self.ref_text and not has_ref:
            raise ValueError("提供参考文本时必须设置参考音频")
        # 克隆模式下可选择是否把 instruction 当作导演指令
        send_instruction = bool(self.instruction) and (
            not has_ref or self.clone_use_instruction
        )
        use_cfg = self.cfg_scale if send_instruction else 1.0

        data = {
            "text": text,
            "cfg_scale": str(use_cfg),
            "seed": str(self.seed),
            "response_format": "wav",
        }
        if send_instruction:
            data["instruction"] = self.instruction
        if self.ref_text:
            data["ref_text"] = self.ref_text
        files = None
        if has_ref:
            try:
                with open(self.ref_audio_path, "rb") as f:
                    audio_bytes = f.read()
                files = [
                    (
                        "ref_audio",
                        (
                            os.path.basename(self.ref_audio_path),
                            audio_bytes,
                            "audio/wav",
                        ),
                    )
                ]
            except OSError as e:
                raise ValueError(f"读取参考音频失败: {e}") from e
        return data, files

    async def _request_audio(self, text: str) -> bytes:
        last_error = "未知错误"
        data, files = self._build_payload(text)
        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    trust_env=httpx.URL(self.api_base).host
                    not in ("localhost", "127.0.0.1", "::1"),
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{self.api_base}/v1/audio/speech",
                        data=data,
                        files=files,
                    ) as resp:
                        if resp.status_code == 409:
                            last_error = "服务正忙(409)"
                        elif resp.status_code != 200:
                            body = await resp.aread()
                            raise RuntimeError(
                                f"HTTP {resp.status_code}: "
                                f"{body[:200].decode('utf-8', 'ignore')}"
                            )
                        else:
                            chunks: list[bytes] = []
                            async for chunk in resp.aiter_bytes():
                                chunks.append(chunk)
                            audio = b"".join(chunks)
                            content_type = (
                                resp.headers.get("content-type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            if content_type == "audio/pcm":
                                if (
                                    resp.headers.get("x-sample-rate")
                                    != str(SAMPLE_RATE)
                                    or resp.headers.get("x-sample-format", "").lower()
                                    != "s16le"
                                ):
                                    raise RuntimeError("服务返回了不支持的 PCM 格式")
                                if len(audio) % (CHANNELS * SAMPLE_WIDTH):
                                    raise RuntimeError("服务返回的 PCM 音频不完整")
                                if len(audio) >= MIN_PCM_BYTES:
                                    return pcm_to_wav(audio)
                            elif content_type in (
                                "audio/wav",
                                "audio/x-wav",
                                "audio/wave",
                            ):
                                try:
                                    with wave.open(io.BytesIO(audio), "rb") as wav:
                                        if (
                                            wav.getnchannels(),
                                            wav.getsampwidth(),
                                            wav.getframerate(),
                                            wav.getcomptype(),
                                        ) != (
                                            CHANNELS,
                                            SAMPLE_WIDTH,
                                            SAMPLE_RATE,
                                            "NONE",
                                        ):
                                            raise RuntimeError(
                                                "服务返回了不支持的 WAV 格式"
                                            )
                                        frames = wav.getnframes()
                                        pcm = wav.readframes(frames)
                                        if (
                                            len(pcm) != frames * CHANNELS * SAMPLE_WIDTH
                                            or len(audio) < 8
                                            or struct.unpack_from("<I", audio, 4)[0] + 8
                                            != len(audio)
                                        ):
                                            raise RuntimeError(
                                                "服务返回的 WAV 音频不完整"
                                            )
                                        if len(pcm) >= MIN_PCM_BYTES:
                                            return audio
                                except (wave.Error, EOFError) as e:
                                    raise RuntimeError(
                                        f"服务返回了无效的 WAV 音频: {e}"
                                    ) from e
                            else:
                                raise RuntimeError(
                                    f"服务返回了不支持的音频类型: {content_type or '未知'}"
                                )
                            last_error = f"音频过短({len(audio)} bytes)"
            except RuntimeError:
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            if attempt < self.max_retries:
                delay = RETRY_BASE_DELAY * attempt
                logger.warning(
                    f"[BreezeTTS] 第 {attempt} 次合成失败({last_error})，"
                    f"{delay:.0f}s 后重试"
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"Breeze TTS 合成失败: {last_error}")

    async def get_audio(self, text: str) -> str | None:
        if getattr(self, "_breeze_cover_state", {}).get("busy"):
            return None
        content = (text or "").strip()
        if not content:
            raise ValueError("待合成文本为空")

        _sync_provider_from_config(self, settings._load_plugin_config())
        _cleanup_old_files()
        audio = await self._request_audio(content)
        path = os.path.join(
            get_astrbot_temp_path(), f"breeze_tts_{uuid.uuid4().hex}.wav"
        )
        try:
            with open(path, "wb") as f:
                f.write(audio)
        except OSError:
            if os.path.exists(path):
                os.remove(path)
            raise
        return path


def _register_provider() -> None:
    from astrbot.core.provider.register import provider_cls_map

    if "breeze_tts" in provider_cls_map:
        provider_cls_map["breeze_tts"].cls_type = ProviderBreezeTTS
        return
    try:
        register_provider_adapter(
            "breeze_tts",
            "Breeze TTS",
            provider_type=ProviderType.TEXT_TO_SPEECH,
            default_config_tmpl={
                "type": "breeze_tts",
                "enable": True,
                "api_base": "http://127.0.0.1:7860",
                "instruction": "一位二十出头的年轻女性，声音柔和偏低，不尖不亮。语速较慢，句子之间有自然的停顿，吐字清楚。平时说话温和，语气轻缓，带一点慵懒。",
                "cfg_scale": 4.0,
                "seed": 42,
                "timeout": 120,
                "max_retries": 3,
                "ref_audio_path": "",
                "ref_text": "",
                "clone_use_instruction": False,
            },
        )(ProviderBreezeTTS)
    except Exception as e:
        logger.error(f"[BreezeTTS] provider 注册失败: {e}")
