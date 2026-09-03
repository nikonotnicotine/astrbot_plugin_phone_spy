"""调用 OpenAI 兼容的视觉模型 API 分析截图。"""
from __future__ import annotations

import base64

import aiohttp
from astrbot.api import logger

DEFAULT_VISION_PROMPT = (
    "你是一个查岗助手，请直接回复，详细描述这张手机屏幕截图的内容。"
    "务必告诉我用户现在大概在干什么（比如在刷什么 App、在和谁聊天、屏幕上的关键信息等）。"
)


async def analyze_image(
    image_data: bytes,
    api_url: str,
    api_key: str,
    model: str = "gpt-4o",
    vision_prompt: str | None = None,
    timeout: float = 60,
) -> str:
    """调用视觉模型，返回对图片的文字描述。失败时抛出异常。"""
    base64_img = base64.b64encode(image_data).decode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
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
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_img}"
                        },
                    },
                ],
            }
        ],
        "max_tokens": 1000,
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

    content = result["choices"][0]["message"]["content"]
    logger.info(f"✅ 视觉模型解析完成：{content[:200]}...")
    return content
