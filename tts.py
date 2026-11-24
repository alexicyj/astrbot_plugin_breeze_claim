"""Breeze TTS protocol, voice selection, design and LLM reply conversion.

Feature reference: https://github.com/hangdingding/AstrBot-breezetts
"""

import asyncio
import base64
import json
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from astrbot.api import logger
from astrbot.api.message_components import At, Plain, Record

DIRECTOR_PROMPT = """决定助手回复是否适合语音。闲聊、关心和情绪表达可用语音；代码、表格、链接、长教程保留文字。
返回 JSON：文字为 {"mode":"text"}；语音为 {"mode":"voice","script":"适合朗读的原意口语稿","instructions":"情绪、语速和停顿指导"}。
不增加事实。用户要求朗读或背诵时保留原文。不要返回其他内容。"""


def clean_speech(text):
    return re.sub(r"[*_`>#]+", "", text).strip()


class TTSService:
    def __init__(self, root: Path, config, context, save_config):
        self.root, self.config, self.context = root, config, context
        self.save_config = save_config
        self.http = httpx.AsyncClient(timeout=180, follow_redirects=False)
        self.job = None
        self.job_state = None

    def public_config(self):
        return {
            **{k: v for k, v in self.config.items() if k != "api_key"},
            "hasApiKey": bool(self.config["api_key"]),
        }

    def allowed(self, event):
        config = self.config
        if not config["enabled"] or not config["api_key"] or not config["voice_id"]:
            return False
        if not config["smart_voice"] and config["voice_probability"] == 0:
            return False
        if event.get_platform_name() == "dingtalk":
            return False
        groups = {
            group.strip()
            for group in config["group_whitelist"].split(",")
            if group.strip()
        }
        group = str(event.get_group_id() or "")
        return not groups or not group or group in groups

    async def request(self, method, path, body=None, params=None):
        if not self.config["api_key"]:
            raise ValueError("请先配置 Breeze API Key")
        response = await self.http.request(
            method,
            self.config["base_url"].rstrip("/") + path,
            headers={"xi-api-key": self.config["api_key"]},
            json=body,
            params=params,
        )
        if response.status_code != 200:
            raise ValueError(f"Breeze TTS 请求失败 HTTP {response.status_code}")
        return response

    async def synthesize(self, text, instructions="", voice_id=None):
        voice = voice_id or self.config["voice_id"]
        if not voice or not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
            raise ValueError("请配置有效的音色 ID")
        if not text or len(text) > self.config["max_chars"]:
            raise ValueError("试听文字为空或超过单条字数上限")
        body = {
            "text": text,
            "language_code": self.config["language_code"],
            "voice_settings": {"speed": self.config["speed"]},
        }
        if instructions:
            body["instructions"] = instructions[:500]
        response = await self.request(
            "POST",
            f"/v1/text-to-speech/{quote(voice, safe='')}/stream",
            body,
            {"output_format": "wav"},
        )
        audio = response.content
        if (
            not audio
            or len(audio) > 25 * 1024 * 1024
            or audio[:4] not in (b"RIFF", b"RF64")
        ):
            raise ValueError("Breeze 未返回有效的 WAV 音频")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (uuid.uuid4().hex + ".wav")
        path.write_bytes(audio)
        for old in self.root.glob("*.wav"):
            try:
                if old.stat().st_mtime < time.time() - 3600:
                    old.unlink()
            except OSError:
                pass
        return path.resolve()

    @staticmethod
    def record(path, text):
        result = Record.fromFileSystem(str(path))
        result.text = text
        return result

    async def direct(self, event, text):
        provider = await self.context.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )
        prompt = (
            DIRECTOR_PROMPT
            + f"\n发音风格：{self.config['style_prompt']}\n用户：{event.message_str[:1000]}\n回复：{text[:2000]}"
        )
        result = await self.context.llm_generate(
            chat_provider_id=provider, prompt=prompt
        )
        raw = str(result.completion_text).strip()
        start, end = raw.find("{"), raw.rfind("}")
        decision = json.loads(raw[start : end + 1])
        if not isinstance(decision, dict):
            raise ValueError("语音判断结果无效")
        return decision

    async def decorate(self, event):
        if not self.allowed(event) or event.get_extra("breezeblue_command", False):
            return
        result = event.get_result()
        if (
            not result
            or not result.is_llm_result()
            or not result.chain
            or any(isinstance(c, Record) for c in result.chain)
        ):
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain)).strip()
        if not text or len(text) > self.config["max_chars"]:
            return
        script, instructions = text, ""
        if self.config["smart_voice"]:
            try:
                decision = await self.direct(event, text)
                if decision.get("mode") != "voice":
                    return
                script = str(decision.get("script") or text)
                instructions = str(decision.get("instructions") or "")
                if re.search(r"念|朗读|朗诵|读一下|背诵|用语音", event.message_str):
                    script = clean_speech(text)
            except Exception:
                logger.warning("[BreezeBlue] 语音判断失败，使用原回复合成")
        elif random.random() * 100 >= self.config["voice_probability"]:
            return
        if len(script) > self.config["max_chars"]:
            return
        path = await self.synthesize(script, instructions)
        record = self.record(path, text)
        if self.config["keep_text"]:
            result.chain.append(record)
        else:
            result.chain = [
                c for c in result.chain if not isinstance(c, (Plain, At))
            ] + [record]

    def start(self, action, payload):
        if self.job and not self.job.done():
            raise ValueError("已有语音任务正在执行")
        if action not in (
            "balance",
            "voices",
            "preview",
            "test",
            "design",
            "save_design",
        ):
            raise ValueError("语音操作无效")
        self.job_state = {"action": action, "running": True, "id": uuid.uuid4().hex}
        self.job = asyncio.create_task(self.run_job(action, payload))
        return self.job_state

    async def run_job(self, action, payload):
        try:
            result = await self.perform(action, payload)
            self.job_state = {
                **self.job_state,
                "running": False,
                "result": {"ok": True, **result},
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "语音操作失败，请检查网络和 API 配置"
            )
            self.job_state = {
                **self.job_state,
                "running": False,
                "result": {"ok": False, "message": message},
            }

    async def perform(self, action, payload):
        if action == "preview":
            voice_id = str(payload.get("voice_id") or self.config["voice_id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
                raise ValueError("请配置有效的音色 ID")
            data = (
                await self.request(
                    "GET", "/v1/voices", params={"search": voice_id, "page_size": "20"}
                )
            ).json()
            voice = next(
                (v for v in data.get("voices", []) if v.get("voice_id") == voice_id),
                {},
            )
            url = str(voice.get("preview_url") or "")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("该音色暂无语音库试听音频")
            return {
                "voiceId": voice_id,
                "name": voice.get("name", ""),
                "previewUrl": url,
            }
        if action == "balance":
            data = (await self.request("GET", "/v1/balance")).json()
            return {"balance": data.get("balance")}
        if action == "voices":
            data = (
                await self.request(
                    "GET",
                    "/v1/voices",
                    params={
                        "search": str(payload.get("search", ""))[:200],
                        "page_size": "20",
                    },
                )
            ).json()
            return {
                "voices": [
                    {
                        "id": v.get("voice_id", ""),
                        "name": v.get("name", ""),
                        "description": v.get("description", ""),
                        "language": v.get("language_code", ""),
                        "previewUrl": v.get("preview_url", ""),
                    }
                    for v in data.get("voices", [])
                ]
            }
        if action == "test":
            path = await self.synthesize(
                str(payload.get("text", "")).strip()
                or "你好，我是 BreezeBlue，很高兴认识你。",
                voice_id=payload.get("voice_id"),
            )
            return {
                "audio": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mime": "audio/wav",
            }
        if action == "design":
            description, text = (
                str(payload.get("description", "")).strip(),
                str(payload.get("text", "")).strip(),
            )
            if not 3 <= len(description) <= 500 or len(text) > 500:
                raise ValueError("音色描述需要 3 至 500 字，试听文字最多 500 字")
            body = {
                "voice_description": description,
                "language_code": self.config["language_code"],
                "preview_count": 3,
            }
            if text:
                body["text"] = text
            data = (
                await self.request("POST", "/v1/voice-previews/design", body)
            ).json()
            previews = [
                {
                    "id": p.get("generated_voice_id", ""),
                    "audio": p.get("audio_base_64", ""),
                    "mime": p.get("media_type", "audio/mpeg"),
                    "duration": p.get("duration_secs"),
                }
                for p in data.get("previews", [])
            ]
            if not previews:
                raise ValueError("没有生成音色预览")
            return {"previews": previews}
        if action == "save_design":
            generated_id = str(payload.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", generated_id):
                raise ValueError("请选择有效的音色预览")
            body = {
                "voice_name": str(payload.get("name", "")).strip()[:80] or "我的音色",
                "language_code": self.config["language_code"],
                "voice_description": str(payload.get("description", ""))[:500],
            }
            data = (
                await self.request(
                    "POST", f"/v1/voice-previews/{generated_id}/save", body
                )
            ).json()
            if not data.get("voice_id"):
                raise ValueError("Breeze 未返回音色 ID")
            self.save_config({"voice_id": data["voice_id"]})
            return {"voiceId": data["voice_id"], "message": "音色已保存并使用"}
        raise ValueError("语音操作无效")

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.http.aclose()
