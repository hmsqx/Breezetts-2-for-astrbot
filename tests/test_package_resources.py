"""Check shipped resources through the relocated installer and settings modules."""

import tempfile
import unittest
from pathlib import Path

from test_support import load_module

installer = load_module("services.installer")
settings = load_module("settings")


class PackagedResourceTests(unittest.TestCase):
    def test_clean_install_finds_both_backends_from_nested_service_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tts, cover = root / "tts", root / "cover"
            for kind in ("tts", "cover"):
                self.assertEqual(installer.prerequisites(kind, tts, cover), [])
            self.assertTrue((installer.BUNDLED / "seed-vc.zip").is_file())

    def test_nested_settings_module_reads_root_schema_without_local_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import patch

            with patch.object(
                settings, "get_astrbot_data_path", return_value=directory
            ):
                self.assertEqual(settings._load_plugin_config(), {})
                defaults = settings._complete_plugin_config({})
                self.assertEqual(defaults["api_base"], "http://127.0.0.1:7860")
                self.assertEqual(
                    defaults["singing"]["api_base"], "http://127.0.0.1:7861"
                )


if __name__ == "__main__":
    unittest.main()
