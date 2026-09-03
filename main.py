"""手机查岗插件 (Phone Spy)。

流程:
1. 大模型调用 check_phone_screen 工具
2. 插件通过 SMTP 向 iCloud 邮箱发送触发邮件
3. iPhone 上的 iOS 快捷指令邮件自动化收到邮件后截屏，压缩为 JPEG
4. 快捷指令将截图 POST 到本插件的 HTTP 接收服务 (webhook_port)
5. 插件把截图交给视觉模型分析，将描述返回给大模型
6. 超时或失败时，可选自动回退到电脑屏幕查岗工具 (check_computer_screen)

设计文档: PHONE_SPY_DESIGN.md
"""
from __future__ import annotations

import asyncio
import os
import tempfile

from astrbot.api import logger
from astrbot.api.all import Image, MessageChain, llm_tool
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star

from .core.fallback import try_fallback
from .core.mailer import send_trigger_email
from .core.pending import pending_manager
from .core.receiver import ScreenshotReceiver
from .core.vision import analyze_image

# 允许超大截图（JPEG 压缩后通常几 MB 内）
MAX_IMAGE_SIZE = 128 * 1024 * 1024  # 128 MB


class PhoneSpy(Star):
    """手机查岗：通过 iCloud 邮件自动化触发 iPhone 截图回传，AI 进行视觉分析。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.receiver: ScreenshotReceiver | None = None
        self._receiver_task: asyncio.Task | None = None

        # 从配置中读取常用值
        self.webhook_port = self.config.get("webhook_port", 8080)
        self.webhook_path = self.config.get("webhook_path", "/phone/screenshot")
        self.webhook_secret = self.config.get("webhook_secret", "")
        self.smtp_host = self.config.get("smtp_host", "smtp.qq.com")
        self.smtp_port = self.config.get("smtp_port", 465)
        self.smtp_user = self.config.get("smtp_user", "")
        self.smtp_password = self.config.get("smtp_password", "")
        self.icloud_address = self.config.get("icloud_address", "")
        self.vision_api_url = self.config.get("vision_api_url", "")
        self.vision_api_key = self.config.get("vision_api_key", "")
        self.vision_model = self.config.get("vision_model", "gpt-4o")
        self.timeout_seconds = self.config.get("timeout_seconds", 90)
        self.forward_image = self.config.get("forward_image", False)
        self.auto_fallback = self.config.get("auto_fallback", True)
        self.fallback_prompt_template = self.config.get(
            "fallback_prompt_template",
            "当前{{failed_device}}因为{{error}}无法查看，系统已返回目前活跃的{{active_device}}设备，请根据人设与上下文向用户发送信息。",
        )
        self.both_failed_prompt_template = self.config.get(
            "both_failed_prompt_template",
            "无法查看当前用户电脑与手机的屏幕，原因可能是{{error}}，可能用户正在忙或者在睡觉，请根据人设向用户发送信息。",
        )

    async def initialize(self) -> None:
        """插件激活时启动截图接收服务。"""
        if not self.webhook_secret or len(self.webhook_secret) < 16:
            logger.warning(
                "⚠️ webhook_secret 未配置或长度不足 16 字符，"
                "截图接收服务仍会启动，但 iPhone 快捷指令将无法通过校验。"
            )

        self.receiver = ScreenshotReceiver(
            port=self.webhook_port,
            path=self.webhook_path,
            secret=self.webhook_secret,
        )
        await self.receiver.start()

    async def terminate(self) -> None:
        """插件禁用/重载时停止接收服务并清理。"""
        if self.receiver is not None:
            await self.receiver.stop()
            self.receiver = None
        pending_manager.clear()

    @llm_tool("check_phone_screen")
    async def check_phone_screen(self, event: AstrMessageEvent):
        """
        当你（AI）想查看用户当前 iPhone 手机屏幕界面时调用此工具。它会通过 iCloud 邮件自动化触发用户 iPhone 截屏，并返回详细的画面描述供你分析。
        注意: 手机查岗失败时插件会自动回退到电脑查岗（check_computer_screen），无需你手动切换。
        """
        # 配置完整性检查
        missing = []
        if not self.smtp_user or not self.smtp_password:
            missing.append("SMTP 账号/授权码 (smtp_user, smtp_password)")
        if not self.icloud_address:
            missing.append("iCloud 邮箱地址 (icloud_address)")
        if not self.webhook_secret:
            missing.append("接收密钥 (webhook_secret)")
        if missing:
            return "手机查岗失败：插件配置不完整，缺少 " + "、".join(missing) + "。请提醒用户配置后再试。"

        # 先确认接收服务是否在运行
        if self.receiver is None or not self.receiver.is_running:
            if self.auto_fallback:
                return await self._fallback(event, "截图接收服务未运行")
            return "手机查岗失败：截图接收服务未运行，请重新激活插件。"

        # 1. 发送触发邮件
        try:
            await send_trigger_email(
                host=self.smtp_host,
                port=self.smtp_port,
                username=self.smtp_user,
                password=self.smtp_password,
                to_address=self.icloud_address,
            )
        except Exception:
            if self.auto_fallback:
                return await self._fallback(event, "触发邮件发送失败")
            return "手机查岗失败：触发邮件发送失败，请检查 SMTP 配置。"

        # 2. 创建等待条目，等待截图到达
        req_id = pending_manager.create()
        logger.info(
            f"📱 触发邮件已发送，等待 iPhone 截图回传（超时 {self.timeout_seconds} 秒）..."
        )
        try:
            image_data = await pending_manager.wait(req_id, self.timeout_seconds)
        except asyncio.TimeoutError:
            if self.auto_fallback:
                return await self._fallback(
                    event, f"等待 iPhone 截图超时（{self.timeout_seconds} 秒）"
                )
            return (
                "手机查岗失败：等待 iPhone 截图超时（"
                f"{self.timeout_seconds} 秒）。手机可能锁屏、iCloud 邮件未同步或快捷指令未配置。"
            )

        # 3. 将截图转发给用户（可选）
        if self.forward_image:
            await self._forward_image(event, image_data)

        # 4. 视觉模型分析
        if not self.vision_api_url or not self.vision_api_key:
            return (
                "手机查岗：已收到 iPhone 截图，但视觉模型未配置，无法分析。"
                "请提醒用户配置 vision_api_url 和 vision_api_key。"
            )

        try:
            content = await analyze_image(
                image_data=image_data,
                api_url=self.vision_api_url,
                api_key=self.vision_api_key,
                model=self.vision_model,
            )
        except asyncio.TimeoutError:
            if self.auto_fallback:
                return await self._fallback(event, "视觉模型分析超时")
            return "手机查岗：已收到截图但视觉模型分析超时，请检查视觉模型配置。"
        except Exception as e:
            logger.error(f"❌ 视觉模型分析异常: {type(e).__name__} - {e}")
            if self.auto_fallback:
                return await self._fallback(
                    event, f"视觉模型分析失败（{type(e).__name__}）"
                )
            return "手机查岗：已收到截图但视觉模型分析失败，请检查视觉模型配置。"

        # 5. 返回给大模型
        logger.info("✅ 手机查岗完成，描述已返回给大模型。")
        return f"手机查岗成功，用户当前的 iPhone 屏幕内容描述如下：\n{content}\n\n请根据上述内容，以你的角色人设对用户发送回复。"

    async def _fallback(self, event: AstrMessageEvent, reason: str) -> str:
        """手机查岗失败时回退到电脑查岗，返回给大模型的文本。"""
        return await try_fallback(
            event=event,
            tool_manager=self._get_tool_manager(),
            failed_device="手机",
            error=reason,
            fallback_template=self.fallback_prompt_template,
            both_failed_template=self.both_failed_prompt_template,
        )

    def _get_tool_manager(self):
        """获取全局 LLM 工具管理器。"""
        return self.context.get_llm_tool_manager()

    async def _forward_image(self, event: AstrMessageEvent, image_data: bytes) -> None:
        """将截图转发给用户（可选）。"""
        try:
            fd, temp_path = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as f:
                f.write(image_data)

            try:
                image_component = Image.fromFileSystem(temp_path)
            except AttributeError:
                image_component = Image(file=temp_path)

            mc = MessageChain()
            mc.chain.append(image_component)
            await event.send(mc)
            logger.info("📷 截图已转发给用户。")

            async def cleanup_temp_file(path: str):
                await asyncio.sleep(10)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"清理临时文件失败: {e}")

            asyncio.create_task(cleanup_temp_file(temp_path))
        except Exception as e:
            logger.error(f"📷 截图转发失败: {type(e).__name__} - {e}")
