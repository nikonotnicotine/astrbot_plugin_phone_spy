import base64
import aiohttp
from astrbot.api import logger


async def analyze_screenshot(
    image_data: bytes,
    api_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int = 60,
) -> str:
    """
    调用 OpenAI 兼容的视觉 API 分析图片，返回描述文字。
    """
    image_base64 = base64.b64encode(image_data).decode("utf-8")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        },
                    },
                ],
            }
        ],
        "max_tokens": 1000,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logger.info(f"[phone_spy] 请求视觉模型 {model} 分析截图...")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            api_url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"[phone_spy] 视觉模型请求失败 {resp.status}: {error_text}")
                raise RuntimeError(f"视觉模型返回 {resp.status}")
            result = await resp.json()

    description = result["choices"][0]["message"]["content"]
    logger.info(f"[phone_spy] 视觉模型解析完成：{description[:80]}...")
    return description
