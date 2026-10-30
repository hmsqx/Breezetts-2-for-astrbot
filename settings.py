"""Canonical plugin configuration, validation and atomic persistence."""

import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

MAX_CONFIG_BODY_BYTES = 64 * 1024

CONFIG_KEYS = (
    "instruction",
    "cfg_scale",
    "seed",
    "ref_audio_path",
    "ref_text",
    "clone_use_instruction",
    "api_base",
    "timeout",
    "max_retries",
    "singing",
)


def _as_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_plugin_config() -> dict:
    """Load dashboard settings, using the legacy file only when absent.

    Returns:
        Settings belonging to this plugin.

    Raises:
        OSError: The existing configuration cannot be read.
        ValueError: The existing JSON is invalid.
        TypeError: The configuration is not an object.
    """
    path = (
        Path(get_astrbot_data_path())
        / "config"
        / "astrbot_plugin_breeze_tts_config.json"
    )
    if not path.exists():
        path = Path(__file__).parent / "data" / "cmd_config.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise TypeError("Breeze TTS configuration must be an object")
    return {key: cfg[key] for key in CONFIG_KEYS if key in cfg}


def _save_plugin_config(cfg: dict, config: AstrBotConfig | None = None) -> None:
    """Persist settings through AstrBot's atomic configuration writer.

    Args:
        cfg: Plugin settings to save.
        config: The live dashboard configuration, when available.

    Raises:
        Exception: Saving fails; the live configuration is restored.
    """
    if config is None:
        schema = json.loads(
            Path(__file__)
            .with_name("_conf_schema.json")
            .read_text(encoding="utf-8-sig")
        )
        path = (
            Path(get_astrbot_data_path())
            / "config"
            / "astrbot_plugin_breeze_tts_config.json"
        )
        config = AstrBotConfig(config_path=str(path), schema=schema)
    previous = dict(config)
    try:
        config.save_config({key: cfg[key] for key in CONFIG_KEYS if key in cfg})
    except Exception:
        config.clear()
        config.update(previous)
        raise


def _plugin_config_defaults() -> dict:
    """Use the plugin schema as the single source of dashboard defaults."""
    schema = json.loads(
        Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8-sig")
    )
    defaults = {key: schema[key]["default"] for key in CONFIG_KEYS if key != "singing"}
    defaults["singing"] = {
        key: item["default"] for key, item in schema["singing"]["items"].items()
    }
    return defaults


def _complete_plugin_config(cfg: dict) -> dict:
    """Fill absent fields without repairing or writing existing invalid values."""
    defaults = _plugin_config_defaults()
    complete = {key: cfg.get(key, value) for key, value in defaults.items()}
    singing = complete["singing"]
    if isinstance(singing, dict):
        complete["singing"] = {
            key: singing.get(key, value) for key, value in defaults["singing"].items()
        }
    if type(complete["cfg_scale"]) in (int, float) and 1 <= complete["cfg_scale"] <= 10:
        complete["cfg_scale"] = float(complete["cfg_scale"])
    # Legacy NaN/Infinity values must remain editable without breaking JSONResponse.
    return json.loads(json.dumps(complete, ensure_ascii=False), parse_constant=str)


def _config_revision(cfg: dict) -> str:
    encoded = json.dumps(
        cfg, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_plugin_config(cfg: dict) -> dict:
    """Validate the complete dashboard form; never read its reference audio path."""
    defaults = _plugin_config_defaults()
    if not isinstance(cfg, dict) or set(cfg) != set(defaults):
        raise ValueError("插件配置项不完整或包含未知字段，请重新读取配置。")
    singing = cfg["singing"]
    if not isinstance(singing, dict) or set(singing) != set(defaults["singing"]):
        raise ValueError("翻唱配置项不完整或包含未知字段。")
    result = {**cfg, "singing": dict(singing)}
    for key in ("instruction", "ref_audio_path", "ref_text", "api_base"):
        if not isinstance(result[key], str) or "\0" in result[key]:
            raise ValueError(f"{key} 必须是有效文本。")
    if (
        type(result["clone_use_instruction"]) is not bool
        or type(result["singing"]["enabled"]) is not bool
    ):
        raise ValueError("启用翻唱和克隆语音引导必须是布尔值。")
    scale = cfg["cfg_scale"]
    if type(scale) not in (int, float) or not 1 <= scale <= 10:
        raise ValueError("CFG 强度必须是 1 到 10 之间的有限数字。")
    result["cfg_scale"] = float(scale)
    seed = result["seed"]
    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise ValueError("随机种子必须是 0 到 4294967295 之间的整数。")
    for label, value in (
        ("普通语音超时", result["timeout"]),
        ("最多请求次数", result["max_retries"]),
        ("翻唱超时", result["singing"]["timeout"]),
    ):
        try:
            valid = type(value) is int and value > 0 and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{label}必须是有限的正整数。")
    for settings in (result, result["singing"]):
        value = settings["api_base"]
        if not isinstance(value, str):
            raise ValueError("服务地址必须是文本。")
        value = value.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            valid = (
                parsed.scheme in ("http", "https")
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (port is None or 1 <= port <= 65535)
                and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
                and "\\" not in value
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "服务地址须为无用户名密码、查询参数或片段的 HTTP(S) 基础地址。"
            )
        settings["api_base"] = value
    result["ref_audio_path"] = result["ref_audio_path"].strip()
    if bool(result["ref_audio_path"]) != bool(result["ref_text"].strip()):
        raise ValueError("参考音频路径和对应文本须同时填写，或同时清空。")
    return result
