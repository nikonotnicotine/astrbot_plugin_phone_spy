import asyncio
from aiohttp import web
from astrbot.api import logger

# 全局等待队列
pending_screenshots: dict = {}


async def handle_screenshot(request: web.Request):
    """接收 iPhone POST 过来的截图"""
    config = request.app["config"]

    try:
        data = await request.post()
    except Exception as e:
        logger.error(f"[phone_spy] 解析 POST 数据失败: {e}")
        return web.Response(status=400, text="Bad request")

    # 校验 secret
    if data.get("secret") != config.get("webhook_secret", ""):
        logger.warning("[phone_spy] 收到请求但 secret 不匹配，拒绝")
        return web.Response(status=403, text="Invalid secret")

    # 获取图片
    image_field = data.get("image")
    if not image_field:
        logger.warning("[phone_spy] 收到请求但缺少 image 字段")
        return web.Response(status=400, text="Missing image field")

    image_data = image_field.file.read()
    logger.info(f"[phone_spy] 收到手机截图，大小: {len(image_data)} bytes")

    # 通知等待中的 check_phone_screen
    entry = pending_screenshots.get("current")
    if entry is not None:
        entry["data"] = image_data
        entry["event"].set()
    else:
        logger.warning("[phone_spy] 收到截图但没有等待中的请求，忽略")

    return web.Response(text="OK")


def setup_receiver(context, config: dict):
    """
    向 AstrBot 注册 HTTP 接收端点。
    AstrBot 通过 context.register_web_handler 注册路由。
    如果该 API 不存在则降级为独立 aiohttp 服务器。
    """
    path = config.get("webhook_path", "/phone/screenshot")
    port = config.get("webhook_port", 8080)

    # 尝试 AstrBot 原生注册方式
    try:
        context.register_web_handler(
            method="POST",
            path=path,
            handler=handle_screenshot,
            config=config,
        )
        logger.info(f"[phone_spy] 已注册接收端点到 AstrBot: POST {path}")
        return
    except AttributeError:
        pass

    # 降级：启动独立 aiohttp 服务器
    async def _start_server():
        app = web.Application()
        app["config"] = config
        app.router.add_post(path, handle_screenshot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"[phone_spy] 独立 HTTP 服务器已启动，监听 0.0.0.0:{port}{path}")

    asyncio.get_event_loop().create_task(_start_server())
