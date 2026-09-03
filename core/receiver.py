"""截图接收 HTTP 服务（独立 aiohttp 服务器）。

设计文档中的接收服务原定使用 context.register_web_api(port=, app=)，
但 AstrBot 4.x 的真实 API 是注册到仪表盘作用域下的路由（需要仪表盘鉴权），
iPhone 快捷指令无法携带该鉴权，因此本插件改为在 webhook_port 上启动一个
独立的 aiohttp 服务器，仅监听 /webhook_path 一个路径。
"""
from __future__ import annotations

from aiohttp import web
from astrbot.api import logger

from .pending import pending_manager


class ScreenshotReceiver:
    def __init__(self, port: int, path: str, secret: str) -> None:
        self.port = port
        self.path = path
        self.secret = secret
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _extract_image(self, post: web.MultiDictProxy) -> bytes | None:
        """从 POST 表单中提取 image 字段。

        iPhone 快捷指令以 multipart/form-data 上传图片时会得到 FileField；
        也可能以字节/字符串形式提交，这里做兼容处理。
        """
        val = post.get("image")
        if val is None:
            return None
        if isinstance(val, web.FileField):
            # aiohttp 的 FileField.file 是 BytesIO
            data = val.file.read()
            return data if data else None
        if isinstance(val, (bytes, bytearray, memoryview)):
            return bytes(val) if val else None
        if isinstance(val, str):
            # 字符串形式：可能是 base64，也可能是未经正确解码的原始字节
            import base64

            try:
                return base64.b64decode(val, validate=True)
            except Exception:
                try:
                    return val.encode("latin-1")
                except Exception:
                    return None
        return None

    async def _handle(self, request: web.Request) -> web.Response:
        if request.method not in ("POST", "PUT"):
            return web.Response(status=405, text="method not allowed")

        try:
            post = await request.post()
        except Exception as e:
            logger.error(f"解析截图 POST 请求失败: {e}")
            return web.Response(status=400, text="bad request")

        # 校验密钥（防伪造请求）
        secret = post.get("secret")
        if secret != self.secret:
            logger.warning("⚠️ 收到密钥不匹配的截图请求，已拒绝。")
            return web.Response(status=403, text="forbidden")

        image_data = self._extract_image(post)
        if image_data is None or not image_data:
            logger.warning("⚠️ 收到缺少图片的截图请求，已拒绝。")
            return web.Response(status=400, text="missing image field")

        fulfilled = pending_manager.fulfill(image_data)
        if fulfilled is None:
            # 没有待处理请求，但密钥正确：仍返回 200，避免快捷指令报错重试
            logger.info("截图已接收，但没有待处理的查岗请求。")
            return web.Response(status=200, text="ok")
        return web.Response(status=200, text="ok")

    @property
    def is_running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        if self._runner is not None:
            return
        try:
            self._app = web.Application()
            self._app.router.add_route("*", self.path, self._handle)
            self._runner = web.AppRunner(self._app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner, host="0.0.0.0", port=self.port
            )
            await self._site.start()
            logger.info(
                f"📡 截图接收服务已启动: http://0.0.0.0:{self.port}{self.path}"
            )
        except OSError as e:
            logger.error(
                f"❌ 截图接收服务启动失败（端口 {self.port} 可能被占用）: {e}"
            )
            self._runner = None
            self._site = None
            self._app = None

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
            logger.info("📡 截图接收服务已停止。")
