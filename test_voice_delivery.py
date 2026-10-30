"""Real ffmpeg delivery regressions; generated PCM stays in an isolated folder."""

import shutil
import struct
import tempfile
import types
import unittest
import wave
from pathlib import Path

from test_support import load_module

delivery = load_module("voice_delivery")


def write_stereo_source(path, seconds):
    """Use a different low-amplitude marker each second to detect reordering."""
    with wave.open(str(path), "wb") as audio:
        audio.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        for second in range(seconds):
            value = 100 + ((second + 1) * 37) % 2000
            audio.writeframes(struct.pack("<hh", value, value) * 44100)


def read_voice_pcm(path):
    with wave.open(str(path), "rb") as audio:
        if (
            audio.getframerate(),
            audio.getnchannels(),
            audio.getsampwidth(),
            audio.getcomptype(),
        ) != (24000, 1, 2, "NONE"):
            raise AssertionError("Voice must be 24kHz mono 16-bit PCM")
        frames = audio.getnframes()
        pcm = audio.readframes(frames)
        if len(pcm) != frames * 2:
            raise AssertionError("Voice PCM is truncated")
        return pcm, frames


class VoiceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("ffmpeg"), "This regression requires ffmpeg")
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.wav"
        self.normalized = self.root / "source_voice.wav"
        self.parts = []
        self.updates = []
        self.normalized_pcm = None
        self.normalized_frames = None

        async def capture(origin, chain):
            self.assertEqual(origin, "test:local:voices")
            if self.normalized_pcm is None:
                self.assertTrue(self.normalized.is_file())
                self.normalized_pcm, self.normalized_frames = read_voice_pcm(
                    self.normalized
                )
            for component in chain.chain:
                self.assertIsInstance(component, delivery.Record)
                path = Path(component.file)
                self.assertEqual(path.resolve().parent, self.root.resolve())
                pcm, frames = read_voice_pcm(path)
                self.parts.append((pcm, frames))
            return True

        self.sender = delivery.VoiceDeliveryMixin()
        self.sender.context = types.SimpleNamespace(send_message=capture)
        self.sender._task_update = lambda stage, label, **values: self.updates.append(
            (stage, values)
        )

    def assert_all_pcm_preserved(self, seconds):
        self.assertEqual(self.normalized_frames, seconds * 24000)
        self.assertEqual(
            sum(frames for _, frames in self.parts), self.normalized_frames
        )
        self.assertEqual(b"".join(pcm for pcm, _ in self.parts), self.normalized_pcm)
        self.assertTrue(all(0 < frames <= 60 * 24000 for _, frames in self.parts))
        self.assertFalse(self.normalized.exists())
        self.assertEqual(self.updates[-1][1]["progress"], 1)
        self.assertEqual(self.updates[-1][1]["sent_parts"], len(self.parts))

    async def test_short_final_phrase_is_sent_as_voice(self):
        write_stereo_source(self.source, 61)
        count = await self.sender._send_cover_voices(
            "test:local:voices", self.source, [58.0]
        )
        self.assertEqual(count, 2)
        self.assertEqual([frames / 24000 for _, frames in self.parts], [58.0, 3.0])
        self.assert_all_pcm_preserved(61)

    async def test_no_safe_pause_fails_before_any_voice_or_file_is_sent(self):
        write_stereo_source(self.source, 61)
        with self.assertRaisesRegex(delivery.VoiceSegmentationError, "未发送音频"):
            await self.sender._send_cover_voices("test:local:voices", self.source, [])
        self.assertEqual(self.parts, [])
        self.assertEqual(self.updates, [])
        self.assertFalse(self.normalized.exists())
        self.assertEqual(list(self.root.glob("source_part*.wav")), [])

    async def test_full_song_is_all_voices_without_missing_or_repeated_pcm(self):
        write_stereo_source(self.source, 258)
        count = await self.sender._send_cover_voices(
            "test:local:voices", self.source, list(range(5, 258, 5))
        )
        self.assertGreater(count, 1)
        self.assertEqual(count, len(self.parts))
        self.assert_all_pcm_preserved(258)


if __name__ == "__main__":
    unittest.main()
