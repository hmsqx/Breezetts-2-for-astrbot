"""Pure configuration API validation cases; never read or write user settings."""

import copy
import unittest

from test_support import load_module

settings = load_module("settings")
defaults = settings._plugin_config_defaults
complete = settings._complete_plugin_config
validate = settings._validate_plugin_config
revision = settings._config_revision


class ConfigFormTests(unittest.TestCase):
    def test_defaults_and_revision_are_canonical_without_rewriting_input(self):
        original = {"cfg_scale": 4, "instruction": "  keep formatting\n"}
        snapshot = copy.deepcopy(original)
        config = complete(original)
        self.assertEqual(original, snapshot)
        self.assertEqual(config["instruction"], original["instruction"])
        self.assertEqual(config["singing"]["timeout"], defaults()["singing"]["timeout"])
        self.assertEqual(
            revision(config), revision(dict(reversed(list(config.items()))))
        )
        self.assertEqual(
            revision(config), revision(complete({**original, "cfg_scale": 4.0}))
        )

    def test_existing_invalid_values_stay_readable_for_repair(self):
        result = complete(
            {"seed": "bad", "timeout": float("nan"), "singing": {"enabled": "wrong"}}
        )
        self.assertEqual(result["seed"], "bad")
        self.assertEqual(result["timeout"], "NaN")
        self.assertEqual(result["singing"]["enabled"], "wrong")
        self.assertEqual(complete({"singing": "broken"})["singing"], "broken")
        self.assertEqual(len(revision(result)), 64)

    def test_strict_scalar_types_and_limits(self):
        for key, bad in (
            ("instruction", []),
            ("instruction", float("nan")),
            ("ref_text", None),
            ("ref_audio_path", 1),
            ("clone_use_instruction", "false"),
            ("cfg_scale", True),
            ("cfg_scale", "4"),
            ("cfg_scale", float("inf")),
            ("cfg_scale", 0),
            ("cfg_scale", 11),
            ("seed", True),
            ("seed", -1),
            ("seed", 4294967296),
            ("timeout", 0),
            ("timeout", True),
            ("timeout", 120.0),
            ("max_retries", -1),
            ("max_retries", "3"),
        ):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                validate({**defaults(), key: bad})
        for bad in (False, 0, float("nan"), 2.0, "1800"):
            cfg = defaults()
            cfg["singing"]["timeout"] = bad
            with self.subTest(singing_timeout=bad), self.assertRaises(ValueError):
                validate(cfg)
        for seed in (0, 4294967295):
            self.assertEqual(validate({**defaults(), "seed": seed})["seed"], seed)

    def test_unknown_or_missing_fields_are_rejected(self):
        for cfg in (
            {**defaults(), "unknown": 1},
            {key: value for key, value in defaults().items() if key != "seed"},
            {**defaults(), "singing": {"enabled": True}},
            {**defaults(), "singing": {**defaults()["singing"], "unknown": 1}},
        ):
            with self.assertRaises(ValueError):
                validate(cfg)

    def test_urls_normalize_and_preserve_custom_addresses(self):
        cfg = defaults()
        cfg["api_base"] = " https://custom.example/proxy/tts/// "
        cfg["singing"]["api_base"] = "http://[::1]:8861/custom/"
        result = validate(cfg)
        self.assertEqual(result["api_base"], "https://custom.example/proxy/tts")
        self.assertEqual(result["singing"]["api_base"], "http://[::1]:8861/custom")
        self.assertTrue(cfg["api_base"].endswith(" "))
        for url in (
            "",
            "ftp://example.com",
            "https://user:pass@example.com",
            "http://example.com:70000",
            "http://example.com:0",
            "http:///no-host",
            "http://example.com?token=secret",
            "http://example.com/#part",
            "http://example.com/with space",
            "http://example.com\\bad",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate({**defaults(), "api_base": url})

    def test_reference_pair_is_validated_without_opening_the_audio(self):
        path = "Z:/not-read-by-this-api/reference.wav"
        cfg = {**defaults(), "ref_audio_path": path, "ref_text": "actual source text"}
        self.assertEqual(validate(cfg)["ref_audio_path"], path)
        for path, text in (("", "orphan text"), ("Z:/reference.wav", " ")):
            with self.assertRaises(ValueError):
                validate({**defaults(), "ref_audio_path": path, "ref_text": text})


if __name__ == "__main__":
    unittest.main()
