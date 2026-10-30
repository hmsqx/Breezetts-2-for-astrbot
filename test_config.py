"""Real module regressions with configuration and audio isolated on disk."""

import asyncio
import importlib
import json
import tempfile
import types
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from test_support import load_module, load_plugin

settings = load_module("settings")
tts = load_module("tts_provider")


class FakeConfig(dict):
    def __init__(self, config_path, schema):
        self.path = Path(config_path)

        def defaults(items):
            return {
                key: defaults(item["items"])
                if item["type"] == "object"
                else item["default"]
                for key, item in items.items()
            }

        super().__init__(defaults(schema))
        if self.path.exists():
            self.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.fail = False

    def save_config(self, changes):
        self.update(changes)
        if self.fail:
            raise OSError("simulated write failure")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self), encoding="utf-8")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        )
        self.root = Path(directory)
        self.plugin_dir = self.root / "plugins/astrbot_plugin_breeze_tts"
        (self.plugin_dir / "data").mkdir(parents=True)
        self.schema = json.loads(
            Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
        )
        (self.plugin_dir / "_conf_schema.json").write_text(
            json.dumps(self.schema), encoding="utf-8"
        )
        self.canonical = self.root / "config/astrbot_plugin_breeze_tts_config.json"
        self.legacy = self.plugin_dir / "data/cmd_config.json"
        self.legacy.write_text(
            json.dumps({"instruction": "legacy", "dashboard": {"token": "excluded"}}),
            encoding="utf-8",
        )
        self.stack.enter_context(
            patch.object(settings, "get_astrbot_data_path", lambda: str(self.root))
        )
        self.stack.enter_context(
            patch.object(settings, "__file__", str(self.plugin_dir / "settings.py"))
        )
        self.stack.enter_context(
            patch.object(tts, "get_astrbot_temp_path", lambda: str(self.root))
        )

    def create_config(self):
        self.canonical.parent.mkdir(exist_ok=True)
        values = dict(FakeConfig(str(self.canonical), self.schema))
        values.update(instruction="dashboard", ref_audio_path="", ref_text="")
        self.canonical.write_text(json.dumps(values), encoding="utf-8")
        return values

    def test_canonical_priority_blank_fields_and_invalid_json(self):
        self.assertEqual(settings._load_plugin_config(), {"instruction": "legacy"})
        values = self.create_config()
        provider = tts.ProviderBreezeTTS(
            {
                "type": "breeze_tts",
                "instruction": "provider",
                "ref_audio_path": "obsolete.wav",
            },
            {},
        )
        self.assertEqual(provider.instruction, "dashboard")
        self.assertEqual(provider.ref_audio_path, "")
        tts._sync_provider_from_config(provider, {"api_base": "  "})
        self.assertEqual(provider.api_base, "http://127.0.0.1:7860")
        self.assertEqual(
            tts.ProviderBreezeTTS({"api_base": "  "}, {}).api_base, values["api_base"]
        )
        self.assertEqual(settings._load_plugin_config(), values)
        self.canonical.write_text("not valid JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            settings._load_plugin_config()

    def test_live_configuration_save_and_rollback(self):
        values = self.create_config()
        live = FakeConfig(str(self.canonical), self.schema)
        changed = {**values, "instruction": "tool"}
        settings._save_plugin_config(changed, live)
        self.assertEqual(live["instruction"], "tool")
        self.assertEqual(settings._load_plugin_config()["instruction"], "tool")
        self.assertEqual(json.loads(self.legacy.read_text())["instruction"], "legacy")
        live.fail = True
        previous = dict(live)
        with self.assertRaises(OSError):
            settings._save_plugin_config(
                {**changed, "instruction": "must not persist"}, live
            )
        self.assertEqual(dict(live), previous)
        self.assertEqual(settings._load_plugin_config()["instruction"], "tool")

    def test_provider_created_before_reload_remains_reusable(self):
        self.create_config()
        provider = tts.ProviderBreezeTTS({"type": "breeze_tts"}, {})
        importlib.reload(tts)
        self.assertNotIsInstance(provider, tts.ProviderBreezeTTS)
        context = types.SimpleNamespace(get_using_tts_provider=lambda: provider)
        self.assertIs(tts._get_breeze_provider(context), provider)

    def test_provider_reads_changed_dashboard_before_each_request(self):
        self.create_config()
        provider = tts.ProviderBreezeTTS({}, {})
        audio_bytes = tts.pcm_to_wav(b"\x01\x00" * 600)

        async def audio(text):
            self.assertEqual(provider.instruction, "changed from dashboard")
            self.assertEqual(provider.ref_audio_path, "")
            return audio_bytes

        provider._request_audio = audio
        settings._save_plugin_config(
            {**settings._load_plugin_config(), "instruction": "changed from dashboard"},
            FakeConfig(str(self.canonical), self.schema),
        )
        path = asyncio.run(provider.get_audio("test"))
        with wave.open(path, "rb") as output:
            self.assertEqual(output.getnframes(), 600)
        self.assertEqual(settings._as_float(float("inf"), 4), 4)
        self.assertEqual(settings._as_float(float("nan"), 4), 4)

    def test_schema_and_atomic_writer_fallback_are_plugin_scoped(self):
        with patch.object(settings, "AstrBotConfig", FakeConfig):
            settings._save_plugin_config(
                {
                    "instruction": "saved without live object",
                    "dashboard": {"token": "excluded"},
                }
            )
        self.assertEqual(
            settings._load_plugin_config()["instruction"], "saved without live object"
        )
        self.assertNotIn("dashboard", json.loads(self.canonical.read_text()))
        self.assertEqual(set(self.schema), set(settings.CONFIG_KEYS))
        self.assertEqual(
            self.schema["cfg_scale"]["slider"], {"min": 1.0, "max": 10.0, "step": 0.1}
        )
        self.assertEqual(self.schema["instruction"]["type"], "text")
        self.assertEqual(self.schema["ref_text"]["type"], "text")
        self.assertEqual(
            settings._load_plugin_config()["singing"],
            {"enabled": True, "api_base": "http://127.0.0.1:7861", "timeout": 1800},
        )

    def test_entry_tools_and_first_deploy_migration_share_settings(self):
        self.create_config()
        live = FakeConfig(str(self.canonical), self.schema)
        provider = tts.ProviderBreezeTTS({"type": "breeze_tts"}, {})
        context = types.SimpleNamespace(
            get_using_tts_provider=lambda: provider,
            get_all_providers=lambda: [provider],
        )
        entry = load_plugin()
        self.stack.enter_context(
            patch.object(entry, "__file__", str(self.plugin_dir / "main.py"))
        )
        plugin = entry.BreezeTTSPlugin.__new__(entry.BreezeTTSPlugin)
        plugin.context, plugin.config = context, live
        reference = self.root / "reference.wav"
        reference.write_bytes(tts.pcm_to_wav(b"\x01\x00" * 600))

        async def exercise():
            clone = await plugin.set_voice_clone_tool(
                None, str(reference), "reference transcript"
            )
            self.assertIn("reference transcript", clone)
            self.assertEqual(
                settings._load_plugin_config()["ref_audio_path"], str(reference)
            )
            self.assertEqual(provider.ref_audio_path, str(reference))
            await plugin.set_voice_design_tool(None, "new design", 99)
            self.assertEqual(settings._load_plugin_config()["ref_audio_path"], "")
            self.assertEqual(provider.cfg_scale, 10)
            before = dict(provider.__dict__)
            live.fail = True
            await plugin.set_voice_design_tool(None, "unsaved", 4)
            self.assertEqual(provider.__dict__, before)
            self.assertEqual(
                settings._load_plugin_config()["instruction"], "new design"
            )
            live.fail = False
            live.first_deploy = True
            await plugin.initialize()
            self.assertEqual(settings._load_plugin_config()["instruction"], "legacy")
            self.assertNotIn("dashboard", settings._load_plugin_config())
            live.first_deploy = False
            settings._save_plugin_config(
                {**settings._load_plugin_config(), "instruction": "existing dashboard"},
                live,
            )
            await plugin.initialize()
            self.assertEqual(
                settings._load_plugin_config()["instruction"], "existing dashboard"
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
