import asyncio
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_plugin_data_path,
)

from .core import normalize_cookies, validate_config
from .service import BreezeService
from .settings import grouped_settings, migrate_legacy_file, validate_tts
from .tts import TTSService

PLUGIN_NAME = "astrbot_plugin_breeze_claim"

# AstrBot normalizes the schema after importing the module; migrate before it removes old keys.
try:
    migrate_legacy_file(
        Path(get_astrbot_config_path()) / f"{Path(__file__).parent.name}_config.json"
    )
except (OSError, ValueError):
    logger.warning("[BreezeBlue] 旧配置迁移失败，请检查插件配置文件")


class BreezeClaimPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        settings = grouped_settings(dict(config))
        if (
            "credits" not in config
            or "tts" not in config
            or any(
                key in config["credits"]
                for key in ("enabled", "browser_login_enabled", "browser_channel")
            )
        ):
            config.clear()
            config.update(settings)
            config.save_config()
        self.service = BreezeService(
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME,
            settings["credits"],
        )
        self.tts = TTSService(
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "audio",
            settings["tts"],
            context,
            self.save_tts,
        )
        self.scheduler = None
        for route, handler, methods in (
            ("state", self.web_state, ["GET"]),
            ("config", self.web_config, ["POST"]),
            ("action", self.web_action, ["POST"]),
            ("session/import", self.web_import, ["POST"]),
            ("session/clear", self.web_clear, ["POST"]),
            ("tts/config", self.web_tts_config, ["POST"]),
            ("tts/action", self.web_tts_action, ["POST"]),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, "BreezeBlue " + route
            )

    async def initialize(self):
        self.scheduler = asyncio.create_task(self.schedule_loop())

    async def schedule_loop(self):
        while True:
            try:
                await self.service.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[BreezeBlue] 定时领取失败，请检查网络及数据目录")
            await asyncio.sleep(60)

    async def web_state(self):
        return json_response(
            {
                **self.service.snapshot(),
                "tts": self.tts.public_config(),
                "ttsJob": self.tts.job_state,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def payload(self):
        if len(await request.body()) > 70000:
            raise ValueError("请求不能超过 70 KiB")
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        return payload

    async def web_config(self):
        try:
            config = validate_config(self.service.config, await self.payload())
        except ValueError as exc:
            return error_response(str(exc))
        old = dict(self.config)
        self.config["credits"] = config
        try:
            self.config.save_config()
        except Exception:
            self.config.clear()
            self.config.update(old)
            return error_response("保存配置失败", status_code=500)
        self.service.config = config
        return json_response({"ok": True, "config": config})

    def save_tts(self, patch):
        config = validate_tts(self.tts.config, patch)
        old = self.config.get("tts")
        self.config["tts"] = config
        try:
            self.config.save_config()
        except Exception:
            self.config["tts"] = old
            raise
        self.tts.config = config
        return config

    async def web_tts_config(self):
        try:
            patch = await self.payload()
            if patch.get("api_key") == "":
                patch.pop("api_key")
            self.save_tts(patch)
            return json_response({"ok": True, "tts": self.tts.public_config()})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            return error_response("保存语音配置失败", status_code=500)

    async def web_tts_action(self):
        try:
            payload = await self.payload()
            return json_response(
                {"ok": True, "job": self.tts.start(payload.get("action"), payload)}
            )
        except ValueError as exc:
            return error_response(str(exc))

    @filter.on_llm_request()
    async def inject_voice_capability(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        if self.tts.allowed(event):
            req.system_prompt = (
                (req.system_prompt or "")
                + "\n回复可转换为语音。用户要求朗读时直接提供完整原文，不要调用系统语音工具。"
            )

    @filter.on_decorating_result()
    async def decorate_reply(self, event: AstrMessageEvent):
        try:
            await self.tts.decorate(event)
        except Exception:
            logger.warning("[BreezeBlue] 语音转换失败，保留文字回复")

    async def web_action(self):
        try:
            action = (await self.payload()).get("action")
            if action not in ("status", "claim"):
                raise ValueError("操作无效")
            return json_response({"ok": True, "job": self.service.start(action)})
        except ValueError as exc:
            return error_response(str(exc))

    async def web_import(self):
        try:
            cookies = normalize_cookies((await self.payload()).get("cookies"))
            return json_response(
                {"ok": True, "job": self.service.start("import", cookies)}
            )
        except (ValueError, TypeError, AttributeError) as exc:
            return error_response(
                str(exc) if isinstance(exc, ValueError) else "Cookie 格式无效"
            )

    async def web_clear(self):
        if (
            self.service.lock.locked()
            or self.service.job
            and not self.service.job.done()
        ):
            return error_response("请等待当前任务完成")
        self.service.client.cookie_path.unlink(missing_ok=True)
        self.service.state.pop("summary", None)
        for field in ("account", "verifiedAt", "accountKey"):
            self.service.state.pop(field, None)
        self.service.save()
        return json_response({"ok": True, "message": "登录 Cookie 已删除"})

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("breeze")
    async def breeze_command(
        self, event: AstrMessageEvent, action: str = "status", text: str = ""
    ):
        """统一管理积分、语音与 Dashboard。"""
        event.set_extra("breezeblue_command", True)
        if action == "dashboard":
            yield event.plain_result(
                "AstrBot WebUI → 插件 → BreezeBlue工作台 → BreezeBlue工作台"
            )
            return
        if action in ("on", "off", "voice", "balance", "voices", "test"):
            try:
                if action in ("on", "off"):
                    self.save_tts({"enabled": action == "on"})
                    yield event.plain_result(
                        "语音回复已开启" if action == "on" else "语音回复已关闭"
                    )
                elif action == "voice":
                    cfg = self.tts.config
                    yield event.plain_result(
                        f"语音开关：{cfg['enabled']}\n智能判断：{cfg['smart_voice']}\n语音概率：{cfg['voice_probability']}%\n音色：{cfg['voice_id']}"
                    )
                elif action == "test":
                    sample = text.strip() or "你好，我是 BreezeBlue，很高兴认识你。"
                    path = await self.tts.synthesize(sample)
                    yield event.chain_result([self.tts.record(path, sample)])
                else:
                    result = await self.tts.perform(action, {"search": text})
                    if action == "balance":
                        yield event.plain_result(f"API 余额：{result['balance']}")
                    else:
                        yield event.plain_result(
                            "\n".join(
                                f"{v['name']}：{v['id']}" for v in result["voices"][:10]
                            )
                            or "未找到音色"
                        )
            except Exception:
                yield event.plain_result("语音操作失败，请检查 API Key、音色与网络")
            return
        if action not in ("status", "claim"):
            yield event.plain_result(
                "用法：/breeze status|claim|dashboard|voice|on|off|balance|voices|test"
            )
            return
        if (
            self.service.lock.locked()
            or self.service.job
            and not self.service.job.done()
        ):
            yield event.plain_result("已有任务正在执行，请稍后再试")
            return
        try:
            result = await self.service.perform(action)
        except Exception:
            yield event.plain_result("Breeze 操作失败，请检查主机网络和数据目录")
            return
        if result.get("ok"):
            message = result.get("message", "查询成功")
            yield event.plain_result(
                f"{message}\n余额：{result.get('balance')}\n今日已领：{bool(result.get('claimedToday') or result.get('claimed'))}"
            )
        else:
            yield event.plain_result(result.get("message", "操作失败"))

    async def terminate(self):
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        await self.service.close()
        await self.tts.close()
