"""Serialized Cookie-based operations and durable daily scheduling."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core import BreezeClient, read_json, write_json


class BreezeService:
    def __init__(self, root, config, transport=None):
        self.root, self.config = root, config
        self.client = BreezeClient(root, transport)
        self.state_path = root / "state.json"
        self.state = read_json(self.state_path, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.lock = asyncio.Lock()
        self.job = None
        self.job_state = None
        key = self.client.session_key()
        if key:
            self.state.setdefault("accountKey", key)
            if self.state["accountKey"] == key:
                for item in self.state.get("history", []):
                    item.setdefault("accountKey", key)

    def now(self):
        return datetime.now(ZoneInfo(self.config["timezone"]))

    def save(self):
        write_json(self.state_path, self.state)

    def completed(self, now):
        reset = self.state.get("successResetAt")
        try:
            reset_at = datetime.fromisoformat(reset) if reset else None
            if reset_at is not None and reset_at.tzinfo is not None:
                return now < reset_at
        except (ValueError, TypeError):
            pass
        return self.state.get("successDay") == now.date().isoformat()

    def snapshot(self):
        now = self.now()
        key = self.client.session_key()
        summary = self.state.get("summary") or {}
        has_session = self.client.has_session()
        auth_state = (
            "unbound"
            if not has_session
            else "expired"
            if summary.get("needLogin")
            else "logged_in"
            if summary.get("ok") and key and self.state.get("accountKey") == key
            else "bound"
        )
        claimed_dates = set()
        for item in self.state.get("history", []):
            if not key or item.get("accountKey") != key or not item.get("claimed"):
                continue
            try:
                at = datetime.fromisoformat(item["at"])
                if at.tzinfo is not None:
                    claimed_dates.add(at.astimezone(now.tzinfo).date().isoformat())
            except (ValueError, TypeError, KeyError):
                continue
        try:
            summary_at = datetime.fromisoformat(summary.get("at", ""))
            if (
                key
                and summary.get("ok")
                and summary.get("claimedToday")
                and summary_at.tzinfo is not None
                and summary_at.astimezone(now.tzinfo).date() == now.date()
            ):
                claimed_dates.add(now.date().isoformat())
        except (ValueError, TypeError):
            pass
        hour, minute = map(int, self.config["claim_time"].split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        attempts = (
            self.state.get("attempts", 0)
            if self.state.get("attemptDay") == now.date().isoformat()
            else 0
        )
        if self.completed(now) or attempts >= 3:
            target += timedelta(days=1)
        elif target <= now:
            retry_at = datetime.fromtimestamp(
                self.state.get("lastAttempt", 0) + 900, now.tzinfo
            )
            target = max(now + timedelta(seconds=60), retry_at)
        return {
            "config": dict(self.config),
            "hasSession": has_session,
            "account": {
                "state": auth_state,
                **(
                    self.state.get("account") or {}
                    if key and self.state.get("accountKey") == key
                    else {}
                ),
                "verifiedAt": self.state.get("verifiedAt")
                if key and self.state.get("accountKey") == key
                else None,
            },
            "calendar": {
                "today": now.date().isoformat(),
                "timezone": self.config["timezone"],
                "claimedDates": sorted(claimed_dates),
            },
            "summary": self.state.get("summary"),
            "job": self.job_state,
            "history": [
                {field: value for field, value in item.items() if field != "accountKey"}
                for item in self.state.get("history", [])
            ],
            "nextRunAt": target.isoformat()
            if self.config["auto_claim"] and self.client.has_session()
            else None,
            "retryCount": attempts,
            "serverTime": now.isoformat(),
        }

    def start(self, action, cookies=None):
        if self.job and not self.job.done():
            raise ValueError("已有任务正在执行")
        self.job_state = {
            "action": action,
            "running": True,
            "startedAt": self.now().isoformat(),
        }
        self.job = asyncio.create_task(self._job(action, cookies))
        return self.job_state

    def bind_cookies(self, cookies):
        previous_key = self.client.session_key()
        write_json(self.client.cookie_path, cookies)
        if self.client.session_key() != previous_key:
            for field in ("account", "verifiedAt", "successDay", "successResetAt"):
                self.state.pop(field, None)
        self.state["accountKey"] = self.client.session_key()
        self.state["attempts"] = 0

    def remember_account(self, result):
        if not result.get("ok"):
            return
        key = self.client.session_key()
        if key != self.state.get("accountKey"):
            self.state.pop("account", None)
        self.state["accountKey"] = key
        if result.get("account"):
            self.state["account"] = result["account"]
        self.state["verifiedAt"] = self.now().isoformat()

    async def _job(self, action, cookies):
        try:
            result = await self.perform(action, cookies)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = {
                "ok": False,
                "message": "操作失败，请检查网络和数据目录",
            }
        self.job_state = {
            **self.job_state,
            "running": False,
            "finishedAt": self.now().isoformat(),
            "result": result,
        }

    async def perform(self, action, cookies=None, scheduled=False):
        async with self.lock:
            if action == "status":
                result = await self.client.status()
            elif action == "claim":
                result = await self.client.claim()
                # The claim response may omit summary fields; refresh without hiding its result.
                summary = await self.client.status()
                if summary.get("ok") or summary.get("needLogin"):
                    self.state["summary"] = {**summary, "at": self.now().isoformat()}
                    self.remember_account(summary)
                complete = result.get("ok") and (
                    result.get("claimed")
                    or result.get("claimedToday")
                    or summary.get("claimedToday")
                )
                if complete:
                    self.state["successDay"] = self.now().date().isoformat()
                    self.state["successResetAt"] = summary.get(
                        "nextResetAt"
                    ) or result.get("nextResetAt")
                item = {
                    "at": self.now().isoformat(),
                    "source": "自动" if scheduled else "手动",
                    **result,
                    "accountKey": self.client.session_key(),
                }
                self.state["history"] = [item, *self.state.get("history", [])][:100]
            elif action == "import":
                result = await self.client.status(cookies)
                if result.get("ok"):
                    self.bind_cookies(cookies)
                    self.state.pop("successDay", None)
                    self.state.pop("successResetAt", None)
                    self.state["attempts"] = 0
                    result["message"] = "登录态已验证并保存"
            else:
                raise ValueError("未知操作")
            if action != "claim" and (action != "import" or result.get("ok")):
                self.state["summary"] = {**result, "at": self.now().isoformat()}
                self.remember_account(result)
            self.save()
            return result

    async def tick(self, now=None):
        if not self.config["auto_claim"] or not self.client.has_session():
            return
        if self.lock.locked() or self.job and not self.job.done():
            return
        now = now or self.now()
        day = now.date().isoformat()
        if now.strftime("%H:%M") < self.config["claim_time"]:
            return
        if self.completed(now):
            return
        if self.state.get("attemptDay") != day:
            self.state.update(attemptDay=day, attempts=0, lastAttempt=0)
        if (
            self.state.get("attempts", 0) >= 3
            or now.timestamp() - self.state.get("lastAttempt", 0) < 900
        ):
            return
        self.state["attempts"] = self.state.get("attempts", 0) + 1
        self.state["lastAttempt"] = now.timestamp()
        self.save()
        await self.perform("claim", scheduled=True)

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.client.close()
