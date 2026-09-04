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
import base64
import json
import os
import tempfile
import time
import traceback

import aiohttp
from astrbot.api import logger
from astrbot.api.all import Image, MessageChain, Plain, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.combined_spy import (
    DEFAULT_FINAL_PROMPT,
    DEFAULT_HINT,
    DEFAULT_MARKER,
    DEFAULT_REPLY_TEMPLATE,
    DEFAULT_ROUND_PROMPT,
    SpyFeature,
    SpyOutcome,
    parse_features,
    render_prompt,
    split_numbered,
    strip_label,
)
from .core.fallback import try_fallback
from .core.mailer import send_trigger_email
from .core.memory_bridge import (
    DEFAULT_TOOL_TEMPLATE,
    DEFAULT_USER_IMAGE_PROMPT,
    DEFAULT_USER_IMAGE_TEMPLATE,
    IMAGE_TASK_KEY,
    ORIG_KEY,
    add_record,
    clear_records,
    describe_user_images,
    get_records,
    render_record,
    strip_records,
)
from .core.pending import pending_manager
from .core.receiver import ScreenshotReceiver
from .core.vision import analyze_image, analyze_images

# 允许超大截图（JPEG 压缩后通常几 MB 内）
MAX_IMAGE_SIZE = 128 * 1024 * 1024  # 128 MB
# 整合查岗攒下的记录/上下文，每个会话最多留这么多条，避免用户长期不说话时无限堆积
COMBINED_PENDING_LIMIT = 50
# 批量识图结果切不开序号时，各项的占位文本（整段原文会另附一次，避免重复 N 遍）
BATCH_FALLBACK_TEXT = "见下方整体描述"


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
        self.netease_play_playlist = netease.get("enable_play_playlist", False)
        self.auto_screenshot_after_music = netease.get("auto_screenshot_after_music", False)
        self.playlist_presets = self._parse_playlist_presets(
            netease.get("playlist_presets", [])
        )

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
        self.enable_alarm_on = control_actions.get("enable_alarm_on", False)
        self.enable_alarm_off = control_actions.get("enable_alarm_off", False)
        self.enable_lock_screen = control_actions.get("enable_lock_screen", False)
        self.auto_screenshot_after_control = control_actions.get("auto_screenshot_after_control", False)

        # 记忆记录配置（写给 conversation_logger / romantic_memory 等记忆插件）
        memo = self.config.get("memory_record", {})
        self.memo_enable = memo.get("enable", True)
        self.memo_record_tools = memo.get("record_tools", True)
        self.memo_record_user_images = memo.get("record_user_images", True)
        self.memo_private_only = memo.get("private_only", True)
        self.memo_max_chars = memo.get("max_detail_chars", 0)
        self.memo_template = memo.get("template", DEFAULT_TOOL_TEMPLATE)
        self.memo_user_image_template = memo.get(
            "user_image_template", DEFAULT_USER_IMAGE_TEMPLATE
        )
        self.memo_user_image_prompt = memo.get(
            "user_image_prompt", DEFAULT_USER_IMAGE_PROMPT
        )
        self.memo_user_image_timeout = memo.get("user_image_timeout", 45)
        self.vision_max_tokens = self.config.get("vision_max_tokens", 4096)

        # 整合查岗配置（默认关闭）
        combined = self.config.get("combined_spy", {})
        self.combined_enabled = combined.get("enable", False)
        self.combined_features = (
            parse_features(combined.get("features", []))
            if self.combined_enabled
            else []
        )
        self.combined_interval = combined.get("interval_seconds", 5)
        self.combined_streaming = combined.get("streaming", False)
        self.combined_suppress_tools = combined.get("suppress_other_tools", True)
        self.combined_private_only = combined.get("private_only", True)
        self.combined_hold_user_message = combined.get("hold_user_message", True)
        self.combined_hold_max = combined.get("hold_max_seconds", 600)
        self.combined_cooldown = combined.get("cooldown_seconds", 300)
        self.combined_history_rounds = combined.get("history_rounds", 10)
        self.combined_vision_timeout = combined.get("vision_timeout_seconds", 180)
        self.combined_marker = (combined.get("marker") or DEFAULT_MARKER).strip()
        self.combined_hint = combined.get("hint", DEFAULT_HINT)
        self.combined_round_prompt = combined.get("round_prompt", DEFAULT_ROUND_PROMPT)
        self.combined_final_prompt = combined.get("final_prompt", DEFAULT_FINAL_PROMPT)
        self.combined_batch_prompt = combined.get("vision_batch_prompt", "")
        self.combined_reply_template = combined.get(
            "reply_template", DEFAULT_REPLY_TEMPLATE
        )

        # 整合查岗运行状态（按会话 umo 区分）
        self._combined_runs: dict[str, asyncio.Event] = {}  # 正在查岗的会话 -> 完成信号
        self._combined_tasks: dict[str, asyncio.Task] = {}  # 正在查岗的后台任务
        self._combined_pending_context: dict[str, list[str]] = {}  # 待注入上下文的主动发言
        self._combined_pending_records: dict[str, list[str]] = {}  # 待写入记忆的记录
        self._combined_last_done: dict[str, float] = {}  # 上次查岗完成时间（冷却用）

    async def initialize(self) -> None:
        """插件激活时启动截图接收服务，并按配置注入工具。"""
        if not self.webhook_secret or len(self.webhook_secret) < 16:
            logger.warning(
                "⚠️ webhook_secret 未配置或长度不足 16 字符，"
                "截图接收服务仍会启动，但 iPhone 快捷指令将无法通过校验。"
            )

        # 按需注册 JSON 数据路由
        combined_keys = {f.key for f in self.combined_features}
        json_paths = []
        if self.enable_location or "location" in combined_keys:
            json_paths.append("/phone/location")
        if self.enable_battery or "battery" in combined_keys:
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
                       "play_netease_song", "play_netease_playlist"):
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
            if not self.netease_play_playlist:
                tm.remove_func("play_netease_playlist")

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
        if not self.enable_alarm_on:
            tm.remove_func("enable_phone_alarm")
        if not self.enable_alarm_off:
            tm.remove_func("disable_phone_alarm")
        if not self.enable_lock_screen:
            tm.remove_func("lock_phone_screen")

        # 整合查岗开着的话，把「查看类」工具的注入全撤掉，只留一条 prompt 提示，
        # 省 token。网易云 / 闹钟 / 锁屏是「操控」不是「查岗」，按需求保留。
        if self.combined_enabled:
            if not self.combined_features:
                logger.warning(
                    "⚠️ 整合查岗已开启，但没有配置任何查岗项目，功能不会生效。"
                )
            if self.combined_suppress_tools:
                for fn in (
                    "check_phone_screen",
                    "get_phone_battery",
                    "view_wechat",
                    "view_alipay_bill",
                    "view_bilibili_history",
                    "view_douyin_messages",
                    "view_douyin_profile",
                    "view_taobao_orders",
                    "view_taobao_cart",
                    "get_phone_location",
                ):
                    tm.remove_func(fn)
                logger.info(
                    "🧹 整合查岗已开启，已移除查看类工具的注入（网易云/闹钟/锁屏保留）。"
                )

    async def terminate(self) -> None:
        """插件禁用/重载时停止接收服务并清理。"""
        # 先放行所有被「等查岗结束」卡住的消息，别让它们陪着插件一起挂掉
        for done in list(self._combined_runs.values()):
            done.set()
        for task in list(self._combined_tasks.values()):
            task.cancel()
        self._combined_runs.clear()
        self._combined_tasks.clear()
        if self.receiver is not None:
            await self.receiver.stop()
            self.receiver = None
        pending_manager.clear()

    # ------------------------------------------------------------------ #
    # 记忆记录：把工具调用与图片识别结果喂给记忆插件                          #
    # 原理与安全性说明见 core/memory_bridge.py 顶部注释                      #
    # ------------------------------------------------------------------ #

    def _memo_allowed(self, event: AstrMessageEvent) -> bool:
        """记忆记录总开关 + 私聊限制。"""
        if not self.memo_enable:
            return False
        if self.memo_private_only and getattr(event.message_obj, "group_id", None):
            # conversation_logger 与 romantic_memory 都只在私聊记录，群聊里做这些是白费
            return False
        return True

    def _memo(self, event: AstrMessageEvent, feature: str, detail: str) -> None:
        """记一条工具调用。在工具执行过程中调用，稍后统一注入给记忆插件。"""
        if not self.memo_record_tools or not self._memo_allowed(event):
            return
        add_record(
            event,
            render_record(self.memo_template, feature, detail, self.memo_max_chars),
        )

    async def _memo_collect_user_images(self, event: AstrMessageEvent) -> None:
        """取回后台的用户图片识别结果并记录。"""
        task = event.get_extra(IMAGE_TASK_KEY)
        if task is None:
            return
        event.set_extra(IMAGE_TASK_KEY, None)
        try:
            descriptions = await asyncio.wait_for(
                asyncio.shield(task), timeout=self.memo_user_image_timeout
            )
        except Exception as e:
            logger.warning(
                f"🖼️ 用户图片识别未在超时内完成，跳过记忆记录: {type(e).__name__} - {e}"
            )
            task.cancel()
            return
        for desc in descriptions:
            add_record(
                event,
                render_record(
                    self.memo_user_image_template, "", desc, self.memo_max_chars
                ),
            )

    @filter.on_llm_request(priority=9999)
    async def _memo_prepare_user_images(self, event: AstrMessageEvent, req) -> None:
        """用户发图时，后台并行跑一次视觉识别，避免拖慢正常回复。"""
        if not self.memo_record_user_images or not self._memo_allowed(event):
            return
        if not self.vision_api_url or not self.vision_api_key:
            return
        images = [
            comp
            for comp in (getattr(event.message_obj, "message", None) or [])
            if isinstance(comp, Image)
        ]
        if not images:
            return
        logger.info(f"🖼️ 检测到用户发送 {len(images)} 张图片，后台识别中（用于记忆记录）。")
        event.set_extra(
            IMAGE_TASK_KEY,
            asyncio.create_task(
                describe_user_images(
                    images,
                    api_url=self.vision_api_url,
                    api_key=self.vision_api_key,
                    model=self.vision_model,
                    prompt=self.memo_user_image_prompt,
                    timeout=self.memo_user_image_timeout,
                    max_tokens=self.vision_max_tokens,
                )
            ),
        )

    @filter.on_llm_response(priority=9999)
    async def _memo_inject(self, event: AstrMessageEvent, resp) -> None:
        """在记忆插件抄写之前，把记录临时拼进回复文本。"""
        if not self.memo_enable:
            return
        await self._memo_collect_user_images(event)
        records = get_records(event)
        if not records:
            return
        original = resp.completion_text or ""
        event.set_extra(ORIG_KEY, original)
        block = "\n".join(records)
        resp.completion_text = f"{block}\n{original}" if original else block
        logger.info(f"🧠 已向记忆插件注入 {len(records)} 条记录。")

    @filter.on_llm_response(priority=-9999)
    async def _memo_restore(self, event: AstrMessageEvent, resp) -> None:
        """记忆插件抄完了，撕掉便利贴——用户与 LLM 上下文都看不到这些记录。"""
        if event.get_extra(ORIG_KEY) is None:
            return
        resp.completion_text = event.get_extra(ORIG_KEY)
        event.set_extra(ORIG_KEY, None)
        clear_records(event)

    @filter.on_decorating_result(priority=-9999)
    async def _memo_scrub(self, event: AstrMessageEvent) -> None:
        """兜底：万一还原钩子没跑到（例如别的插件中途 stop_event），也别漏给用户。"""
        result = event.get_result()
        if result is None or not result.chain:
            return
        marker = self.combined_marker if self.combined_enabled else ""
        for comp in result.chain:
            if not isinstance(comp, Plain):
                continue
            text = comp.text or ""
            if "[系统提示:" in text:
                text = strip_records(text)
            if marker and marker in text:
                text = text.replace(marker, "").strip()
            comp.text = text
        # 便利贴/标记剥完可能只剩空壳，清掉它们，别发一条空消息出去
        if any(isinstance(c, Plain) and not (c.text or "").strip() for c in result.chain):
            kept = [
                comp
                for comp in result.chain
                if not (isinstance(comp, Plain) and not (comp.text or "").strip())
            ]
            if kept:
                result.chain = kept

    # ------------------------------------------------------------------ #
    # 整合查岗：LLM 在回复里写个标记 → 插件后台把配置的项目连着查一遍          #
    # 设计说明见 core/combined_spy.py 顶部注释                              #
    # ------------------------------------------------------------------ #

    def _combined_ready(self, event: AstrMessageEvent) -> bool:
        """这个会话现在能不能查岗（配置是否齐全、是否允许在这个场景里查）。"""
        if not self.combined_enabled or not self.combined_features:
            return False
        if self.combined_private_only and getattr(event.message_obj, "group_id", None):
            return False
        if not self.smtp_user or not self.smtp_password or not self.icloud_address:
            logger.warning("⚠️ 整合查岗：SMTP 或 iCloud 邮箱没配全，查不了。")
            return False
        if self.receiver is None or not self.receiver.is_running:
            logger.warning("⚠️ 整合查岗：截图接收服务没在跑，查不了。")
            return False
        return True

    def _combined_active(self, umo: str) -> bool:
        return umo in self._combined_runs

    def _combined_cooling(self, umo: str) -> bool:
        last = self._combined_last_done.get(umo)
        if last is None:
            return False
        return (time.time() - last) < self.combined_cooldown

    @filter.on_llm_request(priority=10000)
    async def _combined_before_llm(self, event: AstrMessageEvent, req) -> None:
        """查岗期间按住新消息 → 补上后台攒的上下文与记忆 → 塞那一条查岗提示。"""
        if not self.combined_enabled:
            return
        umo = event.unified_msg_origin

        # 1. 查岗还在跑：先把这条消息按住，等查完再让它去问 LLM
        #    钩子是被管线 await 着的，在这里等就等于把这一轮原地挂住，之后照常继续，
        #    记忆插件依旧按正常流程记录这一轮，不需要额外发消息。
        done = self._combined_runs.get(umo)
        if done is not None and self.combined_hold_user_message:
            logger.info(
                f"⏸️ 查岗还没结束，先按住用户这条消息（最多等 {self.combined_hold_max} 秒）。"
            )
            try:
                await asyncio.wait_for(done.wait(), timeout=self.combined_hold_max)
                logger.info("▶️ 查岗结束，放行刚才按住的消息。")
            except asyncio.TimeoutError:
                logger.warning(
                    f"⚠️ 等查岗结束超过 {self.combined_hold_max} 秒，直接放行用户消息。"
                )

        # 2. 把查岗时主动说过的话补进上下文，免得 LLM 不知道自己刚说了什么
        said = self._combined_pending_context.pop(umo, None)
        if said:
            for text in said:
                req.contexts.append({"role": "assistant", "content": text})
            logger.info(f"📌 已把查岗时主动说的 {len(said)} 句话补进上下文。")

        # 3. 后台查岗攒下的记忆记录挂到这一轮
        #    便利贴必须贴在真实事件上才有记忆插件来抄，后台任务自己贴没人看
        records = self._combined_pending_records.pop(umo, None)
        if records and self._memo_allowed(event):
            for text in records:
                add_record(event, text)
            logger.info(f"🧠 已把查岗攒下的 {len(records)} 条记录挂到本轮。")

        # 4. 只注入一条提示，告诉 LLM 它可以查岗（省 token）
        if not self.combined_hint or not self._combined_ready(event):
            return
        if self._combined_active(umo) or self._combined_cooling(umo):
            return
        base = req.system_prompt or ""
        req.system_prompt = f"{base}\n{self.combined_hint}".strip()

    @filter.on_llm_response(priority=10000)
    async def _combined_catch_marker(self, event: AstrMessageEvent, resp) -> None:
        """抓到标记 → 从回复里剥掉 → 后台开查。

        放在最前面剥，是为了让记忆插件与用户都看不到这个标记；
        开后台任务本身不阻塞，用户那条回复照常先发出去。
        """
        if not self.combined_enabled or not self.combined_marker:
            return
        text = resp.completion_text or ""
        if self.combined_marker not in text:
            return
        resp.completion_text = text.replace(self.combined_marker, "").strip()
        if not self._combined_ready(event):
            logger.info("🕵️ 抓到查岗标记，但当前会话查不了，只把标记剥掉。")
            return
        self._combined_launch(event)

    def _combined_launch(self, event: AstrMessageEvent) -> None:
        """开一个后台任务去查岗，不占着当前这条回复。"""
        umo = event.unified_msg_origin
        if self._combined_active(umo):
            logger.info("🕵️ 上一轮查岗还没跑完，这次标记忽略。")
            return
        if self._combined_cooling(umo):
            left = self.combined_cooldown - (time.time() - self._combined_last_done[umo])
            logger.info(f"🕵️ 查岗冷却中（还剩 {left:.0f} 秒），这次标记忽略。")
            return
        done = asyncio.Event()
        self._combined_runs[umo] = done
        self._combined_tasks[umo] = asyncio.create_task(
            self._combined_worker(event, umo, done)
        )

    async def _combined_worker(
        self, event: AstrMessageEvent, umo: str, done: asyncio.Event
    ) -> None:
        """后台任务外壳：无论怎么结束，都要放行被按住的消息。"""
        try:
            await self._combined_run(event, umo)
        except asyncio.CancelledError:
            logger.info("🕵️ 整合查岗任务被取消。")
            raise
        except Exception as e:
            logger.error(
                f"❌ 整合查岗出错: {type(e).__name__} - {e}\n{traceback.format_exc()}"
            )
        finally:
            self._combined_last_done[umo] = time.time()
            self._combined_runs.pop(umo, None)
            self._combined_tasks.pop(umo, None)
            done.set()

    async def _combined_run(self, event: AstrMessageEvent, umo: str) -> None:
        """一项接一项地查，中间隔 interval_seconds 秒。"""
        features = self.combined_features
        total = len(features)
        logger.info(
            f"🕵️ 开始整合查岗，共 {total} 项："
            f"{'、'.join(f.name for f in features)}"
            f"（{'流式' if self.combined_streaming else '一次性'}）"
        )
        outcomes: list[SpyOutcome] = []
        for index, feature in enumerate(features, 1):
            if index > 1 and self.combined_interval > 0:
                await asyncio.sleep(self.combined_interval)
            outcome = await self._combined_step(
                feature, need_text=self.combined_streaming
            )
            outcomes.append(outcome)
            if self.combined_streaming:
                # 流式：这一项已经看清了，立刻问一次 LLM 并发出去
                self._combined_memo(umo, outcome)
                await self._combined_stream_reply(event, umo, outcome, index, total)
        if not self.combined_streaming:
            await self._combined_batch_reply(event, umo, outcomes)
        logger.info("✅ 整合查岗结束。")

    async def _combined_step(self, feature: SpyFeature, need_text: bool) -> SpyOutcome:
        """查一项：发触发指令 → 等回传 →（流式才在这里识图）。"""
        try:
            await send_trigger_email(**self._smtp_kwargs(), subject=feature.subject)
        except Exception as e:
            logger.error(
                f"❌ 查岗「{feature.name}」触发邮件发送失败: {type(e).__name__} - {e}"
            )
            return SpyOutcome(
                feature, False, f"没看到（触发指令发不出去：{type(e).__name__}）"
            )

        try:
            # guard_late：这一项超时后留张迟到条，别让迟到的图被下一项冒领
            req_id = pending_manager.create(label=feature.name, guard_late=True)
        except RuntimeError as e:
            logger.warning(f"⚠️ 查岗「{feature.name}」被限流: {e}")
            return SpyOutcome(feature, False, "没看到（待处理请求过多）")

        try:
            raw = await pending_manager.wait(req_id, self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                f"⚠️ 查岗「{feature.name}」等回传超时（{self.timeout_seconds} 秒）。"
            )
            return SpyOutcome(
                feature,
                False,
                f"没看到（{self.timeout_seconds} 秒内没等到回传，手机可能锁屏或没网）",
            )

        if feature.kind == "json":
            return self._combined_parse_json(feature, raw)

        if not need_text:
            # 一次性模式：先把图攒着，最后一起丢给识图模型
            return SpyOutcome(feature, True, "", image=raw)

        if not self.vision_api_url or not self.vision_api_key:
            return SpyOutcome(feature, False, "拿到截图了，但视觉模型没配置，看不清内容")
        try:
            content = await analyze_image(
                image_data=raw,
                api_url=self.vision_api_url,
                api_key=self.vision_api_key,
                model=self.vision_model,
                max_tokens=self.vision_max_tokens,
            )
        except Exception as e:
            logger.error(f"❌ 查岗「{feature.name}」识图失败: {type(e).__name__} - {e}")
            return SpyOutcome(
                feature, False, f"拿到截图了，但识别失败（{type(e).__name__}）", image=raw
            )
        return SpyOutcome(feature, True, content, image=raw)

    def _combined_parse_json(self, feature: SpyFeature, raw: bytes) -> SpyOutcome:
        """位置 / 电量这类直接回传 JSON 的项目。"""
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.error(
                f"❌ 查岗「{feature.name}」数据解析失败: {type(e).__name__} - {e}"
            )
            return SpyOutcome(feature, False, f"拿到数据了但读不懂（{type(e).__name__}）")
        if feature.key == "location":
            text = (
                f"用户当前位置:{data.get('address', '未知')}"
                f"（经度 {data.get('longitude', '?')}，纬度 {data.get('latitude', '?')}）"
            )
        elif feature.key == "battery":
            text = f"当前电量 {data.get('level', data.get('battery', '未知'))}%"
        else:
            text = json.dumps(data, ensure_ascii=False)
        return SpyOutcome(feature, True, text)

    async def _combined_stream_reply(
        self,
        event: AstrMessageEvent,
        umo: str,
        outcome: SpyOutcome,
        index: int,
        total: int,
    ) -> None:
        """流式查岗：每查完一项就问一次 LLM，并把回复发出去。"""
        prompt = render_prompt(
            self.combined_round_prompt,
            index=str(index),
            total=str(total),
            feature=outcome.name,
            result=outcome.text or "（没看到内容）",
        )
        reply = await self._combined_ask_llm(event, umo, prompt)
        if not reply:
            return
        images = [outcome.image] if (self.forward_image and outcome.image) else None
        await self._combined_send(event, reply, images)
        self._combined_remember_reply(umo, reply)

    async def _combined_batch_reply(
        self, event: AstrMessageEvent, umo: str, outcomes: list[SpyOutcome]
    ) -> None:
        """一次性查岗：所有截图一起识图，然后只问一次 LLM。"""
        raw_block = await self._combined_batch_vision(outcomes)

        for outcome in outcomes:
            if outcome.text == BATCH_FALLBACK_TEXT:
                continue  # 切不开序号，整段原文在下面单独记一条，别把占位文本记进去
            self._combined_memo(umo, outcome)
        if raw_block:
            self._combined_memo_push(
                umo,
                render_record(
                    self.memo_template,
                    "整合查岗",
                    f"识别截屏结果:{raw_block}",
                    self.memo_max_chars,
                ),
            )

        lines = [
            f"{i}. {o.name}：{o.text or '（没有内容）'}"
            for i, o in enumerate(outcomes, 1)
        ]
        result = "\n".join(lines)
        if raw_block:
            result = f"{result}\n\n识图模型对这些截图的整体描述：\n{raw_block}"
        prompt = render_prompt(
            self.combined_final_prompt, total=str(len(outcomes)), result=result
        )
        reply = await self._combined_ask_llm(event, umo, prompt)
        if not reply:
            return
        images = (
            [o.image for o in outcomes if o.image] if self.forward_image else None
        )
        await self._combined_send(event, reply, images)
        self._combined_remember_reply(umo, reply)

    async def _combined_batch_vision(self, outcomes: list[SpyOutcome]) -> str:
        """把所有截图塞进一次识图请求。

        能按序号切开就逐条写回各自的 text 并返回 ""；
        切不开就返回整段原文，由调用方整体附一次，免得重复 N 遍。
        """
        shots = [(o.name, o.image) for o in outcomes if o.image]
        if not shots:
            return ""

        def mark_all(reason: str) -> None:
            for outcome in outcomes:
                if outcome.image:
                    outcome.ok = False
                    outcome.text = reason

        if not self.vision_api_url or not self.vision_api_key:
            mark_all("拿到截图了，但视觉模型没配置，看不清内容")
            return ""
        try:
            content = await analyze_images(
                images=shots,
                api_url=self.vision_api_url,
                api_key=self.vision_api_key,
                model=self.vision_model,
                vision_prompt=self.combined_batch_prompt or None,
                timeout=self.combined_vision_timeout,
                max_tokens=self.vision_max_tokens,
            )
        except Exception as e:
            logger.error(f"❌ 整合查岗批量识图失败: {type(e).__name__} - {e}")
            mark_all(f"拿到截图了，但识别失败（{type(e).__name__}）")
            return ""

        pending = [o for o in outcomes if o.image]
        parts = split_numbered(content, len(pending))
        if parts is None:
            logger.warning("⚠️ 批量识图结果没能按序号切开，改为整段附在最后。")
            for outcome in pending:
                outcome.text = BATCH_FALLBACK_TEXT
            return content
        for outcome, text in zip(pending, parts):
            outcome.text = strip_label(text, outcome.name)
        return ""

    async def _combined_ask_llm(
        self, event: AstrMessageEvent, umo: str, prompt: str
    ) -> str:
        """直接问一次 LLM。

        走 provider.text_chat 而不是管线，所以这一问一答不会污染上下文，
        也不会被记忆插件自动抄走 —— 该记的我们自己记（见 _combined_remember_reply）。
        """
        try:
            getter = getattr(self.context, "get_using_provider_async", None)
            provider = (
                await getter(umo=umo)
                if getter is not None
                else self.context.get_using_provider(umo=umo)
            )
        except Exception as e:
            logger.error(f"❌ 整合查岗：取聊天模型失败: {type(e).__name__} - {e}")
            return ""
        if provider is None:
            logger.error("❌ 整合查岗：没有可用的聊天模型。")
            return ""

        system_prompt, contexts = await self._combined_session_context(event, umo)
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
            )
        except Exception as e:
            logger.error(f"❌ 整合查岗：调用聊天模型失败: {type(e).__name__} - {e}")
            return ""

        text = (getattr(resp, "completion_text", "") or "").strip()
        if self.combined_marker:
            text = text.replace(self.combined_marker, "").strip()
        text = strip_records(text).strip()
        if not text:
            logger.warning("⚠️ 整合查岗：聊天模型没给出文本，这一条跳过。")
        return text

    async def _combined_session_context(
        self, event: AstrMessageEvent, umo: str
    ) -> tuple[str, list[dict]]:
        """取当前会话的人设与最近几轮对话，让主动发言不至于人格分裂。

        任何一步失败都只降级，不让整个查岗挂掉。
        """
        contexts: list[dict] = []
        persona_id = None
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                conv = await self.context.conversation_manager.get_conversation(
                    umo, cid
                )
                if conv is not None:
                    persona_id = getattr(conv, "persona_id", None)
                    for item in json.loads(conv.history or "[]"):
                        if not isinstance(item, dict):
                            continue
                        if item.get("role") not in ("user", "assistant"):
                            continue
                        if item.get("tool_calls"):
                            continue
                        content = item.get("content")
                        if not isinstance(content, str) or not content.strip():
                            continue
                        contexts.append({"role": item["role"], "content": content})
                    if self.combined_history_rounds > 0:
                        contexts = contexts[-(self.combined_history_rounds * 2) :]
        except Exception as e:
            logger.warning(
                f"⚠️ 整合查岗：读会话历史失败，这次就不带历史了: {type(e).__name__} - {e}"
            )
            contexts = []

        system_prompt = ""
        try:
            provider_settings = self.context.get_config(umo=umo).get(
                "provider_settings", {}
            )
            _, persona, _, _ = (
                await self.context.persona_manager.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=persona_id,
                    platform_name=event.get_platform_name(),
                    provider_settings=provider_settings,
                )
            )
            if persona and persona["prompt"]:
                system_prompt = persona["prompt"]
        except Exception as e:
            logger.warning(
                f"⚠️ 整合查岗：取人设失败，这次就不带人设了: {type(e).__name__} - {e}"
            )
        return system_prompt, contexts

    async def _combined_send(
        self,
        event: AstrMessageEvent,
        text: str,
        images: list[bytes] | None = None,
    ) -> None:
        """用平台原生动作把主动发言发出去。

        既不碰 context.send_message，也不碰 event.send —— 和
        astrbot_plugin_reply_quote 的 [拍一拍对方] 走同一条 call_action 路。
        """
        segments: list[dict] = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        for data in images or []:
            if not data:
                continue
            encoded = base64.b64encode(data).decode("utf-8")
            segments.append(
                {"type": "image", "data": {"file": f"base64://{encoded}"}}
            )
        if not segments:
            return

        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            logger.error(
                "❌ 整合查岗：当前平台没有 call_action，主动发言发不出去"
                "（这条内容仍会补进上下文与记忆）。"
            )
            return

        params: dict = {}
        self_id = getattr(event.message_obj, "self_id", None)
        if self_id:
            params["self_id"] = self_id
        group_id = getattr(event.message_obj, "group_id", None)
        try:
            if group_id:
                await bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=segments,
                    **params,
                )
            else:
                await bot.call_action(
                    "send_private_msg",
                    user_id=int(event.get_sender_id()),
                    message=segments,
                    **params,
                )
            logger.info("📨 查岗主动发言已发送。")
        except Exception as e:
            logger.error(f"❌ 整合查岗：主动发言发送失败: {type(e).__name__} - {e}")

    # -------------------------------------------------------------- #
    # 后台查岗的记录：先攒着，等用户下次说话时挂到那一轮上              #
    # -------------------------------------------------------------- #

    def _combined_memo(self, umo: str, outcome: SpyOutcome) -> None:
        """把一项查岗结果记成一条便利贴。措辞与单个工具的记录保持一致。"""
        if not self.memo_enable or not self.memo_record_tools:
            return
        if not outcome.ok:
            detail = f"但失败了:{outcome.text}"
        elif outcome.feature.kind == "json":
            detail = outcome.text  # 位置/电量本来就是现成的话，不用「识别截屏结果」
        else:
            detail = f"识别截屏结果:{outcome.text}"
        self._combined_memo_push(
            umo,
            render_record(
                self.memo_template,
                outcome.feature.record_name,
                detail,
                self.memo_max_chars,
            ),
        )

    def _combined_remember_reply(self, umo: str, reply: str) -> None:
        """主动发言既要进上下文，也要进记忆——它不走管线，没人替我们记。"""
        self._combined_push(self._combined_pending_context, umo, reply)
        if self.memo_enable:
            self._combined_memo_push(
                umo, render_prompt(self.combined_reply_template, detail=reply)
            )

    def _combined_memo_push(self, umo: str, text: str) -> None:
        self._combined_push(self._combined_pending_records, umo, text)

    @staticmethod
    def _combined_push(bucket: dict[str, list[str]], umo: str, text: str) -> None:
        """攒一条，并且掐住上限，避免用户长期不说话时无限堆积。"""
        if not text:
            return
        items = bucket.setdefault(umo, [])
        items.append(text)
        if len(items) > COMBINED_PENDING_LIMIT:
            del items[: len(items) - COMBINED_PENDING_LIMIT]

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
            self._memo(
                event, "查看手机屏幕", "但失败了:插件配置不完整，缺少 " + "、".join(missing)
            )
            return "手机查岗失败：插件配置不完整，缺少 " + "、".join(missing) + "。请提醒用户配置后再试。"

        # 先确认接收服务是否在运行
        if self.receiver is None or not self.receiver.is_running:
            if self.auto_fallback:
                return await self._fallback(event, "截图接收服务未运行")
            self._memo(event, "查看手机屏幕", "但失败了:截图接收服务未运行")
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
            self._memo(event, "查看手机屏幕", "但失败了:触发邮件发送失败")
            return "手机查岗失败：触发邮件发送失败，请检查 SMTP 配置。"

        # 2. 创建等待条目，等待截图到达
        try:
            req_id = pending_manager.create()
        except RuntimeError as e:
            logger.warning(f"⚠️ 手机查岗请求被限流: {e}")
            if self.auto_fallback:
                return await self._fallback(event, "待处理查岗请求过多")
            self._memo(event, "查看手机屏幕", "但失败了:待处理查岗请求过多")
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
            self._memo(
                event,
                "查看手机屏幕",
                f"但失败了:等待 iPhone 截图超时（{self.timeout_seconds} 秒）",
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
            self._memo(
                event, "查看手机屏幕", "已收到截图，但视觉模型未配置，没能看清内容"
            )
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
                max_tokens=self.vision_max_tokens,
            )
        except asyncio.TimeoutError:
            if self.auto_fallback:
                return await self._fallback(event, "视觉模型分析超时")
            self._memo(event, "查看手机屏幕", "但失败了:视觉模型分析超时")
            return "手机查岗：已收到截图但视觉模型分析超时，请检查视觉模型配置。"
        except Exception as e:
            logger.error(f"❌ 视觉模型分析异常: {type(e).__name__} - {e}")
            if self.auto_fallback:
                return await self._fallback(
                    event, f"视觉模型分析失败（{type(e).__name__}）"
                )
            self._memo(
                event, "查看手机屏幕", f"但失败了:视觉模型分析失败（{type(e).__name__}）"
            )
            return "手机查岗：已收到截图但视觉模型分析失败，请检查视觉模型配置。"

        # 5. 返回给大模型
        logger.info("✅ 手机查岗完成，描述已返回给大模型。")
        self._memo(event, "查看手机屏幕", f"识别截屏结果:{content}")
        return f"手机查岗成功，用户当前的 iPhone 屏幕内容描述如下：\n{content}\n\n请根据上述内容，以你的角色人设对用户发送回复。"

    async def _fallback(self, event: AstrMessageEvent, reason: str) -> str:
        """手机查岗失败时回退到电脑查岗，返回给大模型的文本。"""
        result = await try_fallback(
            event=event,
            tool_manager=self._get_tool_manager(),
            failed_device="手机",
            error=reason,
            fallback_template=self.fallback_prompt_template,
            both_failed_template=self.both_failed_prompt_template,
        )
        self._memo(
            event,
            "查看手机屏幕",
            f"但手机没看成（{reason}），已自动改为查看电脑屏幕，结果:{result}",
        )
        return result

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

    @staticmethod
    def _parse_playlist_presets(raw) -> dict[str, str]:
        """把配置里的 ["摇滚:2829883282", ...] 解析成 {名字: 歌单ID}。

        容错：允许中英文冒号、前后空格；跳过格式不对或 ID 非数字的行。
        """
        presets: dict[str, str] = {}
        if isinstance(raw, str):
            raw = raw.splitlines()
        for line in raw or []:
            text = str(line).strip()
            if not text:
                continue
            normalized = text.replace("：", ":")
            if ":" not in normalized:
                logger.warning(f"⚠️ 预设歌单格式不正确（缺少冒号），已跳过：{text}")
                continue
            name, _, playlist_id = normalized.rpartition(":")
            name = name.strip()
            playlist_id = playlist_id.strip()
            if not name or not playlist_id.isdigit():
                logger.warning(f"⚠️ 预设歌单格式不正确（名字为空或 ID 非数字），已跳过：{text}")
                continue
            presets[name] = playlist_id
        if presets:
            logger.info(f"🎵 已加载 {len(presets)} 个预设歌单：{'、'.join(presets)}")
        return presets

    def _match_playlist(self, keyword: str) -> tuple[str, str] | tuple[None, None]:
        """按名字匹配预设歌单，返回 (名字, 歌单ID)。

        依次尝试：完全匹配 → 忽略大小写完全匹配 → 双向包含匹配。
        """
        key = (keyword or "").strip()
        if not key:
            return None, None
        if key in self.playlist_presets:
            return key, self.playlist_presets[key]
        lowered = key.lower()
        for name, pid in self.playlist_presets.items():
            if name.lower() == lowered:
                return name, pid
        for name, pid in self.playlist_presets.items():
            if lowered in name.lower() or name.lower() in lowered:
                return name, pid
        return None, None

    async def _wait_screenshot_and_analyze(
        self,
        action_name: str,
        event: AstrMessageEvent | None = None,
        feature: str | None = None,
        detail_prefix: str = "",
    ) -> str:
        """等待截图回传并用视觉模型分析，返回描述字符串。超时时返回提示文本。

        传入 event 时会顺便把结果记进记忆（feature 默认取 action_name，
        detail_prefix 用于在识别结果前补充动作信息，如「播放了《X》，」）。
        """
        feature = feature or action_name

        def memo(detail: str) -> None:
            if event is not None:
                self._memo(event, feature, f"{detail_prefix}{detail}")

        if not self.vision_api_url or not self.vision_api_key:
            memo("已发送指令，但视觉模型未配置，没能看到截图")
            return f"已发送{action_name}指令（视觉模型未配置，无法分析截图）。"
        req_id = pending_manager.create()
        try:
            image_data = await pending_manager.wait(req_id, self.timeout_seconds)
        except asyncio.TimeoutError:
            memo(f"但截图未在 {self.timeout_seconds} 秒内回传")
            return f"{action_name}：截图未在 {self.timeout_seconds} 秒内回传。"
        try:
            content = await analyze_image(
                image_data=image_data,
                api_url=self.vision_api_url,
                api_key=self.vision_api_key,
                model=self.vision_model,
                max_tokens=self.vision_max_tokens,
            )
        except Exception as e:
            logger.error(f"视觉模型分析失败: {e}")
            memo(f"已收到截图，但识别失败（{type(e).__name__}）")
            return f"{action_name}：已收到截图，但视觉模型分析失败（{type(e).__name__}）。"
        memo(f"识别截屏结果:{content}")
        return f"{action_name}完成，屏幕内容：\n{content}"

    async def _do_view_action(
        self, subject: str, action_name: str, event: AstrMessageEvent | None = None
    ) -> str:
        """查看类功能的统一流程：发指令 →（按开关）等截图并分析。"""
        await send_trigger_email(**self._smtp_kwargs(), subject=subject)
        if not self.auto_screenshot_view:
            if event is not None:
                self._memo(
                    event,
                    action_name,
                    "已打开对应页面，但「查看类自动截图」是关的，没能看到屏幕内容",
                )
            return (
                f"已让 iPhone {action_name}。当前「查看类自动截图」开关是关闭的，"
                "所以我看不到屏幕上的具体内容。"
            )
        return await self._wait_screenshot_and_analyze(action_name, event)

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
            return await self._wait_screenshot_and_analyze(
                "播放每日推荐", event, "播放每日推荐", "已开始播放每日推荐，"
            )
        self._memo(event, "播放每日推荐", "已开始播放每日推荐")
        return "已向 iPhone 发送播放每日推荐指令。"

    @llm_tool("play_netease_fm")
    async def play_netease_fm(self, event: AstrMessageEvent):
        """播放网易云音乐私人漫游（私人 FM）。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_FM")
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze(
                "播放私人漫游", event, "播放私人漫游", "已开始播放私人漫游，"
            )
        self._memo(event, "播放私人漫游", "已开始播放私人漫游")
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
            return await self._wait_screenshot_and_analyze(
                "播放红心歌单", event, "播放红心歌单", "已开始播放红心歌单，"
            )
        self._memo(event, "播放红心歌单", "已开始播放红心歌单")
        return "已向 iPhone 发送播放红心歌单指令。"

    @llm_tool("play_pause_music")
    async def play_pause_music(self, event: AstrMessageEvent):
        """播放或暂停当前正在播放的音乐（系统级媒体控制，适用于所有音乐 App）。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_PLAYPAUSE")
        self._memo(event, "播放/暂停音乐", "已发送播放/暂停指令")
        return "已向 iPhone 发送播放/暂停指令。"

    @llm_tool("play_netease_song")
    async def play_netease_song(self, event: AstrMessageEvent, song_name: str):
        """在网易云音乐播放指定歌曲。

        Args:
            song_name(string): 歌曲名称，可带歌手名，如"晴天"、"稻香 周杰伦"
        """
        try:
            song_id, display_name = await self._search_netease_song(song_name)
        except Exception as e:
            logger.error(f"搜索网易云歌曲失败: {e}")
            self._memo(
                event, "播放指定歌曲", f"但失败了:搜索《{song_name}》时网络出错"
            )
            return f"搜索歌曲《{song_name}》时网络出错，请稍后再试。"

        if song_id is None:
            self._memo(event, "播放指定歌曲", f"但失败了:没找到歌曲《{song_name}》")
            return f"未找到歌曲《{song_name}》，请换个关键词试试。"

        scheme = f"orpheus://song/{song_id}/?autoplay=1"
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_MUSIC_SONG", body=scheme)
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze(
                f"播放《{display_name}》",
                event,
                "播放指定歌曲",
                f"播放了《{display_name}》，",
            )
        self._memo(event, "播放指定歌曲", f"播放了《{display_name}》")
        return f"已向 iPhone 发送播放《{display_name}》指令。"

    @llm_tool("play_netease_playlist")
    async def play_netease_playlist(self, event: AstrMessageEvent, playlist_name: str):
        """播放用户预先设置好的网易云歌单。只能播放用户在插件配置里预设过的歌单。

        Args:
            playlist_name(string): 歌单名字，如"摇滚"、"睡前"。必须是用户预设过的歌单名
        """
        if not self.playlist_presets:
            self._memo(event, "播放预设歌单", "但失败了:用户没有预设任何歌单")
            return "用户还没有在插件配置里预设任何歌单，请提醒用户先去配置「预设歌单列表」。"

        name, playlist_id = self._match_playlist(playlist_name)
        if playlist_id is None:
            available = "、".join(self.playlist_presets)
            self._memo(
                event, "播放预设歌单", f"但失败了:没有叫「{playlist_name}」的预设歌单"
            )
            return f"没有找到叫「{playlist_name}」的预设歌单。目前可用的歌单有：{available}"

        scheme = f"orpheus://playlist/{playlist_id}/?autoplay=1"
        await send_trigger_email(
            **self._smtp_kwargs(), subject="ASTRBOT_MUSIC_LIST", body=scheme
        )
        if self.auto_screenshot_after_music:
            return await self._wait_screenshot_and_analyze(
                f"播放歌单「{name}」", event, "播放预设歌单", f"播放了歌单「{name}」，"
            )
        self._memo(event, "播放预设歌单", f"播放了歌单「{name}」")
        return f"已向 iPhone 发送播放歌单「{name}」的指令。"

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
        except asyncio.TimeoutError:
            self._memo(
                event, "查看电量", f"但失败了:iPhone 未在 {self.timeout_seconds} 秒内响应"
            )
            return f"获取电量失败：iPhone 未在 {self.timeout_seconds} 秒内响应。"
        except Exception as e:
            self._memo(event, "查看电量", f"但失败了:{type(e).__name__} - {e}")
            return f"获取电量失败：{e}"
        self._memo(event, "查看电量", f"当前电量 {level}%")
        return f"iPhone 当前电量：{level}%"

    @llm_tool("view_wechat")
    async def view_wechat(self, event: AstrMessageEvent):
        """打开 iPhone 上的微信并截图，让 AI 看到当前的微信界面。"""
        return await self._do_view_action("ASTRBOT_WECHAT", "查看微信", event)

    @llm_tool("view_alipay_bill")
    async def view_alipay_bill(self, event: AstrMessageEvent):
        """打开支付宝账单页面并截图。"""
        return await self._do_view_action("ASTRBOT_ALIPAY", "查看支付宝账单", event)

    @llm_tool("view_bilibili_history")
    async def view_bilibili_history(self, event: AstrMessageEvent):
        """打开 B 站观看历史页面并截图。"""
        return await self._do_view_action("ASTRBOT_BILIBILI", "查看 B 站历史", event)

    @llm_tool("view_douyin_messages")
    async def view_douyin_messages(self, event: AstrMessageEvent):
        """打开抖音私信页面并截图。"""
        return await self._do_view_action("ASTRBOT_DOUYIN_MSG", "查看抖音私信", event)

    @llm_tool("view_douyin_profile")
    async def view_douyin_profile(self, event: AstrMessageEvent):
        """打开抖音个人主页并截图。"""
        return await self._do_view_action(
            "ASTRBOT_DOUYIN_PROFILE", "查看抖音个人主页", event
        )

    @llm_tool("view_taobao_orders")
    async def view_taobao_orders(self, event: AstrMessageEvent):
        """打开淘宝订单页面并截图。"""
        return await self._do_view_action("ASTRBOT_TAOBAO_ORDER", "查看淘宝订单", event)

    @llm_tool("view_taobao_cart")
    async def view_taobao_cart(self, event: AstrMessageEvent):
        """打开淘宝购物车并截图。"""
        return await self._do_view_action("ASTRBOT_TAOBAO_CART", "查看淘宝购物车", event)

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
        except asyncio.TimeoutError:
            self._memo(
                event,
                "获取手机位置",
                f"但失败了:iPhone 未在 {self.timeout_seconds} 秒内响应",
            )
            return f"获取位置失败：iPhone 未在 {self.timeout_seconds} 秒内响应。"
        except Exception as e:
            self._memo(event, "获取手机位置", f"但失败了:{type(e).__name__} - {e}")
            return f"获取位置失败：{e}"
        description = (
            f"用户当前位置:{loc.get('address', '未知')}"
            f"（经度 {loc.get('longitude', '?')}，纬度 {loc.get('latitude', '?')}）"
        )
        self._memo(event, "获取手机位置", description)
        return (
            f"用户当前位置：{loc.get('address', '未知')}"
            f"（经度 {loc.get('longitude', '?')}，纬度 {loc.get('latitude', '?')}）"
        )

    @llm_tool("set_phone_alarm")
    async def set_phone_alarm(self, event: AstrMessageEvent, time: str):
        """在 iPhone 上设置一个闹钟。

        Args:
            time(string): 闹钟时间，24 小时制 HH:MM 格式，如 "07:30"、"22:00"
        """
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_ALARM", body=time)
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze(
                f"设置闹钟 {time}", event, "设置闹钟", f"设置了 {time} 的闹钟，"
            )
        self._memo(event, "设置闹钟", f"设置了 {time} 的闹钟")
        return f"已向 iPhone 发送设置闹钟 {time} 的指令。"

    @llm_tool("enable_phone_alarm")
    async def enable_phone_alarm(self, event: AstrMessageEvent, time: str):
        """开启 iPhone 上指定时间的闹钟（该时间的闹钟必须已存在，新建闹钟请用 set_phone_alarm）。

        Args:
            time(string): 闹钟时间，24 小时制 HH:MM 格式，如 "07:30"、"22:00"
        """
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_ON_ALARM", body=time)
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze(
                f"开启闹钟 {time}", event, "开启闹钟", f"开启了 {time} 的闹钟，"
            )
        self._memo(event, "开启闹钟", f"开启了 {time} 的闹钟")
        return f"已向 iPhone 发送开启 {time} 闹钟的指令。"

    @llm_tool("disable_phone_alarm")
    async def disable_phone_alarm(self, event: AstrMessageEvent, time: str):
        """关闭 iPhone 上指定时间的闹钟（该时间的闹钟必须已存在）。

        Args:
            time(string): 闹钟时间，24 小时制 HH:MM 格式，如 "07:30"、"22:00"
        """
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_OFF_ALARM", body=time)
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze(
                f"关闭闹钟 {time}", event, "关闭闹钟", f"关闭了 {time} 的闹钟，"
            )
        self._memo(event, "关闭闹钟", f"关闭了 {time} 的闹钟")
        return f"已向 iPhone 发送关闭 {time} 闹钟的指令。"

    @llm_tool("lock_phone_screen")
    async def lock_phone_screen(self, event: AstrMessageEvent):
        """锁定用户的 iPhone 屏幕。"""
        await send_trigger_email(**self._smtp_kwargs(), subject="ASTRBOT_LOCK")
        if self.auto_screenshot_after_control:
            return await self._wait_screenshot_and_analyze(
                "锁定屏幕", event, "锁定屏幕", "已锁定手机屏幕，"
            )
        self._memo(event, "锁定屏幕", "已锁定手机屏幕")
        return "已向 iPhone 发送锁定屏幕指令。"
