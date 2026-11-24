"""Breeze web API and validated local session storage."""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

BASE = "https://breezeblue.ai"
DEFAULTS = {
    "auto_claim": True,
    "claim_time": "08:10",
    "timezone": "Asia/Shanghai",
}


def validate_config(current, patch):
    if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
        raise ValueError("配置字段无效")
    result = {**DEFAULTS, **current, **patch}
    for key in ("auto_claim",):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    if not isinstance(result["claim_time"], str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", result["claim_time"]
    ):
        raise ValueError("签到时间必须是 00:00 至 23:59")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("时区无效，例如 Asia/Shanghai") from None
    return result


def read_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def normalize_cookies(value):
    if isinstance(value, str):
        value = value.strip()
        if len(value) > 65536:
            raise ValueError("Cookie 内容不能超过 64 KiB")
        if value.startswith(("[", "{")):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("Cookie JSON 格式无效") from None
        elif "=" in value:
            value = [
                {
                    "name": pair.strip().split("=", 1)[0],
                    "value": pair.strip().split("=", 1)[1],
                }
                for pair in value.removeprefix("Cookie:").strip().split(";")
                if "=" in pair
            ]
        else:
            value = [{"name": "breeze_blue_session", "value": value}]
    if isinstance(value, dict):
        value = value.get("cookies")
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("请输入 session 值、Cookie 请求头或 Cookie JSON")
    cookies = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Cookie 项必须是对象")
        domain = item.get("domain", "breezeblue.ai")
        if not isinstance(domain, str):
            raise ValueError("Cookie 域名无效")
        domain = domain.lstrip(".").lower()
        if domain != "breezeblue.ai" and not domain.endswith(".breezeblue.ai"):
            continue
        name, val = item.get("name"), item.get("value")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            raise ValueError("Cookie 名称无效")
        if (
            not isinstance(val, str)
            or not val
            or any(ord(c) < 33 or ord(c) > 126 or c == ";" for c in val)
        ):
            raise ValueError("Cookie 值无效")
        expires = item.get("expires", -1)
        if not isinstance(expires, (int, float)):
            raise ValueError("Cookie 到期时间无效")
        cookies.append(
            {
                "name": name,
                "value": val,
                "domain": "." + domain,
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": expires,
            }
        )
    if not any(
        c["name"] == "breeze_blue_session" and c["domain"] == ".breezeblue.ai"
        for c in cookies
    ):
        raise ValueError("缺少 breeze_blue_session 登录 Cookie")
    return cookies


def public_account(data):
    """Allow only optional display fields from the existing summary response."""
    source = data.get("user") or data.get("account")
    if not isinstance(source, dict):
        return None
    result = {}
    for field, aliases in {
        "username": ("username", "display_name", "displayName", "name"),
        "email": ("email",),
    }.items():
        for key in aliases:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                result[field] = value.strip()[:160]
                break
    return result or None


class BreezeClient:
    def __init__(self, root: Path, transport=None):
        self.root = root
        self.cookie_path = root / "cookies.json"
        self.http = httpx.AsyncClient(
            timeout=45,
            transport=transport,
            follow_redirects=False,
            headers={
                "Origin": BASE,
                "Referer": BASE + "/app/home",
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )

    def has_session(self):
        return self.cookie_path.exists()

    def session_key(self):
        try:
            cookies = normalize_cookies(read_json(self.cookie_path, []))
        except (ValueError, TypeError, AttributeError):
            return None
        session = next(
            cookie["value"]
            for cookie in cookies
            if cookie["name"] == "breeze_blue_session"
            and cookie["domain"] == ".breezeblue.ai"
        )
        return hashlib.sha256(session.encode("ascii")).hexdigest()

    async def request(self, path, method="GET", cookies=None):
        if cookies is None:
            cookies = read_json(self.cookie_path, [])
        try:
            cookies = normalize_cookies(cookies)
        except (ValueError, AttributeError, TypeError):
            return {
                "ok": False,
                "needLogin": True,
                "message": "请导入有效登录 Cookie",
            }
        # Send only cookies applicable to this host, and never follow redirects.
        header = "; ".join(
            f"{c['name']}={c['value']}"
            for c in cookies
            if c["domain"].lstrip(".") == "breezeblue.ai"
            and (
                c["expires"] == -1
                or isinstance(c["expires"], (int, float))
                and c["expires"] > time.time()
            )
        )
        try:
            response = await self.http.request(
                method,
                BASE + path,
                headers={"Cookie": header},
                **({"json": {}} if method == "POST" else {}),
            )
            if response.status_code != 200:
                code = response.status_code
                return {
                    "ok": False,
                    "needLogin": code in (401, 403),
                    "message": f"Breeze 请求失败 HTTP {code}",
                }
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except httpx.HTTPError:
            return {"ok": False, "message": "Breeze 网络请求失败，请稍后重试"}
        except ValueError:
            return {"ok": False, "message": "Breeze 返回了无效的 JSON 数据"}
        result = {
            "ok": True,
            "balance": data.get("balance"),
            "claimedToday": data.get("daily_claim_claimed_today"),
            "available": data.get("daily_claim_available"),
            "amount": data.get("daily_claim_amount"),
            "nextResetAt": data.get("daily_claim_next_reset_at"),
            "account": public_account(data),
        }
        if method == "POST":
            got = bool(data.get("claimed"))
            result.update(
                claimed=got,
                credits=data.get("credits_issued", 0),
                message="领取成功" if got else "今天已领取或暂不可领取",
            )
        return result

    async def status(self, cookies=None):
        return await self.request("/api/credits/summary", cookies=cookies)

    async def claim(self):
        return await self.request("/api/credits/daily-claim", "POST")

    async def close(self):
        await self.http.aclose()
