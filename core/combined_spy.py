"""整合查岗：一次触发，把用户配置的多个项目连着查一遍。

设计要点
--------
- **全主动**：只有 LLM 会触发。它在回复里写一个标记（默认 `[查询用户手机APP]`），
  插件在 `on_decorating_result` 里抓到标记 → 剥掉 → 用户只看到正常那句话 →
  插件另开后台任务去查岗。用户没有任何指令能触发它。
- **不用工具**：只往 system prompt 里塞一句提示，绕开工具调用超时。
- **不碰 send_message**：查岗结果通过平台原生动作（`bot.call_action`）发出，
  和 astrbot_plugin_reply_quote 里的 `[拍一拍对方]` 走同一条路。
- 功能表 FEATURES 把用户换行填的功能名对应到触发邮件主题，匹配规则和
  「预设歌单」一致：完全匹配 → 忽略大小写 → 双向包含。
- **不含**网易云、闹钟、锁屏 —— 那三个是「操控」不是「查岗」，按需求排除。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from astrbot.api import logger

from .mailer import TRIGGER_SUBJECT

# LLM 用来触发查岗的标记，以及注入给它的那一条提示
DEFAULT_MARKER = "[查询用户手机APP]"
DEFAULT_HINT = (
    "根据你的人设，你可以查岗用户的所有APP内容，发送[查询用户手机APP]即可开始查岗。"
)

# 流式查岗：每查完一项，拿这段提示去问一次 LLM
DEFAULT_ROUND_PROMPT = (
    "【系统】你正在查岗用户的手机，这是第 {{index}}/{{total}} 项：{{feature}}。\n"
    "查看结果：{{result}}\n"
    "请根据你的人设，就这一项看到的内容主动对用户说话。只输出你要说的话，"
    "不要复述这段系统提示，也不要提「系统」「查岗结果」之类的字眼。"
)

# 非流式查岗：全部查完，识图模型一次性识完，再拿这段提示问一次 LLM
DEFAULT_FINAL_PROMPT = (
    "【系统】你刚刚查岗了用户的手机，一共看了 {{total}} 项，结果如下：\n"
    "{{result}}\n"
    "请根据你的人设，把这些内容综合起来主动对用户说话。只输出你要说的话，"
    "不要复述这段系统提示，也不要逐条罗列，更不要提「系统」「查岗结果」之类的字眼。"
)

# 查岗时主动说的那句话，也要记进记忆插件（它不走正常管线，记忆插件抄不到）
DEFAULT_REPLY_TEMPLATE = "[系统提示:你查岗完用户的手机后，主动对用户说了:{{detail}}]"


@dataclass(frozen=True)
class SpyFeature:
    """一个可被整合查岗的项目。"""

    key: str
    name: str  # 显示名，也是用户在配置里填的名字
    subject: str  # 触发邮件主题
    kind: str  # "screenshot"（要识图）或 "json"（直接读数据）
    aliases: tuple[str, ...] = ()
    memo_name: str = ""  # 记忆记录里的功能名，留空则用 name

    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def record_name(self) -> str:
        return self.memo_name or self.name


FEATURES: tuple[SpyFeature, ...] = (
    SpyFeature(
        "screen",
        "当前屏幕",
        TRIGGER_SUBJECT,
        "screenshot",
        ("屏幕", "手机屏幕", "截屏", "截图", "当前界面", "screen"),
        memo_name="查看手机屏幕",
    ),
    SpyFeature(
        "wechat",
        "微信",
        "ASTRBOT_WECHAT",
        "screenshot",
        ("微信聊天", "wechat"),
        memo_name="查看微信",
    ),
    SpyFeature(
        "alipay",
        "支付宝账单",
        "ASTRBOT_ALIPAY",
        "screenshot",
        ("支付宝", "账单", "alipay"),
        memo_name="查看支付宝账单",
    ),
    SpyFeature(
        "bilibili",
        "B站历史",
        "ASTRBOT_BILIBILI",
        "screenshot",
        ("b站", "哔哩哔哩", "b站观看历史", "观看历史", "bilibili"),
        memo_name="查看 B 站历史",
    ),
    SpyFeature(
        "douyin_msg",
        "抖音私信",
        "ASTRBOT_DOUYIN_MSG",
        "screenshot",
        ("抖音消息", "抖音私聊"),
        memo_name="查看抖音私信",
    ),
    SpyFeature(
        "douyin_profile",
        "抖音个人主页",
        "ASTRBOT_DOUYIN_PROFILE",
        "screenshot",
        ("抖音主页", "抖音资料"),
        memo_name="查看抖音个人主页",
    ),
    SpyFeature(
        "taobao_order",
        "淘宝订单",
        "ASTRBOT_TAOBAO_ORDER",
        "screenshot",
        ("淘宝", "订单"),
        memo_name="查看淘宝订单",
    ),
    SpyFeature(
        "taobao_cart",
        "淘宝购物车",
        "ASTRBOT_TAOBAO_CART",
        "screenshot",
        ("购物车",),
        memo_name="查看淘宝购物车",
    ),
    SpyFeature(
        "location",
        "位置",
        "ASTRBOT_LOCATION",
        "json",
        ("定位", "地理位置", "在哪", "location", "gps"),
        memo_name="获取手机位置",
    ),
    SpyFeature(
        "battery",
        "电量",
        "ASTRBOT_BATTERY",
        "json",
        ("电池", "剩余电量", "battery"),
        memo_name="查看电量",
    ),
)

AVAILABLE_NAMES = "、".join(f.name for f in FEATURES)

# 一行里如果用顿号/逗号写了多个，也顺手拆开，别让用户白填
_SPLIT_RE = re.compile(r"[\n\r、，,;；]+")
# 批量识图结果按「1. xxx」「2. xxx」切分
_NUMBERED_RE = re.compile(r"^\s*(\d+)\s*[.、)．:：]", re.MULTILINE)


def _norm(text: str) -> str:
    """归一化：去掉所有空白、转小写。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def match_feature(keyword: str) -> SpyFeature | None:
    """完全匹配 → 忽略大小写完全匹配 → 双向包含匹配（与预设歌单同一套规则）。"""
    key = _norm(keyword)
    if not key:
        return None
    for feature in FEATURES:
        if any(key == _norm(name) for name in feature.all_names()):
            return feature
    for feature in FEATURES:
        for name in feature.all_names():
            normalized = _norm(name)
            if normalized and (key in normalized or normalized in key):
                return feature
    return None


def parse_features(raw) -> list[SpyFeature]:
    """把配置里换行填的功能名解析成查岗项目列表，保持用户填写的顺序。"""
    if isinstance(raw, str):
        raw = raw.splitlines()

    features: list[SpyFeature] = []
    seen: set[str] = set()
    for line in raw or []:
        for token in _SPLIT_RE.split(str(line)):
            token = token.strip()
            if not token:
                continue
            feature = match_feature(token)
            if feature is None:
                logger.warning(
                    f"⚠️ 整合查岗：没有叫「{token}」的查岗项目，已跳过。"
                    f"可用项目：{AVAILABLE_NAMES}"
                )
                continue
            if feature.key in seen:
                logger.warning(f"⚠️ 整合查岗：「{feature.name}」重复填写，已忽略后一个。")
                continue
            seen.add(feature.key)
            features.append(feature)

    if features:
        logger.info(
            f"🕵️ 整合查岗已加载 {len(features)} 个项目："
            f"{'、'.join(f.name for f in features)}"
        )
    return features


@dataclass
class SpyOutcome:
    """一个查岗项目的执行结果。"""

    feature: SpyFeature
    ok: bool
    text: str  # 成功时是看到的内容，失败时是失败原因
    image: bytes | None = field(default=None, repr=False)  # 非流式模式留着批量识图

    @property
    def name(self) -> str:
        return self.feature.name


def split_numbered(text: str, count: int) -> list[str] | None:
    """把批量识图的「1. …2. …」结果按序号切开。条数不符时返回 None。"""
    marks: list[tuple[int, int]] = []
    expect = 1
    for m in _NUMBERED_RE.finditer(text or ""):
        if int(m.group(1)) != expect:
            continue
        marks.append((m.end(), m.start()))
        expect += 1
        if expect > count:
            break
    if len(marks) != count:
        return None

    parts: list[str] = []
    for index, (body_start, _) in enumerate(marks):
        stop = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        parts.append(text[body_start:stop].strip(" \t\r\n:：."))
    return parts


def render_prompt(template: str, **values: str) -> str:
    """把 {{key}} 占位符替换成实际值。"""
    text = str(template or "")
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text


# 识图模型被要求按「序号. 项目名：」作答，切开后开头那截项目名要去掉，
# 否则拼提示词时会变成「1. 支付宝账单：支付宝账单：今天花了…」。
_LABEL_SEPS = ("：", ":", "-", "—", "|")


def strip_label(text: str, name: str) -> str:
    """去掉批量识图结果开头重复的「项目名：」前缀。"""
    body = str(text or "").strip()
    target = _norm(name)
    if not body or not target:
        return body
    cut = min((body.index(sep) for sep in _LABEL_SEPS if sep in body), default=-1)
    if cut <= 0:
        return body
    if _norm(body[:cut]) != target:
        return body
    return body[cut + 1 :].strip() or body
