"""等待截图回传的请求管理。

每个查岗请求创建一个独立的等待条目（id -> PendingRequest），
接收服务收到截图后从最旧的待处理请求开始满足（FIFO），
避免并发请求时互相覆盖。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from astrbot.api import logger


MAX_PENDING_REQUESTS = 8


@dataclass
class PendingRequest:
    req_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    data: bytes | None = None
    created_at: float = field(default_factory=time.time)


class PendingManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}
        self._counter = itertools.count(1)

    def create(self) -> str:
        """创建一个等待条目，返回其请求 id。"""
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise RuntimeError(
                f"待处理查岗请求已达到上限（{MAX_PENDING_REQUESTS}）"
            )
        req_id = f"req-{next(self._counter)}"
        self._pending[req_id] = PendingRequest(req_id=req_id)
        logger.debug(f"待处理查岗请求已创建: {req_id}，当前待处理数: {len(self._pending)}")
        return req_id

    async def wait(self, req_id: str, timeout: float) -> bytes:
        """阻塞等待对应请求的截图数据，超时抛出 asyncio.TimeoutError。"""
        req = self._pending.get(req_id)
        if req is None:
            raise asyncio.TimeoutError("请求条目不存在或已被清理")
        try:
            await asyncio.wait_for(req.event.wait(), timeout=timeout)
        finally:
            # 无论成功或超时，消费完毕后都清理条目
            self.cancel(req_id)
        if req.data is None:
            raise asyncio.TimeoutError("请求已被满足但没有数据")
        return req.data

    def fulfill(self, data: bytes) -> str | None:
        """收到截图时调用。

        从最旧的待处理请求开始满足（FIFO），返回被满足的请求 id；
        如果没有待处理请求，返回 None。
        """
        if not self._pending:
            logger.warning("收到截图但没有待处理的查岗请求，已丢弃。")
            return None
        oldest_id = min(self._pending, key=lambda k: self._pending[k].created_at)
        req = self._pending[oldest_id]
        req.data = data
        req.event.set()
        logger.info(f"✅ 截图已到达，满足请求 {oldest_id}。")
        return oldest_id

    def cancel(self, req_id: str) -> None:
        if req_id in self._pending:
            del self._pending[req_id]
            logger.debug(f"待处理请求已清理: {req_id}")

    def active_count(self) -> int:
        return len(self._pending)

    def clear(self) -> None:
        """清理所有等待条目（插件重载/停用时调用）。"""
        self._pending.clear()


# 全局单例：便于 main.py 与 receiver.py 共享
pending_manager = PendingManager()
