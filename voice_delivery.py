"""Normalize completed covers and deliver all PCM as sequential voice messages."""

import asyncio
import math
import subprocess
import wave
from pathlib import Path

from astrbot.core.message.components import Record
from astrbot.core.message.message_event_result import MessageChain


class VoiceSegmentationError(RuntimeError):
    """The complete audio cannot be split safely within the voice duration limit."""


class VoiceDeliveryMixin:
    async def _send_cover_voices(
        self,
        origin: str,
        path: Path,
        pauses: list[float] | None = None,
        phrase_hints: list[float] | None = None,
    ) -> int:
        """Convert a short cover to QQ-compatible WAV chunks and send in order.

        Args:
            origin: Original conversation identifier.
            path: Validated 44.1 kHz stereo cover file.

        Raises:
            RuntimeError: Conversion fails or a voice message cannot be sent.
        """
        from .voice_parts import choose_voice_cuts

        prefix = path.with_suffix("")
        normalized = Path(str(prefix) + "_voice.wav")
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(normalized),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout=60)
            if process.returncode:
                raise RuntimeError(
                    "翻唱语音分段失败：" + error.decode("utf-8", "replace")[-500:]
                )
            parts = []
            with wave.open(str(normalized), "rb") as audio:
                rate = audio.getframerate()
                duration = audio.getnframes() / rate
                hints = [
                    v
                    for v in (phrase_hints or [])
                    if math.isfinite(v) and 0 < v < duration
                ]
                cuts = choose_voice_cuts(duration, pauses or [], hints)
                if any(end - start > 60 for start, end in zip(cuts, cuts[1:])):
                    raise VoiceSegmentationError(
                        "没有足够的自然停顿，无法在不截断唱词的前提下分成每条60秒以内的语音；"
                        "未发送音频。需要完整文件时请明确选择delivery=file。"
                    )
                for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
                    audio.setpos(round(start * rate))
                    frames = audio.readframes(round(end * rate) - round(start * rate))
                    part = Path(str(prefix) + f"_part{index:02d}.wav")
                    with wave.open(str(part), "wb") as output:
                        output.setparams(audio.getparams())
                        output.writeframes(frames)
                    parts.append(part)
            if not parts:
                raise RuntimeError("翻唱语音分段数量异常，请改用文件发送。")
            self._task_update(
                "sending",
                f"正在发送语音：0/{len(parts)}",
                progress=0.0,
                sent_parts=0,
                total_parts=len(parts),
                delivery="voice",
            )
            for index, part in enumerate(parts, 1):
                with wave.open(str(part), "rb") as audio:
                    if (
                        audio.getframerate(),
                        audio.getnchannels(),
                        audio.getsampwidth(),
                    ) != (24000, 1, 2):
                        raise RuntimeError("翻唱语音分段格式无效。")
                sent = await self.context.send_message(
                    origin, MessageChain([Record(file=str(part))])
                )
                if not sent:
                    raise RuntimeError(
                        "找不到发起请求的平台，无法发送翻唱语音；已发送的分段不会重复发送。"
                    )
                self._task_update(
                    "sending",
                    f"已发送语音：{index}/{len(parts)}",
                    progress=index / len(parts),
                    sent_parts=index,
                    total_parts=len(parts),
                    delivery="voice",
                )
            return len(parts)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            normalized.unlink(missing_ok=True)
