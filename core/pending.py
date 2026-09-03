import asyncio
from astrbot.api import logger


async def wait_for_screenshot(timeout: int) -> bytes:
    """
    阻塞等待手机截图上传，超时抛出 asyncio.TimeoutError。

    每次调用占用全局 "current" 槽位；如果有并发调用，
    后一个会覆盖前一个的 event，前一个会超时。
    """
    from .receiver import pending_screenshots

    event = asyncio.Event()
    pending_screenshots["current"] = {
        "event": event,
        "data": None,
    }

    try:
        logger.info(f"[phone_spy] 等待手机截图上传（超时 {timeout}s）...")
        await asyncio.wait_for(event.wait(), timeout=timeout)
        data = pending_screenshots["current"]["data"]
        logger.info("[phone_spy] 截图已收到")
        return data
    except asyncio.TimeoutError:
        logger.warning(f"[phone_spy] 等待截图超时（{timeout}s）")
        raise
    finally:
        pending_screenshots.pop("current", None)
