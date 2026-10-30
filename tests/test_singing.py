"""Real tool/module regressions with a simulated backend and local audio fixtures."""

import asyncio
import hashlib
import io
import json
import tempfile
import types
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from test_support import load_module, load_plugin

MAIN = load_plugin()
COVER = load_module("singing.cover")
LIBRARY = load_module("singing.library")
SETTINGS = load_module("settings")
TASK_STATE = load_module("task_state")
TTS = load_module("tts_provider")
VOICE = load_module("singing.voice_delivery")
BACKEND = load_module("singing.backend_client")

from astrbot.core.message.components import File, Plain, Record  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402


def audio(channels=2, rate=44100, frames=2400):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x10\x00" * channels * frames)
    return output.getvalue()


class FakeContext:
    def __init__(self):
        self.sent = []
        self.accept = True
        self.accept_sequence = []
        self.providers = []
        self.selected = None

    def get_all_providers(self):
        return self.providers

    async def get_using_tts_provider_async(self, origin):
        return self.selected

    async def send_message(self, origin, chain):
        self.sent.append((origin, chain))
        return self.accept_sequence.pop(0) if self.accept_sequence else self.accept


class SingingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="singing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ref = self.root / "reference.wav"
        self.ref.write_bytes(audio(1, 24000, 24000))
        self.pcm = audio()
        self.cfg = {
            "ref_audio_path": str(self.ref),
            "singing": {
                "enabled": True,
                "api_base": "http://localhost:7861",
                "timeout": 1800,
            },
        }
        self.blocker, self.learn_blocker = asyncio.Event(), asyncio.Event()
        self.state = {
            "cover": "ok",
            "learn": "ok",
            "calls": [],
            "timeouts": [],
            "cleanups": 0,
        }
        self.voices = []
        self.context = FakeContext()
        self.plugin = MAIN.BreezeTTSPlugin.__new__(MAIN.BreezeTTSPlugin)
        self.plugin.context = self.context
        self.plugin.config = None
        self.plugin._song_task = None
        self.plugin._cover_state = {"busy": False}
        self.event = types.SimpleNamespace(unified_msg_origin="onebot:private:123")
        self.plugin._send_cover_voices = self.fake_voices
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(
            patch.object(SETTINGS, "_load_plugin_config", lambda: self.cfg)
        )
        stack.enter_context(
            patch.object(COVER, "get_astrbot_temp_path", lambda: str(self.root))
        )
        stack.enter_context(patch.object(TTS, "_cleanup_old_files", self.cleanup_count))
        for module in (COVER, LIBRARY, TASK_STATE):
            stack.enter_context(patch.object(module, "logger", Mock()))
        transport = httpx.MockTransport(self.handle)
        state = self.state

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                state["timeouts"].append(kwargs["timeout"])
                super().__init__(transport=transport, **kwargs)

        proxy = types.ModuleType("singing_test_httpx")
        proxy.__dict__.update(vars(httpx))
        proxy.AsyncClient = Client
        for module in (COVER, LIBRARY):
            stack.enter_context(patch.object(module, "httpx", proxy))

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def cleanup_count(self):
        self.state["cleanups"] += 1

    async def handle(self, request):
        self.state["calls"].append((request.method, request.url.path))
        if request.url.path == "/songs" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "song1", "name": "歌名", "duration": 100}]
            )
        if request.url.path == "/songs" and request.method == "POST":
            expected = {"name": "歌名", "url": "https://example.com/song.mp3"}
            if self.state.get("artist"):
                expected["artist"] = self.state["artist"]
            self.assertEqual(json.loads(request.content), expected)
            if self.state["learn"] == "wait":
                await self.learn_blocker.wait()
            return httpx.Response(
                200,
                json={
                    "id": "song1",
                    "name": "歌名",
                    "duration": 10,
                    "reused": self.state.get("reused", False),
                },
            )
        if request.url.path == "/cover":
            self.assertTrue(self.plugin._cover_state["busy"])
            data = await request.aread()
            self.state["multipart"] = data
            self.assertTrue(
                all(value in data for value in (b"song1", b"reference.wav", b"RIFF"))
            )
            if self.state["cover"] == "wait":
                await self.blocker.wait()
            if self.state["cover"] == "busy":
                return httpx.Response(409, json={"detail": "busy"})
            payload = (
                self.pcm[:-3]
                if self.state["cover"] == "corrupt"
                else self.ref.read_bytes()
                if self.state["cover"] == "wrongformat"
                else self.pcm
            )
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": "audio/wav",
                    "x-cover-phrase-boundaries": "[4.5,15.0]",
                    "x-cover-start-seconds": "12",
                },
            )
        return httpx.Response(404)

    async def fake_voices(self, origin, path, pauses, hints):
        self.voices.append((origin, Path(path).read_bytes(), pauses, hints))
        self.plugin._task_update(
            "sending",
            "正在发送语音",
            progress=0,
            sent_parts=0,
            total_parts=2,
            delivery="voice",
        )
        for index in range(1, 3):
            sent = await self.context.send_message(
                origin, MessageChain([Record(file=str(path))])
            )
            if not sent:
                raise RuntimeError(
                    "找不到发起请求的平台，无法发送翻唱语音；已发送的分段不会重复发送。"
                )
            self.plugin._task_update(
                "sending",
                "语音已发送",
                progress=index / 2,
                sent_parts=index,
                total_parts=2,
                delivery="voice",
            )
        return 2

    async def finish(self):
        await self.plugin._song_task
        await asyncio.sleep(0)

    def assert_cover_failed(self, detail):
        status = self.plugin._task_snapshot()
        self.assertEqual(status["state"], "failed")
        self.assertFalse(status["active"])
        self.assertIsNone(status["progress"])
        self.assertIn(detail, status["error"])
        message = self.context.sent[-1][1].chain[0]
        self.assertIsInstance(message, Plain)
        self.assertIn("没能完成", message.text)
        self.assertNotIn(detail, message.text)

    async def test_learn_background_ownership_artist_and_scoped_notification(self):
        self.state["learn"] = "wait"
        self.assertIn(
            "正在收录",
            await self.plugin.learn_song_tool(
                self.event, "歌名", "https://example.com/song.mp3"
            ),
        )
        self.assertEqual(self.plugin._task_snapshot()["kind"], "learn")
        self.assertIn(
            "正在进行",
            await self.plugin.learn_song_tool(
                self.event, "歌名", "https://example.com/song.mp3"
            ),
        )
        self.assertIn("正在进行", await self.plugin.sing_song_tool(self.event, "歌名"))
        await asyncio.sleep(0)
        self.assertEqual(self.state["timeouts"][-1], 1200)
        self.learn_blocker.set()
        await self.finish()
        origin, chain = self.context.sent[-1]
        self.assertEqual(origin, self.event.unified_msg_origin)
        self.assertIn("10.0 秒", chain.chain[0].text)
        self.assertEqual(self.plugin._task_snapshot()["state"], "completed")
        self.state["artist"] = "实际歌手"
        await self.plugin.learn_song_tool(
            self.event, "歌名", "https://example.com/song.mp3", artist=" 实际歌手 "
        )
        await self.finish()
        self.assertEqual(self.plugin._task_snapshot()["state"], "completed")
        self.state["artist"] = ""
        self.assertIn(
            "歌手信息",
            await self.plugin.learn_song_tool(
                self.event, "歌名", "https://example.com/song.mp3", artist="x" * 161
            ),
        )
        self.state["reused"] = True
        await self.plugin.learn_song_tool(
            self.event, "歌名", "https://example.com/song.mp3"
        )
        await self.finish()
        self.assertIn("复用", self.context.sent[-1][1].chain[0].text)

    async def test_learn_timeout_and_cancellation_leave_terminal_state(self):
        self.cfg["singing"]["timeout"] = 0.01
        self.state["learn"] = "wait"
        await self.plugin.learn_song_tool(
            self.event, "歌名", "https://example.com/song.mp3"
        )
        await self.finish()
        self.assertIn("超时", self.context.sent[-1][1].chain[0].text)
        self.assertIn("超时", self.plugin._task_snapshot()["error"])
        self.cfg["singing"]["timeout"] = 1800
        for let_task_start in (False, True):
            self.context.sent.clear()
            await self.plugin.learn_song_tool(
                self.event, "歌名", "https://example.com/song.mp3"
            )
            if let_task_start:
                await asyncio.sleep(0)
            await self.plugin.terminate()
            await asyncio.sleep(0)
            self.assertTrue(self.plugin._song_task.cancelled())
            self.assertFalse(self.context.sent)
            self.assertEqual(self.plugin._task_snapshot()["state"], "cancelled")

    async def test_source_validation_listing_and_disabled_singing(self):
        oversized = self.root / "big.wav"
        with oversized.open("wb") as stream:
            stream.seek(COVER.MAX_REFERENCE_BYTES)
            stream.write(b"x")
        self.cfg["ref_audio_path"] = str(oversized)
        self.assertIn("20 MiB", await self.plugin.sing_song_tool(self.event, "歌名"))
        self.cfg["ref_audio_path"] = str(self.ref)
        listing = await self.plugin.list_learned_songs_tool(self.event)
        self.assertTrue(all(value in listing for value in ("歌名", "100.0 秒", "试听")))
        self.assertIn(
            "还没有收录", await self.plugin.sing_song_tool(self.event, "不存在")
        )
        self.cfg["singing"]["enabled"] = False
        self.assertIn("未启用", await self.plugin.list_learned_songs_tool(self.event))

    async def test_full_song_defaults_to_multiple_voices_and_keeps_full_kind(self):
        self.state["cover"] = "wait"
        self.assertIn("分段语音", await self.plugin.sing_song_tool(self.event, "歌名"))
        self.assertEqual(self.plugin._task_snapshot()["kind"], "full")
        self.assertTrue(self.plugin._cover_state["busy"])
        self.assertIn("正在进行", await self.plugin.sing_song_tool(self.event, "歌名"))
        self.blocker.set()
        await self.finish()
        self.assertEqual(self.state["cleanups"], 1)
        self.assertEqual(len(self.context.sent), 2)
        self.assertTrue(
            all(
                origin == self.event.unified_msg_origin
                and isinstance(chain.chain[0], Record)
                for origin, chain in self.context.sent
            )
        )
        state = self.plugin._task_snapshot()
        self.assertEqual(
            (
                state["state"],
                state["kind"],
                state["delivery"],
                state["sent_parts"],
                state["total_parts"],
            ),
            ("completed", "full", "voice", 2, 2),
        )
        self.assertIn(b'name="duration_seconds"\r\n\r\n0.0', self.state["multipart"])
        self.assertFalse(self.plugin._cover_state["busy"])

    async def test_explicit_file_delivery_is_separate_from_song_range(self):
        self.assertIn(
            "音频文件",
            await self.plugin.sing_song_tool(self.event, "歌名", delivery="file"),
        )
        await self.finish()
        component = self.context.sent[-1][1].chain[0]
        self.assertIsInstance(component, File)
        self.assertEqual(component.name, "cover.wav")
        self.assertEqual(Path(component.file_).read_bytes(), self.pcm)
        self.assertEqual(
            (
                self.plugin._task_snapshot()["kind"],
                self.plugin._task_snapshot()["delivery"],
            ),
            ("full", "file"),
        )
        self.assertFalse(self.voices)
        await self.plugin.sing_song_clip_tool(
            self.event, "歌名", start_seconds=12, end_seconds=40, delivery="file"
        )
        await self.finish()
        self.assertIsInstance(self.context.sent[-1][1].chain[0], File)
        self.assertEqual(
            (
                self.plugin._task_snapshot()["kind"],
                self.plugin._task_snapshot()["delivery"],
            ),
            ("clip", "file"),
        )

    async def test_invalid_delivery_is_rejected_before_backend_requests(self):
        for invalid in ("", "wav", True, None, {"file": True}):
            with self.subTest(delivery=invalid):
                self.assertIn(
                    "发送方式",
                    await self.plugin.sing_song_tool(
                        self.event, "歌名", delivery=invalid
                    ),
                )
                self.assertIn(
                    "发送方式",
                    await self.plugin.sing_song_clip_tool(
                        self.event, "歌名", delivery=invalid
                    ),
                )
        self.assertEqual(self.state["calls"], [])
        self.assertIsNone(self.plugin._song_task)

    async def test_voice_and_file_delivery_refusal_is_not_marked_success(self):
        self.context.accept = False
        for delivery in ("voice", "file"):
            with self.subTest(delivery=delivery):
                await self.plugin.sing_song_tool(self.event, "歌名", delivery=delivery)
                await self.finish()
                self.assert_cover_failed("找不到发起请求的平台")
                self.assertEqual(self.plugin._task_snapshot()["sent_parts"], 0)

    async def test_backend_errors_and_invalid_wav_do_not_emit_media(self):
        for mode, detail in (
            ("busy", "409"),
            ("corrupt", "WAV"),
            ("wrongformat", "格式无效"),
        ):
            with self.subTest(mode=mode):
                self.context.sent.clear()
                self.state["cover"] = mode
                await self.plugin.sing_song_tool(self.event, "歌名")
                await self.finish()
                self.assert_cover_failed(detail)
                self.assertEqual(len(self.context.sent), 1)
                self.assertFalse(self.plugin._cover_state["busy"])
        self.state["cover"] = "ok"
        with patch.object(COVER, "MAX_COVER_BYTES", 100):
            await self.plugin.sing_song_tool(self.event, "歌名")
            await self.finish()
            self.assert_cover_failed("128 MiB")

    async def test_conversion_cancellation_does_not_send_or_leave_busy(self):
        self.state["cover"] = "wait"
        await self.plugin.sing_song_tool(self.event, "歌名")
        await asyncio.sleep(0)
        await self.plugin.terminate()
        await asyncio.sleep(0)
        self.assertTrue(self.plugin._song_task.cancelled())
        self.assertFalse(self.context.sent)
        self.assertEqual(self.plugin._task_snapshot()["state"], "cancelled")
        self.assertFalse(self.plugin._cover_state["busy"])

    async def test_automatic_clip_preserves_plan_scope_hints_and_clip_kind(self):
        planning = asyncio.Event()

        async def plan(origin, song, artist):
            self.assertFalse(self.plugin._cover_state["busy"])
            await planning.wait()
            return {
                "start_seconds": 12,
                "end_seconds": 87,
                "phrase_ends": [20, 40, 87],
                "source_url": "https://lrclib.net/api/get/1",
                "artist": "歌手",
                "track": "歌名",
            }

        self.plugin._plan_chorus = AsyncMock(side_effect=plan)
        self.assertIn(
            "片段翻唱",
            await self.plugin.sing_song_clip_tool(self.event, "歌名", artist="歌手"),
        )
        await asyncio.sleep(0)
        self.assertFalse(self.plugin._cover_state["busy"])
        self.assertIn("正在进行", await self.plugin.sing_song_tool(self.event, "歌名"))
        planning.set()
        await self.finish()
        self.plugin._plan_chorus.assert_awaited_once_with(
            self.event.unified_msg_origin,
            {"id": "song1", "name": "歌名", "duration": 100},
            "歌手",
        )
        self.assertEqual(
            self.voices,
            [(self.event.unified_msg_origin, self.pcm, [4.5, 15.0], [8.0, 28.0, 75.0])],
        )
        state = self.plugin._task_snapshot()
        self.assertEqual((state["kind"], state["delivery"]), ("clip", "voice"))
        self.assertIn("同步歌词", state["detail"])
        self.assertIn(b'name="duration_seconds"\r\n\r\n75.0', self.state["multipart"])

    async def test_manual_clips_do_not_call_planner_and_validate_ranges(self):
        self.plugin._plan_chorus = AsyncMock(
            side_effect=AssertionError("Manual clip must not invoke model")
        )
        for kwargs, start, duration in (
            ({"start_seconds": 12, "duration_seconds": 20}, 12.0, 20.0),
            ({"start_seconds": 13, "end_seconds": 90}, 13.0, 77.0),
        ):
            await self.plugin.sing_song_clip_tool(self.event, "歌名", **kwargs)
            await self.finish()
            self.assertEqual(self.voices[-1][-1], [])
            self.assertIn(
                f'name="start_seconds"\r\n\r\n{start}'.encode(), self.state["multipart"]
            )
            self.assertIn(
                f'name="duration_seconds"\r\n\r\n{duration}'.encode(),
                self.state["multipart"],
            )
        self.plugin._plan_chorus.assert_not_awaited()
        cases = (
            ({"duration_seconds": 61}, "起点"),
            ({"start_seconds": float("nan")}, "有效"),
            ({"start_seconds": 12, "end_seconds": 10}, "晚于"),
            (
                {"start_seconds": 12, "end_seconds": 40, "duration_seconds": 20},
                "不一致",
            ),
            ({"start_seconds": 90, "end_seconds": 110}, "完整"),
        )
        for kwargs, detail in cases:
            self.assertIn(
                detail,
                await self.plugin.sing_song_clip_tool(self.event, "歌名", **kwargs),
            )

    async def test_planning_failure_invalid_output_and_cancellation_do_not_convert(
        self,
    ):
        cases = [
            ValueError("未找到匹配版本"),
            {"start_seconds": 12, "end_seconds": 101},
            {"start_seconds": float("nan"), "end_seconds": 30},
            {"start_seconds": 50, "end_seconds": 50},
        ]
        for result in cases:
            before = self.state["calls"].count(("POST", "/cover"))
            self.plugin._plan_chorus = (
                AsyncMock(side_effect=result)
                if isinstance(result, Exception)
                else AsyncMock(return_value=result)
            )
            await self.plugin.sing_song_clip_tool(self.event, "歌名")
            await self.finish()
            self.assert_cover_failed(
                "未找到匹配版本" if isinstance(result, Exception) else "完整副歌"
            )
            self.assertEqual(self.state["calls"].count(("POST", "/cover")), before)
            self.assertFalse(self.plugin._cover_state["busy"])
        self.context.sent.clear()
        blocked = asyncio.Event()

        async def pending_plan(*args):
            await blocked.wait()

        self.plugin._plan_chorus = pending_plan
        await self.plugin.sing_song_clip_tool(self.event, "歌名")
        await asyncio.sleep(0)
        await self.plugin.terminate()
        await asyncio.sleep(0)
        self.assertTrue(self.plugin._song_task.cancelled())
        self.assertFalse(self.context.sent)
        self.assertFalse(self.plugin._cover_state["busy"])

    async def test_tts_pause_and_result_decorator_keep_scope(self):
        provider = types.SimpleNamespace(provider_config={"type": "breeze_tts"})
        self.context.providers = [provider]
        self.context.selected = provider
        self.plugin._set_cover_busy(True)
        self.assertIs(provider._breeze_cover_state, self.plugin._cover_state)
        with patch.object(
            TTS,
            "_get_breeze_provider",
            side_effect=AssertionError("Paused TTS must not request provider"),
        ):
            self.assertIn("翻唱", await self.plugin.speak_tool(self.event, "test"))
            event = types.SimpleNamespace(plain_result=lambda text: text)
            self.assertIn("翻唱", await self.plugin.breeze_sample(event).__anext__())

        class Result:
            kind = "llm"

            def is_llm_result(self):
                return self.kind == "llm"

            def set_result_content_type(self, kind):
                self.kind = kind

        result = Result()
        self.event.get_result = lambda: result
        await self.plugin.cover_text_reply(self.event)
        self.assertEqual(result.kind, MAIN.ResultContentType.GENERAL_RESULT)
        result.kind = "llm"
        self.context.selected = types.SimpleNamespace(provider_config={"type": "other"})
        await self.plugin.cover_text_reply(self.event)
        self.assertEqual(result.kind, "llm")
        self.plugin._set_cover_busy(False)
        self.context.selected = provider
        await self.plugin.cover_text_reply(self.event)
        self.assertEqual(result.kind, "llm")

    def normalization(self, duration):
        expected = hashlib.sha256()

        async def create(*args, **kwargs):
            self.assertEqual(args[0], "ffmpeg")
            normalized = Path(args[-1])
            with wave.open(str(normalized), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                block = b"\x10\x00" * 24000
                for _ in range(duration):
                    wav.writeframesraw(block)
                    expected.update(block)
            return types.SimpleNamespace(
                returncode=0, communicate=AsyncMock(return_value=(b"", b""))
            )

        return create, expected

    async def test_real_voice_sender_handles_ten_minutes_without_lost_or_repeated_pcm(
        self,
    ):
        del self.plugin._send_cover_voices
        self.plugin._task_begin("歌名", "full")
        create, expected = self.normalization(600)
        with patch.object(VOICE.asyncio, "create_subprocess_exec", create):
            count = await self.plugin._send_cover_voices(
                self.event.unified_msg_origin,
                self.root / "cover.wav",
                list(range(20, 600, 20)),
                [],
            )
        self.assertGreater(count, 1)
        self.assertEqual(count, len(self.context.sent))
        joined, frames = hashlib.sha256(), 0
        for origin, chain in self.context.sent:
            self.assertEqual(origin, self.event.unified_msg_origin)
            self.assertIsInstance(chain.chain[0], Record)
            with wave.open(chain.chain[0].file, "rb") as wav:
                self.assertEqual(
                    (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()),
                    (24000, 1, 2),
                )
                self.assertLessEqual(wav.getnframes() / wav.getframerate(), 60)
                frames += wav.getnframes()
                joined.update(wav.readframes(wav.getnframes()))
        self.assertEqual(frames, 600 * 24000)
        self.assertEqual(joined.hexdigest(), expected.hexdigest())

    async def test_missing_pause_fails_before_any_voice_or_file_is_sent(self):
        del self.plugin._send_cover_voices
        create, _ = self.normalization(61)
        with patch.object(VOICE.asyncio, "create_subprocess_exec", create):
            with self.assertRaisesRegex(RuntimeError, "自然停顿"):
                await self.plugin._send_cover_voices(
                    self.event.unified_msg_origin, self.root / "cover.wav", [], []
                )
        self.assertEqual(self.context.sent, [])
        self.assertFalse((self.root / "cover_voice.wav").exists())

    async def test_voice_sender_stops_after_refused_part_and_keeps_confirmed_count(
        self,
    ):
        del self.plugin._send_cover_voices
        self.plugin._task_begin("歌名", "full")
        self.context.accept_sequence = [True, False]
        create, _ = self.normalization(100)
        with patch.object(VOICE.asyncio, "create_subprocess_exec", create):
            with self.assertRaisesRegex(RuntimeError, "已发送的分段不会重复"):
                await self.plugin._send_cover_voices(
                    self.event.unified_msg_origin, self.root / "cover.wav", [40, 80], []
                )
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual(self.plugin._task_snapshot()["sent_parts"], 1)
        self.assertTrue(
            all(isinstance(chain.chain[0], Record) for _, chain in self.context.sent)
        )

    def test_backend_error_summary_retains_real_error(self):
        detail = (
            "[BiliBili] Downloading webpage\n" * 30
            + "ERROR: [BiliBili] Unable to extract initial state; please report this issue on upstream"
        )
        self.assertEqual(
            BACKEND._backend_error_detail(json.dumps({"detail": detail})),
            "ERROR: [BiliBili] Unable to extract initial state",
        )
        self.assertEqual(
            BACKEND._backend_error_detail("upstream unavailable"),
            "upstream unavailable",
        )


if __name__ == "__main__":
    unittest.main()
