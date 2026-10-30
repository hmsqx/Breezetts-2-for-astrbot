"""Song selection, conversion and delivery orchestration; framework tools live in main."""

import asyncio
import json
import math
import struct
import uuid
import wave
from pathlib import Path

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .. import settings, tts_provider
from ..tts_provider import MAX_COVER_BYTES, MAX_REFERENCE_BYTES
from .backend_client import _backend_error_detail
from .voice_delivery import VoiceDeliveryMixin, VoiceSegmentationError


class CoverMixin(VoiceDeliveryMixin):
    async def _sing_song(
        self, event: AstrMessageEvent, name: str, delivery: str = "voice"
    ) -> str:
        """用当前参考音色翻唱整首已收录歌曲，默认按自然停顿连续发送多条语音。

        用户明确要整首或完整翻唱时调用。只想唱一段、高潮或指定片段时用 sing_song_clip。
        未收录时先搜索并使用 learn_song，不能把试听片段称为完整歌曲。

        Args:
            name(string): 已收录歌曲的完整名称。
            delivery(string): voice按停顿连续发送语音（默认）；仅用户明确要文件时用file。

        Returns:
            整首翻唱任务状态。
        """
        return await self._start_cover(event, name, -1.0, 0.0, delivery)

    async def _sing_song_clip(
        self,
        event: AstrMessageEvent,
        name: str,
        start_seconds: float = -1.0,
        duration_seconds: float = 0.0,
        end_seconds: float = -1.0,
        artist: str = "",
        delivery: str = "voice",
    ) -> str:
        """联网核对歌词或识别原音频时间轴，让模型选副歌候选并按停顿发送。

        默认不要填写固定时长或猜测时间，后台会读取同步歌词、调用当前模型判断完整副歌范围。
        找不到匹配版本时会临时在CPU识别当前音频的时间轴；识别不足时明确失败，不随机截取30秒。
        工具返回正在处理只表示任务启动，不能提前告诉用户已能翻唱或已经成功。
        用户明确指定时间时才填写起点和终点或时长；语音只在人声停顿处分段，不按15秒硬切。
        不会自动改发文件；缺少安全停顿时明确提示，只有用户明确要文件才用delivery=file。
        用户要整首时应调用 sing_song，未收录时先搜索并使用 learn_song。
        对话中已确认实际歌手时务必填写artist，包括随后只说“唱吧”的情况；不能只按同名曲目匹配。

        Args:
            name(string): 已收录歌曲的完整名称。
            start_seconds(float): 用户指定的原曲起点；默认-1由模型分析，不可猜测。
            duration_seconds(float): 用户指定的片段时长；默认0不限制副歌长度，手动范围最长600秒。
            end_seconds(float): 用户指定的原曲终点；默认-1。起点和终点决定完整片段。
            artist(string): 对话中已确认的实际歌手或录音版本；已知时填写，未知留空。
            delivery(string): voice按停顿连续发送语音（默认）；用户明确要文件时用file。

        Returns:
            片段翻唱任务状态。
        """
        try:
            start_seconds = float(start_seconds)
            duration_seconds = float(duration_seconds)
            end_seconds = float(end_seconds)
            if not all(
                math.isfinite(v) for v in (start_seconds, duration_seconds, end_seconds)
            ):
                return "片段时间必须是有效秒数。"
            automatic = (
                start_seconds == -1 and duration_seconds == 0 and end_seconds == -1
            )
            if not automatic:
                if start_seconds < 0:
                    return "请给出片段起点和终点；自动高潮请保留默认时间，由模型判断。"
                if end_seconds >= 0:
                    if end_seconds <= start_seconds:
                        return "片段终点必须晚于起点。"
                    if (
                        duration_seconds
                        and abs(duration_seconds - (end_seconds - start_seconds)) > 0.05
                    ):
                        return "指定的终点和时长不一致，请只提供一种。"
                    duration_seconds = end_seconds - start_seconds
                if not 0 < duration_seconds <= 600:
                    return "指定片段需要有效的起止范围，最长为已收录歌曲的完整长度。"
        except (TypeError, ValueError):
            return "片段时间必须是有效秒数。"
        return await self._start_cover(
            event,
            name,
            start_seconds,
            duration_seconds,
            delivery,
            auto_chorus=automatic,
            artist=(artist or "").strip(),
        )

    async def _start_cover(
        self,
        event: AstrMessageEvent,
        name: str,
        start_seconds: float = -1.0,
        duration_seconds: float = 0.0,
        delivery: str = "voice",
        *,
        auto_chorus: bool = False,
        artist: str = "",
    ) -> str:
        """Start one full cover, explicit excerpt, or lyric-planned chorus task.

        Args:
            event: Originating message event.
            name: Stored song name.
            start_seconds: Explicit source offset, or -1 during model planning.
            duration_seconds: Explicit duration, or zero for full/planned mode.
            delivery: Explicit voice or file delivery, independent of song range.
            auto_chorus: Fetch synced lyrics and invoke the session model first.
            artist: Optional artist/version hint for synced lyric lookup.

        Returns:
            Task start status or a validation error.
        """
        if self._song_task and not self._song_task.done():
            return "已有歌曲任务正在进行，请等它完成。"
        name = (name or "").strip()
        if not name:
            return "请提供已收录歌曲的名称。"
        try:
            start_seconds = float(start_seconds)
            duration_seconds = float(duration_seconds)
            if (
                not math.isfinite(start_seconds)
                or not math.isfinite(duration_seconds)
                or (start_seconds < 0 and start_seconds != -1)
                or not (duration_seconds == 0 or 1 <= duration_seconds <= 600)
            ):
                return "片段起点须为-1或非负秒数，时长须为1至600秒，0表示整首。"
            if delivery not in ("voice", "file"):
                return "发送方式须为voice（分条语音）或file（音频文件）。"
            task_kind = "full" if not auto_chorus and duration_seconds == 0 else "clip"
            api_base, timeout = self._singing_settings()
            cfg = settings._load_plugin_config()
            ref_path = Path(str(cfg.get("ref_audio_path") or "").strip())
            if not ref_path.is_file():
                return "当前没有可用的参考音频，请先在插件配置中设置参考音频路径。"
            if ref_path.stat().st_size > MAX_REFERENCE_BYTES:
                return "参考音频超过 20 MiB 限制，请换用较短的参考音频。"
            with ref_path.open("rb") as stream:
                ref_audio = stream.read(MAX_REFERENCE_BYTES + 1)
            if len(ref_audio) > MAX_REFERENCE_BYTES:
                return "参考音频超过 20 MiB 限制，请换用较短的参考音频。"
            async with httpx.AsyncClient(
                timeout=min(timeout, 15.0), trust_env=False
            ) as client:
                response = await client.get(f"{api_base}/songs")
            response.raise_for_status()
            songs = response.json()
            if not isinstance(songs, list):
                raise ValueError("翻唱服务返回了无效的歌曲列表")
            matches = [
                song
                for song in songs
                if isinstance(song, dict)
                and str(song.get("name", "")).strip().casefold() == name.casefold()
                and song.get("id")
            ]
            if not matches:
                return f"还没有收录《{name}》，请先搜索音频并收录。"
            if len(matches) != 1:
                return f"有多首歌叫《{name}》，请为歌曲使用不同名称。"
            if not auto_chorus and duration_seconds > 0:
                available = float(matches[0].get("duration", 0))
                if start_seconds + duration_seconds > available + 0.05:
                    return "当前收录音频不包含完整的指定范围，请核对录音版本或先收录完整音源。"
            if self._song_task and not self._song_task.done():
                return "已有歌曲任务正在进行，请等它完成。"
            tts_provider._cleanup_old_files()
            origin = event.unified_msg_origin
            self._set_cover_busy(not auto_chorus)
            self._task_begin(name, task_kind)
            self._song_task = asyncio.create_task(
                self._perform_cover(
                    origin,
                    name,
                    str(matches[0]["id"]),
                    ref_path.name,
                    ref_audio,
                    api_base,
                    timeout,
                    start_seconds,
                    duration_seconds,
                    delivery,
                    song=matches[0],
                    auto_chorus=auto_chorus,
                    artist=artist,
                    task_kind=task_kind,
                )
            )
            self._song_task.add_done_callback(self._song_task_done)
            delivery_label = "音频文件" if delivery == "file" else "分段语音"
            if auto_chorus:
                return f"正在准备《{name}》的片段翻唱，完成后会发送{delivery_label}。任务尚未完成，只需简短自然回应，不要向用户播报内部处理流程或提前声称成功。"
            return f"正在翻唱《{name}》，完成后会发送{delivery_label}。处理期间请先用文字回复，普通语音会在完成后恢复。"
        except httpx.HTTPStatusError as e:
            return f"翻唱准备失败（HTTP {e.response.status_code}）：{e.response.text[:300]}"
        except Exception as e:
            return f"翻唱准备失败：{e}"

    async def _plan_chorus(self, origin: str, song: dict, artist: str) -> dict:
        """Select complete lyric lines using public lyrics and the session model.

        Args:
            origin: Originating conversation, used only to select its model.
            song: Source audio metadata.
            artist: Optional artist/version hint.

        Returns:
            A locally validated lyric-based excerpt plan.
        """
        from .chorus_planner import MAX_RESPONSE_BYTES, plan_chorus

        provider = await self.context.get_using_provider_async(origin)
        if provider is None:
            raise ValueError("当前会话没有可用的模型，无法判断完整高潮。")

        async def generate(prompt: str, system_prompt: str) -> str:
            response = await provider.text_chat(
                prompt=prompt, system_prompt=system_prompt, contexts=[], func_tool=None
            )
            return response.completion_text

        api_base, timeout = self._singing_settings()

        async def report(stage: str, detail: str) -> None:
            labels = {
                "searching": "正在查询匹配歌词",
                "local_timeline": "正在识别当前音源",
                "model": "正在选择完整副歌",
            }
            updates = (
                {"fallback_reason": detail[:1000]} if stage == "local_timeline" else {}
            )
            self._task_update(
                stage, labels.get(stage, "正在分析歌曲"), detail, **updates
            )

        async def transcribe(language: str) -> dict:
            song_id = str(song.get("id") or "")
            if len(song_id) != 32 or any(
                char not in "0123456789abcdef" for char in song_id
            ):
                raise ValueError("歌曲ID无效，请重新收录音源。")
            logger.info(
                "[BreezeTTS] Synced lyrics unavailable; requesting source audio timeline"
            )
            self._task_update(
                "local_timeline",
                "正在识别当前音源",
                self._task_snapshot()["detail"]
                + "；使用临时CPU识别，结束后释放内存，不暂停普通TTS。",
            )
            async with httpx.AsyncClient(
                timeout=min(timeout, 900.0), trust_env=False
            ) as client:
                async with client.stream(
                    "POST",
                    f"{api_base}/songs/{song_id}/timeline",
                    json={"language": language},
                ) as response:
                    if response.status_code == 404:
                        raise ValueError(
                            "翻唱后台尚未加载音频识别接口，或歌曲已移除。请在插件服务页关闭并重新启动翻唱服务后重试。"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("本地歌词时间轴过大，已停止分析。")
                        data.extend(chunk)
                    if response.status_code != 200:
                        detail = _backend_error_detail(
                            bytes(data).decode("utf-8", "replace")
                        )
                        raise ValueError(
                            f"本地音频识别失败（HTTP {response.status_code}）：{detail}"
                        )
            try:
                return json.loads(data)
            except ValueError as exc:
                raise ValueError("本地音频识别返回了无效时间轴。") from exc

        try:
            return await asyncio.wait_for(
                plan_chorus(
                    song, artist, generate, transcribe=transcribe, report=report
                ),
                timeout=min(timeout, 1100.0),
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ValueError(
                "歌词查询、音频识别或模型选段超时，请稍后重试或手动指定范围。"
            ) from exc

    async def _perform_cover(
        self,
        origin: str,
        name: str,
        song_id: str,
        ref_name: str,
        ref_audio: bytes,
        api_base: str,
        timeout: float,
        start_seconds: float,
        duration_seconds: float,
        delivery: str,
        *,
        song: dict | None = None,
        auto_chorus: bool = False,
        artist: str = "",
        task_kind: str = "clip",
    ) -> None:
        """Run one cover request and report only to its originating conversation.

        Args:
            origin: Original conversation identifier.
            name: Song title used for status messages.
            song_id: Backend song identifier.
            ref_name: Reference audio filename.
            ref_audio: Reference audio bytes captured when the task starts.
            api_base: Cover service base URL.
            timeout: Per-operation request timeout in seconds.
            start_seconds: Requested source offset, or -1 for an automatic excerpt.
            duration_seconds: Requested excerpt length, or zero for the full song.
            delivery: Explicit voice/file choice; never changes the selected range.
            task_kind: Full song or clip, independent of the delivery format.
        """
        if not self._task_snapshot()["active"]:
            self._task_begin(name, task_kind)
        as_file = delivery == "file"
        path = Path(get_astrbot_temp_path()) / f"breeze_tts_{uuid.uuid4().hex}.wav"
        complete = False
        phrase_hints = []
        pauses = []
        try:
            if auto_chorus:
                plan = await self._plan_chorus(origin, song or {}, artist)
                start_seconds = float(plan["start_seconds"])
                end_seconds = float(plan["end_seconds"])
                duration_seconds = end_seconds - start_seconds
                if (
                    not 0
                    <= start_seconds
                    < end_seconds
                    <= float((song or {})["duration"])
                ):
                    raise ValueError("模型选定的完整副歌不在当前音源范围内。")
                phrase_hints = plan.get("phrase_ends", [])
                timeline_label = (
                    "当前音源的本地识别时间轴"
                    if plan.get("timeline_source") == "local_whisper"
                    else "同步歌词"
                )
                self._task_update(
                    "selected",
                    "副歌范围已选定",
                    f"{timeline_label}：{start_seconds:.1f}–{end_seconds:.1f} 秒；"
                    f"版本：{plan['artist']} / {plan['track']}；来源：{plan['source_url']}。时间轴与选段可能有误。"
                    + (
                        f" 回退原因：{self._task_snapshot()['fallback_reason']}"
                        if self._task_snapshot().get("fallback_reason")
                        else ""
                    ),
                )
            self._set_cover_busy(True)
            selection_detail = self._task_snapshot()["detail"]
            self._task_update(
                "converting",
                "正在生成翻唱音频",
                selection_detail
                + " 后台正在分离人声、转换声线、混音并恢复普通TTS；暂无法测量整体百分比。",
            )
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{api_base}/cover",
                    data={
                        "song_id": song_id,
                        "start_seconds": str(start_seconds),
                        "duration_seconds": str(duration_seconds),
                    },
                    files={"ref_audio": (ref_name, ref_audio, "audio/wav")},
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        raise RuntimeError(
                            f"HTTP {response.status_code}: {_backend_error_detail(body.decode('utf-8', 'replace'))}"
                        )
                    if (
                        not as_file
                        and "x-cover-phrase-boundaries" not in response.headers
                    ):
                        raise RuntimeError(
                            "翻唱服务尚未加载自然断句接口，请更新后端后重试。"
                        )
                    if not as_file:
                        pauses = json.loads(
                            response.headers["x-cover-phrase-boundaries"]
                        )
                        actual_start = float(response.headers["x-cover-start-seconds"])
                        phrase_hints = [float(v) - actual_start for v in phrase_hints]
                    if response.headers.get("content-type", "").split(";", 1)[
                        0
                    ].strip().lower() not in ("audio/wav", "audio/x-wav", "audio/wave"):
                        raise RuntimeError("翻唱服务未返回 WAV 音频")
                    size = 0
                    with path.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_COVER_BYTES:
                                raise RuntimeError("翻唱音频超过 128 MiB 限制")
                            stream.write(chunk)
            self._task_update("checking", "正在检查生成的音频", selection_detail)
            with path.open("rb") as stream:
                header = stream.read(12)
                if (
                    len(header) != 12
                    or header[:4] != b"RIFF"
                    or header[8:] != b"WAVE"
                    or struct.unpack_from("<I", header, 4)[0] + 8 != path.stat().st_size
                ):
                    raise RuntimeError("翻唱服务返回的 WAV 不完整")
            with wave.open(str(path), "rb") as wav:
                if (
                    wav.getcomptype() != "NONE"
                    or wav.getnframes() <= 0
                    or wav.getnchannels() != 2
                    or wav.getsampwidth() != 2
                    or wav.getframerate() != 44100
                ):
                    raise RuntimeError(
                        "翻唱服务返回的 WAV 格式无效（需要 44.1 kHz、16 位双声道 PCM）"
                    )
                frames = wav.getnframes()
                width = wav.getsampwidth() * wav.getnchannels()
                if len(wav.readframes(frames)) != frames * width:
                    raise RuntimeError("翻唱服务返回的 WAV 不完整")
            if as_file:
                self._task_update(
                    "sending_file",
                    "正在发送音频文件",
                    progress=0.0,
                    total_parts=1,
                    sent_parts=0,
                    delivery="file",
                )
                sent = await self.context.send_message(
                    origin, MessageChain([File(name="cover.wav", file=str(path))])
                )
                if not sent:
                    raise RuntimeError("找不到发起请求的平台，无法发送翻唱文件")
                self._task_update(
                    "sending_file",
                    "音频文件已发送",
                    progress=1.0,
                    total_parts=1,
                    sent_parts=1,
                    delivery="file",
                )
            else:
                self._task_update("splitting", "正在准备语音分段", selection_detail)
                count = await self._send_cover_voices(
                    origin, path, pauses, phrase_hints
                )
                if type(count) is not int or count < 1:
                    raise RuntimeError("没有确认成功发送的翻唱音频。")
            complete = True
            state = self._task_snapshot()
            label = (
                "已完成：音频文件已发送"
                if state.get("delivery") == "file"
                else f"已完成：{state['sent_parts']}条语音已发送"
            )
            self._task_finish("completed", label, selection_detail)
        except asyncio.CancelledError:
            self._task_finish("cancelled", "翻唱已取消", "已发送的音频不会重复发送。")
            raise
        except Exception as e:
            logger.error(f"[BreezeTTS] Cover failed: {e}")
            self._task_finish("failed", "翻唱未完成", str(e), error=e)
            failure_message = (
                f"《{name}》没有足够的自然停顿，无法在不切断唱词的情况下分条发送。"
                "如果需要完整文件，可以明确告诉我。"
                if isinstance(e, VoiceSegmentationError)
                else f"这次没能完成《{name}》的翻唱，请稍后再试。"
            )
            try:
                await self.context.send_message(
                    origin,
                    MessageChain([Plain(failure_message)]),
                )
            except Exception as send_error:
                logger.error(f"[BreezeTTS] Failed to report cover error: {send_error}")
        finally:
            if not complete:
                path.unlink(missing_ok=True)
            self._set_cover_busy(False)
