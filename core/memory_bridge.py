"""把工具调用与图片识别结果喂给外部记忆插件（不修改那些插件本身）。

背景
----
AstrBot 的 `on_llm_request` / `on_llm_response` 钩子包在整个 Agent 循环外面，
一轮对话只触发一次，工具调用发生在循环内部，只走 `OnUsingLLMToolEvent` /
`OnLLMToolRespondEvent`。而 astrbot_plugin_conversation_logger 与
astrbot_plugin_romantic_memory 都只挂了 request/response 钩子，所以它们永远
看不到工具做了什么。

做法（"便利贴"）
----------------
1. 工具执行时把一条记录挂到 event 上（`add_record`）。
2. 用一个 priority 极高的 `on_llm_response` 钩子，把记录临时拼进
   `LLMResponse.completion_text`。
3. 记忆插件（priority 10 / 0）照常抄写，于是连记录一起抄走了。
4. 用一个 priority 极低的 `on_llm_response` 钩子把原文还原。

为什么用户和 LLM 都看不到这些记录：
- 用户看到的消息在所有 on_llm_response 钩子跑完之后才拼装
  （tool_loop_agent_runner.py 中 `_complete_with_assistant_response()` 在第 902 行，
  `MessageChain().message(llm_resp.completion_text)` 在第 920 行）；
- LLM 的下一轮上下文来自 `run_context.messages`，其中的 `TextPart` 在钩子触发
  之前就已经用原文构造好了（同文件第 195~197 行），改 `completion_text` 不会动它。

注意：记录一律压成单行。conversation_logger 的日志是按行组织的，多行会破坏格式。
"""
from __future__ import annotations

import asyncio
import base64
import re

from astrbot.api import logger

# 挂在 event 上的键
MEMO_KEY = "_phone_spy_memo"  # list[str]，待写入记忆的记录
ORIG_KEY = "_phone_spy_memo_orig"  # str，被临时替换掉的原始回复
IMAGE_TASK_KEY = "_phone_spy_memo_image_task"  # asyncio.Task，用户图片识别任务

DEFAULT_TOOL_TEMPLATE = "[系统提示:你操控用户的手机使用了{{feature}}功能，{{detail}}]"
DEFAULT_USER_IMAGE_TEMPLATE = "[系统提示:用户发送了一张图片，识别结果:{{detail}}]"
DEFAULT_USER_IMAGE_PROMPT = (
    "请用简洁的中文客观描述这张图片的内容，包括画面主体、场景、"
    "以及图上可见的关键文字信息。不要臆测画面之外的事情，也不要加评论。"
)

# 兜底清理用：匹配一整行的 [系统提示:...]
_RECORD_LINE_RE = re.compile(r"^\[系统提示:[^\n]*\]\n?", re.MULTILINE)


def _one_line(text: str) -> str:
    """把多行文本压成单行，避免破坏 conversation_logger 的按行日志格式。"""
    return re.sub(r"\s*\n+\s*", " ", str(text or "").strip())


def render_record(
    template: str, feature: str, detail: str, max_detail_chars: int = 0
) -> str:
    """按模板渲染一条记录。`max_detail_chars` 大于 0 时截断 detail。"""
    detail = _one_line(detail)
    if max_detail_chars > 0 and len(detail) > max_detail_chars:
        detail = detail[:max_detail_chars] + "…"
    return (
        str(template or DEFAULT_TOOL_TEMPLATE)
        .replace("{{feature}}", _one_line(feature))
        .replace("{{detail}}", detail)
    )


def add_record(event, text: str) -> None:
    """把一条记录挂到 event 上，等 on_llm_response 时统一注入。"""
    if not text:
        return
    records = event.get_extra(MEMO_KEY)
    if not isinstance(records, list):
        records = []
        event.set_extra(MEMO_KEY, records)
    records.append(text)
    logger.debug(f"[PhoneSpy Memory] 记录待注入: {text}")


def get_records(event) -> list[str]:
    records = event.get_extra(MEMO_KEY)
    return list(records) if isinstance(records, list) else []


def clear_records(event) -> None:
    event.set_extra(MEMO_KEY, None)


def strip_records(text: str) -> str:
    """从文本里删掉所有 [系统提示:...] 行（兜底用）。"""
    return _RECORD_LINE_RE.sub("", str(text or "")).strip()


async def describe_user_images(
    images,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float,
    max_images: int = 3,
    max_tokens: int = 4096,
) -> list[str]:
    """用视觉模型描述用户在聊天里发的图片，返回描述列表。

    单张图片失败只跳过那一张，不影响其它图片。
    """
    from .vision import analyze_image

    results: list[str] = []
    for index, component in enumerate(images[:max_images]):
        try:
            b64 = await component.convert_to_base64()
            image_data = base64.b64decode(b64)
        except Exception as e:
            logger.warning(
                f"[PhoneSpy Memory] 第 {index + 1} 张用户图片读取失败: "
                f"{type(e).__name__} - {e}"
            )
            continue
        try:
            content = await analyze_image(
                image_data=image_data,
                api_url=api_url,
                api_key=api_key,
                model=model,
                vision_prompt=prompt or DEFAULT_USER_IMAGE_PROMPT,
                timeout=timeout,
                max_tokens=max_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"[PhoneSpy Memory] 第 {index + 1} 张用户图片识别失败: "
                f"{type(e).__name__} - {e}"
            )
            continue
        if content:
            results.append(content)
    return results
