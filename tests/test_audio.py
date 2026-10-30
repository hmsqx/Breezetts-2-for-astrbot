"""Exercise the real speech provider with an in-memory HTTP transport."""

import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from test_support import load_module

settings = load_module("settings")
tts = load_module("tts_provider")


class SpeechAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pcm = b"\x01\x00" * 600
        self.wav = tts.pcm_to_wav(self.pcm)
        self.response = httpx.Response(
            200, content=self.wav, headers={"content-type": "audio/wav"}
        )
        self.calls = []

        async def handle(request):
            self.calls.append(request)
            return self.response

        transport = httpx.MockTransport(handle)

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                assert kwargs["trust_env"] is False
                super().__init__(transport=transport, **kwargs)

        for target, name, value in (
            (settings, "_load_plugin_config", lambda: {}),
            (tts, "_cleanup_old_files", lambda: None),
            (tts, "get_astrbot_temp_path", lambda: str(self.root)),
            (tts, "httpx", types.SimpleNamespace(AsyncClient=Client, URL=httpx.URL)),
        ):
            mock = patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.provider = tts.ProviderBreezeTTS(
            {"api_base": "http://localhost", "timeout": 2, "max_retries": 1}, {}
        )

    async def test_busy_provider_does_not_make_request(self):
        self.provider._breeze_cover_state = {"busy": True}
        self.assertIsNone(await self.provider.get_audio("must not request"))
        self.assertEqual(self.calls, [])

    async def test_wav_and_legacy_pcm_responses_remain_complete(self):
        for payload, kind in ((self.wav, "audio/wav"), (self.pcm, "audio/pcm")):
            self.response = httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": kind,
                    "x-sample-rate": "24000",
                    "x-sample-format": "s16le",
                },
            )
            path = await self.provider.get_audio("test")
            with wave.open(path, "rb") as audio:
                self.assertEqual(
                    (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                    (24000, 1, 2),
                )
                self.assertEqual(audio.readframes(audio.getnframes()), self.pcm)
            self.assertEqual(
                parse_qs(self.calls[-1].content.decode())["response_format"], ["wav"]
            )

    async def test_invalid_responses_are_rejected_without_saving_audio(self):
        for payload, kind in (
            (b'{"message":"bad"}' * 100, "application/json"),
            (self.wav[:-2], "audio/wav"),
            (self.pcm + b"\0", "audio/pcm"),
        ):
            self.response = httpx.Response(
                200,
                content=payload,
                headers={
                    "content-type": kind,
                    "x-sample-rate": "24000",
                    "x-sample-format": "s16le",
                },
            )
            with self.subTest(content_type=kind), self.assertRaises(RuntimeError):
                await self.provider.get_audio("test")
        self.assertEqual(list(self.root.glob("*.wav")), [])

    async def test_unsupported_pcm_rate_is_rejected(self):
        self.response = httpx.Response(
            200,
            content=self.pcm,
            headers={
                "content-type": "audio/pcm",
                "x-sample-rate": "48000",
                "x-sample-format": "s16le",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "PCM"):
            await self.provider.get_audio("test")

    async def test_reference_validation_and_clone_instruction_cfg(self):
        self.provider.ref_audio_path = str(self.root / "missing.wav")
        self.provider.ref_text = "reference"
        with self.assertRaises(ValueError):
            self.provider._build_payload("test")
        reference = self.root / "reference.wav"
        reference.write_bytes(self.wav)
        self.provider.ref_audio_path = str(reference)
        self.provider.ref_text = ""
        with self.assertRaises(ValueError):
            self.provider._build_payload("test")
        self.provider.ref_text = "reference"
        self.provider.instruction = "gentle"
        self.provider.clone_use_instruction = False
        data, files = self.provider._build_payload("test")
        self.assertEqual(data["cfg_scale"], "1.0")
        self.assertTrue(files)
        self.assertNotIn("instruction", data)
        self.provider.clone_use_instruction = True
        data, files = self.provider._build_payload("test")
        self.assertEqual(float(data["cfg_scale"]), 4)
        self.assertEqual(data["instruction"], "gentle")


if __name__ == "__main__":
    unittest.main()
