"""调用 OpenAI 兼容的视觉模型 API 分析截图。"""
from __future__ import annotations

import base64

import aiohttp
from astrbot.api import logger

DEFAULT_VISION_PROMPT = (
    "你是一个查岗助手，请直接回复，详细描述这张手机屏幕截图的内容。"
    "务必告诉我用户现在大概在干什么（比如在刷什么 App、在和谁聊天、屏幕上的关键信息等）。"
)

# 整合查岗（非流式）一次性识多张图时用。{{count}} 是张数，{{items}} 是带序号的项目清单。
DEFAULT_BATCH_PROMPT = (
    "你是一个查岗助手。下面按顺序给你 {{count}} 张手机屏幕截图，分别对应这些查岗项目：\n"
    "{{items}}\n"
    "请严格按这个顺序逐条描述，每一条都以「序号. 项目名：」开头，"
    "说清屏幕上的关键信息，以及用户在这一项里大概在干什么。"
    "不要臆测画面之外的事情，也不要加评论。"
)


def _image_part(image_data: bytes) -> dict:
    """把图片字节转成 OpenAI 视觉接口的 image_url 内容块。"""
    base64_img = base64.b64encode(image_data).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"},
    }


async def _chat(api_url: str, api_key: str, payload: dict, timeout: float) -> str:
    """发一次 chat/completions 请求，返回回复文本。失败时抛出异常。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            api_url, headers=headers, json=payload, timeout=timeout
        ) as resp:
            if resp.status != 200:
                error_info = await resp.text()
                logger.error(f"视觉模型请求出错 {error_info}")
                raise RuntimeError(f"vision api returned {resp.status}")
            result = await resp.json()
    return result["choices"][0]["message"]["content"]


async def analyze_image(
    image_data: bytes,
    api_url: str,
    api_key: str,
    model: str = "gpt-4o",
    vision_prompt: str | None = None,
    timeout: float = 60,
    max_tokens: int = 4096,
) -> str:
    """调用视觉模型，返回对图片的文字描述。失败时抛出异常。"""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": vision_prompt or DEFAULT_VISION_PROMPT,
                    },
                    _image_part(image_data),
                ],
            }
        ],
        "max_tokens": max_tokens,
    }

    content = await _chat(api_url, api_key, payload, timeout)
    logger.info(f"✅ 视觉模型解析完成：{content[:200]}...")
    return content


async def analyze_images(
    images: list[tuple[str, bytes]],
    api_url: str,
    api_key: str,
    model: str = "gpt-4o",
    vision_prompt: str | None = None,
    timeout: float = 120,
    max_tokens: int = 4096,
) -> str:
    """一次请求里塞进多张截图，返回逐条描述。失败时抛出异常。

    Args:
        images: [(项目名, 图片字节), ...]，顺序即描述顺序
        vision_prompt: 提示词模板，可用变量 {{count}} 与 {{items}}

    """
    if not images:
        raise ValueError("没有可分析的截图")

    items = "\n".join(f"{i}. {label}" for i, (label, _) in enumerate(images, 1))
    text = (
        (vision_prompt or DEFAULT_BATCH_PROMPT)
        .replace("{{count}}", str(len(images)))
        .replace("{{items}}", items)
    )
    content: list[dict] = [{"type": "text", "text": text}]
    content.extend(_image_part(data) for _, data in images)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
    }

    result = await _chat(api_url, api_key, payload, timeout)
    logger.info(
        f"✅ 视觉模型批量解析完成（{len(images)} 张）：{result[:200]}..."
    )
    return result
