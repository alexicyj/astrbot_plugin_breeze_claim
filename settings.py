"""Grouped settings with migration from the original credit-only configuration."""

import json
import math
import re
from urllib.parse import urlsplit

from .core import DEFAULTS, validate_config, write_json

TTS_DEFAULTS = {
    "enabled": True,
    "api_key": "",
    "voice_id": "voc_u3nxkd5578wc",
    "voice_probability": 100,
    "smart_voice": True,
    "style_prompt": "",
    "speed": 1.0,
    "language_code": "zh",
    "max_chars": 1000,
    "keep_text": False,
    "group_whitelist": "",
    "base_url": "https://api.breeze.blue",
}


def validate_tts(current, patch):
    if not isinstance(patch, dict) or set(patch) - set(TTS_DEFAULTS):
        raise ValueError("语音配置字段无效")
    result = {**TTS_DEFAULTS, **current, **patch}
    for key in ("enabled", "smart_voice", "keep_text"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    for key, lower, upper in (("voice_probability", 0, 100), ("max_chars", 10, 2000)):
        if type(result[key]) is not int or not lower <= result[key] <= upper:
            raise ValueError(f"{key} 必须在 {lower} 至 {upper} 之间")
    if (
        type(result["speed"]) not in (float, int)
        or not math.isfinite(result["speed"])
        or not 0.5 <= result["speed"] <= 2
    ):
        raise ValueError("语速必须在 0.5 至 2 之间")
    for key, limit in (
        ("api_key", 300),
        ("voice_id", 160),
        ("style_prompt", 500),
        ("group_whitelist", 5000),
        ("base_url", 500),
    ):
        if not isinstance(result[key], str) or len(result[key]) > limit:
            raise ValueError(f"{key} 格式无效")
    if not re.fullmatch(r"[A-Za-z0-9_-]*", result["voice_id"]):
        raise ValueError("音色 ID 格式无效")
    if not isinstance(result["language_code"], str) or not re.fullmatch(
        r"[a-zA-Z]{2}", result["language_code"]
    ):
        raise ValueError("语言代码需要两个字母")
    url = urlsplit(result["base_url"])
    if (
        url.scheme not in ("https", "http")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Breeze API 地址无效")
    if any(ord(c) < 32 for c in result["api_key"]):
        raise ValueError("API Key 格式无效")
    return result


def grouped_settings(raw):
    credits = raw.get("credits", {key: raw[key] for key in DEFAULTS if key in raw})
    if "credits" not in raw and "enabled" in raw:
        credits["enabled"] = raw["enabled"]
    if isinstance(credits, dict):
        if credits.get("enabled") is False:
            credits = {**credits, "auto_claim": False}
        credits = {
            key: value
            for key, value in credits.items()
            if key not in ("enabled", "browser_login_enabled", "browser_channel")
        }
    tts = raw.get(
        "tts",
        {key: raw[key] for key in TTS_DEFAULTS if key in raw and key != "enabled"},
    )
    return {"credits": validate_config({}, credits), "tts": validate_tts({}, tts)}


def migrate_legacy_file(path):
    if not path.is_file():
        return False
    raw = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(raw, dict):
        return False
    is_grouped = "credits" in raw and "tts" in raw
    if is_grouped:
        credits = raw.get("credits")
        if not isinstance(credits, dict) or not any(
            key in credits
            for key in ("enabled", "browser_login_enabled", "browser_channel")
        ):
            return False
    elif not any(key in raw for key in (*DEFAULTS, "enabled")):
        return False
    grouped = grouped_settings(raw)
    backup = path.with_name(
        path.stem + (".pre-workbench.json" if is_grouped else ".legacy-v1.json")
    )
    if not backup.exists():
        write_json(backup, raw)
    write_json(path, grouped)
    return True
