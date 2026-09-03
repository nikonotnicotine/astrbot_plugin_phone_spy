import asyncio
import os
import tempfile

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.all import llm_tool, MessageChain, Image
from astrbot.api import logger


@register("phone_spy", "nikonotnicotine", "让 LLM 查看你的 iPhone 屏幕", "1.0.0")
class PhoneSpyStar(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 启动 HTTP 接收服务器
        from .core.receiver import setup_receiver
        setup_receiver(context, self.config)

    @llm_tool("check_phone_screen")
    async def check_phone_screen(self, event: AstrMessageEvent):
        """
        当你（AI）想查看用户当前手机屏幕界面时调用此工具。
        它会触发用户 iPhone 自动截屏并上传，返回屏幕内容的详细描述。
        """
        config = self.config
        api_url = config.get("vision_api_url", "")
        api_key = config.get("vision_api_key", "")
        model = config.get("vision_model", "gpt-4o")
        vision_prompt = config.get(
            "vision_prompt",
            "你是一个查岗助手，请直接回复，详细描述这张手机屏幕截图的内容。告诉我用户现在大概在干什么（比如在刷抖音、看微信消息、打游戏、听音乐等）。",
        )
        timeout = config.get("timeout_seconds", 90)
        vision_timeout = config.get("vision_api_timeout", 60)
        send_to_user = config.get("forward_image", False)

        if not api_url or not api_key:
            return "查岗失败：视觉模型未配置，请提醒用户在插件设置中填写 vision_api_url 和 vision_api_key。"

        from .core.mailer import send_trigger_email
        from .core.pending import wait_for_screenshot
        from .core.vision import analyze_screenshot
        from .core.fallback import try_fallback

        try:
            logger.info("📱 大模型发起了手机查岗请求，正在发送触发邮件...")
            await send_trigger_email(config)
            logger.info("✉️ 触发邮件已发送，等待手机截图上传...")

            image_data = await wait_for_screenshot(timeout=timeout)

            logger.info(f"📤 截图已收到，正在请求视觉模型解析（超时 {vision_timeout}s）...")

            if send_to_user:
                await self._forward_image(event, image_data)

            description = await analyze_screenshot(
                image_data, api_url, api_key, model, vision_prompt, timeout=vision_timeout
            )

            logger.info(f"✅ 手机查岗完成：{description[:60]}...")
            return (
                f"查岗成功，用户当前的手机屏幕内容描述如下：\n{description}\n\n"
                "请根据上述内容，以你的角色人设对用户发送回复。"
            )

        except asyncio.TimeoutError:
            logger.warning(f"⏰ 手机查岗超时（{timeout}s），尝试故障转移...")
            if config.get("auto_fallback", True):
                return await try_fallback("手机", "响应超时", event, config)
            both_failed = config.get(
                "both_failed_prompt_template",
                "无法查看当前用户电脑与手机的屏幕，原因可能是{{error}}，可能用户正在忙或者在睡觉，请根据人设向用户发送信息。",
            )
            return both_failed.replace("{{error}}", "手机响应超时")

        except Exception as e:
            logger.error(f"❌ 手机查岗异常：{type(e).__name__} - {e}")
            if config.get("auto_fallback", True):
                return await try_fallback("手机", str(e), event, config)
            both_failed = config.get(
                "both_failed_prompt_template",
                "无法查看当前用户电脑与手机的屏幕，原因可能是{{error}}，可能用户正在忙或者在睡觉，请根据人设向用户发送信息。",
            )
            return both_failed.replace("{{error}}", str(e))

    async def _forward_image(self, event: AstrMessageEvent, image_data: bytes):
        """将截图发送给用户，发送后异步清理临时文件"""
        try:
            fd, temp_path = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as f:
                f.write(image_data)

            try:
                image_component = Image.fromFileSystem(temp_path)
            except AttributeError:
                image_component = Image(path=temp_path)

            mc = MessageChain()
            mc.chain.append(image_component)
            await event.send(mc)
            logger.info("📲 手机截图已同步发送给用户")

            async def _cleanup(path):
                await asyncio.sleep(10)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    logger.error(f"清理临时文件失败: {e}")

            asyncio.create_task(_cleanup(temp_path))

        except Exception as e:
            logger.error(f"发送截图给用户失败: {e}")
