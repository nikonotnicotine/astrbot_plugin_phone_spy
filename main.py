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
import json
import os
import tempfile

import aiohttp
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
        self.smtp_host = self.config.get("smtp_host", "smtp.mail.me.com")
        self.smtp_port = self.config.get("smtp_port", 587)
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

        # 网易云音乐配置
        netease = self.config.get("netease_music", {})
        self.netease_enabled = netease.get("enable_music_control", False)
        self.netease_daily = netease.get("enable_daily_recommend", False)
        self.netease_fm = netease.get("enable_personal_fm", False)
        self.netease_favorite = netease.get("enable_favorite_playlist", False)
        self.netease_playlist_id = netease.get("favorite_playlist_id", "")
        self.netease_play_pause = netease.get("enable_play_pause", False)
        self.netease_play_song = netease.get("enable_play_song", False)
        self.auto_screenshot_after_music = netease.get("auto_screenshot_after_music", False)

        # 查看类配置
        view_actions = self.config.get("view_actions", {})
        self.enable_battery = view_actions.get("enable_battery", False)
        self.enable_wechat = view_actions.get("enable_wechat", False)
        self.enable_alipay = view_actions.get("enable_alipay", False)
        self.enable_bilibili = view_actions.get("enable_bilibili", False)
        self.enable_douyin_msg = view_actions.get("enable_douyin_msg", False)
        self.enable_douyin_profile = view_actions.get("enable_douyin_profile", False)
        self.enable_taobao_order = view_actions.get("enable_taobao_order", False)
        self.enable_taobao_cart = view_actions.get("enable_taobao_cart", False)
        self.auto_screenshot_view = view_actions.get("auto_screenshot", True)

        # 控制类配置
        control_actions = self.config.get("control_actions", {})
        self.enable_location = control_actions.get("enable_location", False)
        self.enable_alarm = control_actions.get("enable_alarm", False)
        self.enable_lock_screen = control_actions.get("enable_lock_screen", False)
        self.auto_screenshot_after_control = control_actions.get("auto_screenshot_after_control", False)

    async def initialize(self) -> None:
        """插件激活时启动截图接收服务，并按配置注入工具。"""
        if not self.webhook_secret or len(self.webhook_secret) < 16:
            logger.warning(
                "⚠️ webhook_secret 未配置或长度不足 16 字符，"
                "截图接收服务仍会启动，但 iPhone 快捷指令将无法通过校验。"
            )

        # 按需注册 JSON 数据路由
        json_paths = []
        if self.enable_location:
            json_paths.append("/phone/location")
        if self.enable_battery:
            json_paths.append("/phone/battery")

        self.receiver = ScreenshotReceiver(
            port=self.webhook_port,
            path=self.webhook_path,
            secret=self.webhook_secret,
            json_paths=json_paths,
        )
        await self.receiver.start()

        # 按需移除未启用的工具，节省 token 开销
        tm = self.context.get_llm_tool_manager()

        if not self.netease_enabled:
            for fn in ("play_netease_daily", "play_netease_fm",
                       "play_netease_favorite", "play_pause_music",
                       "play_netease_song"):
                tm.remove_func(fn)
        else:
            if not self.netease_daily:
                tm.remove_func("play_netease_daily")
            if not self.netease_fm:
                tm.remove_func("play_netease_fm")
            if not self.netease_favorite:
                tm.remove_func("play_netease_favorite")
            if not self.netease_play_pause:
                tm.remove_func("play_pause_music")
            if not self.netease_play_song:
                tm.remove_func("play_netease_song")

        if not self.enable_battery:
            tm.remove_func("get_phone_battery")
        if not self.enable_wechat:
            tm.remove_func("view_wechat")
        if not self.enable_alipay:
            tm.remove_func("view_alipay_bill")
        if not self.enable_bilibili:
            tm.remove_func("view_bilibili_history")
        if not self.enable_douyin_msg:
            tm.remove_func("view_douyin_messages")
        if not self.enable_douyin_profile:
            tm.remove_func("view_douyin_profile")
        if not self.enable_taobao_order:
            tm.remove_func("view_taobao_orders")
        if not self.enable_taobao_cart:
            tm.remove_func("view_taobao_cart")
        if not self.enable_location:
            tm.remove_func("get_phone_location")
        if not self.enable_alarm:
            tm.remove_func("set_phone_alarm")
        if not self.enable_lock_screen:
            tm.remove_func("lock_phone_screen")

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
        try:
            req_id = pending_manager.create()
        except RuntimeError as e:
            logger.warning(f"⚠️ 手机查岗请求被限流: {e}")
            if self.auto_fallback:
                return await self._fallback(event, "待处理查岗请求过多")
            return "手机查岗失败：当前查岗请求过多，请稍后再试。"
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

    # ------------------------------------------------------------------ #
    # 内部帮助方法                                                          #
    # ------------------------------------------------------------------ #

    def _smtp_kwargs(self) -> dict:
        """返回发邮件所需的公共 SMTP 参数。"""
        return dict(
            host=self.smtp_host,
            port=self.smtp_port,
            username=self.smtp_user,
            password=self.smtp_password,
            to_address=self.icloud_address,
        )

    async def _wait_screenshot_and_analyze(self, action_name: str) -> str:
        """等待截图回传并用视觉模型分析，返回描述字符串。超时时返回提示文本。"""
        if not self.vision_api_url or not self.vision_api_key:
            return f"已发送{action_name}指令（视觉模型未配置，无法分析截图）。"
        req_id = pending_manager.create()
        try:
            image_data = await pending_manager.wait(req_id, self.timeout_seconds)
        except asyncio.TimeoutError:
            return f"{action_name}：截图未在 {self.timeout_seconds} 秒内回传。"
        try:
            content = await analyze_image(
                image_data=image_data,
                api_url=self.vision_api_url,
                api_key=self.vision_api_key,
                model=self.vision_model,
            )
        except Exception as e:
            logger.error(f"视觉模型分析失败: {e}")
            return f"{action_name}：已收到截图，但视觉模型分析失败（{type(e).__name__}）。"
        return f"{action_name}完成，屏幕内容：\n{content}"

    async def _search_netease_song(self, keyword: str) -> tuple[str, str] | tuple[None, None]:
        """搜索网易云歌曲，返回 (歌曲ID, 显示名)。未找到时返回 (None, None)。"""
        url = "https://music.163.com/api/cloudsearch/pc"
        params = {"s": keyword, "type": 1, "offset": 0, "limit": 1}
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://music.163.com/",
            "Cookie": "NMTID=1",
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
        songs = (data.get("result") or {}).get("songs") or []
        if not songs:
            return None, None
        s = songs[0]
        artists = "/".join(a["name"] for a in s.get("ar", []))
        return str(s["id"]), f"{s['name']} - {artists}"

    # ------------------------------------------------------------------ #
    # 网易云音乐工具                                                        #
    # ------------------------------------------------------------------ #

    @llm_tool("play_netease_daily")
    async def play_netease_daily(self, event: AstrMessageEvent):
        """播放网易云音乐每日推荐。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_DAILY")
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze("播放每日推荐")
        return "已向 iPhone 发送播放每日推荐指令。"

    @llm_tool("play_netease_fm")
    async def play_netease_fm(self, event: AstrMessageEvent):
        """播放网易云音乐私人漫游（私人 FM）。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_FM")
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze("播放私人漫游")
        return "已向 iPhone 发送播放私人漫游指令。"

    @llm_tool("play_netease_favorite")
    async def play_netease_favorite(self, event: AstrMessageEvent):
        """播放网易云音乐红心歌单（我喜欢的音乐）。"""
        if self.netease_playlist_id:
            body = f"orpheus://playlist/{self.netease_playlist_id}/?autoplay=1"
            await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_FAVORITE", body=body)
        else:
            await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_FAVORITE")
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze("播放红心歌单")
        return "已向 iPhone 发送播放红心歌单指令。"

    @llm_tool("play_pause_music")
    async def play_pause_music(self, event: AstrMessageEvent):
        """播放或暂停当前正在播放的音乐（系统级媒体控制，适用于所有音乐 App）。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_PLAYPAUSE")
        return "已向 iPhone 发送播放/暂停指令。"

    @llm_tool("play_netease_song")
    async def play_netease_song(self, event: AstrMessageEvent, song_name: str):
        """
        在网易云音乐播放指定歌曲。
        参数:
            song_name: 歌曲名称，如"晴天"、"稻香 周杰伦"
        """
        try:
            song_id, display_name = await self._search_netease_song(song_name)
        except Exception as e:
            logger.error(f"搜索网易云歌曲失败: {e}")
            return f"搜索歌曲《{song_name}》时网络出错，请稍后再试。"

        if song_id is None:
            return f"未找到歌曲《{song_name}》，请换个关键词试试。"

        scheme = f"orpheus://song/{song_id}/?autoplay=1"
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_PLAY", body=scheme)
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze(f"播放《{display_name}》")
        return f"已向 iPhone 发送播放《{display_name}》指令。"

    # ------------------------------------------------------------------ #
    # 查看类工具                                                            #
    # ------------------------------------------------------------------ #

    @llm_tool("get_phone_battery")
    async def get_phone_battery(self, event: AstrMessageEvent):
        """获取用户 iPhone 的当前电池电量百分比。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_BATTERY")
        req_id = pending_manager.create()
        try:
            raw = await pending_manager.wait(req_id, self.timeout_seconds)
            data = json.loads(raw.decode("utf-8"))
            level = data.get("level", data.get("battery", "未知"))
            return f"iPhone 当前电量：{level}%"
        except asyncio.TimeoutError:
            return f"获取电量失败：iPhone 未在 {self.timeout_seconds} 秒内响应。"
        except Exception as e:
            return f"获取电量失败：{e}"

    @llm_tool("view_wechat")
    async def view_wechat(self, event: AstrMessageEvent):
        """打开 iPhone 上的微信并截图，让 AI 看到当前的微信界面。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_WECHAT")
        return await self._wait_screenshot_and_analyze("查看微信")

    @llm_tool("view_alipay_bill")
    async def view_alipay_bill(self, event: AstrMessageEvent):
        """打开支付宝账单页面并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_ALIPAY")
        return await self._wait_screenshot_and_analyze("查看支付宝账单")

    @llm_tool("view_bilibili_history")
    async def view_bilibili_history(self, event: AstrMessageEvent):
        """打开 B 站观看历史页面并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_BILIBILI")
        return await self._wait_screenshot_and_analyze("查看 B 站历史")

    @llm_tool("view_douyin_messages")
    async def view_douyin_messages(self, event: AstrMessageEvent):
        """打开抖音私信页面并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_DOUYIN_MSG")
        return await self._wait_screenshot_and_analyze("查看抖音私信")

    @llm_tool("view_douyin_profile")
    async def view_douyin_profile(self, event: AstrMessageEvent):
        """打开抖音个人主页并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_DOUYIN_PROFILE")
        return await self._wait_screenshot_and_analyze("查看抖音个人主页")

    @llm_tool("view_taobao_orders")
    async def view_taobao_orders(self, event: AstrMessageEvent):
        """打开淘宝订单页面并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_TAOBAO_ORDER")
        return await self._wait_screenshot_and_analyze("查看淘宝订单")

    @llm_tool("view_taobao_cart")
    async def view_taobao_cart(self, event: AstrMessageEvent):
        """打开淘宝购物车并截图。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_TAOBAO_CART")
        return await self._wait_screenshot_and_analyze("查看淘宝购物车")

    # ------------------------------------------------------------------ #
    # 控制类工具                                                            #
    # ------------------------------------------------------------------ #

    @llm_tool("get_phone_location")
    async def get_phone_location(self, event: AstrMessageEvent):
        """获取用户 iPhone 的当前 GPS 地理位置（街道地址 + 经纬度）。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_LOCATION")
        req_id = pending_manager.create()
        try:
            raw = await pending_manager.wait(req_id, self.timeout_seconds)
            loc = json.loads(raw.decode("utf-8"))
            return (
                f"用户当前位置：{loc.get('address', '未知')}"
                f"（经度 {loc.get('longitude', '?')}，纬度 {loc.get('latitude', '?')}）"
            )
        except asyncio.TimeoutError:
            return f"获取位置失败：iPhone 未在 {self.timeout_seconds} 秒内响应。"
        except Exception as e:
            return f"获取位置失败：{e}"

    @llm_tool("set_phone_alarm")
    async def set_phone_alarm(self, event: AstrMessageEvent, time: str):
        """
        在 iPhone 上设置一个闹钟。
        参数:
            time: 闹钟时间，格式 HH:MM，如 "07:30"、"22:00"
        """
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_ALARM", body=time)
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze(f"设置闹钟 {time}")
        return f"已向 iPhone 发送设置闹钟 {time} 的指令。"

    @llm_tool("lock_phone_screen")
    async def lock_phone_screen(self, event: AstrMessageEvent):
        """锁定用户的 iPhone 屏幕。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_LOCK")
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze("锁定屏幕")
        return "已向 iPhone 发送锁定屏幕指令。"
