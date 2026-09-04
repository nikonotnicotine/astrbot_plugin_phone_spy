"""等待截图回传的请求管理。

每个查岗请求创建一个独立的等待条目（id -> PendingRequest），
接收服务收到截图后从最旧的待处理请求开始满足（FIFO），
避免并发请求时互相覆盖。

「迟到截图」的专门处理（整合查岗用）
------------------------------------
iPhone 回传截图时没法带上「这是第几张」，插件只能按 FIFO 认领。整合查岗是
一项接一项连着跑的，如果第 1 项超时了、它的截图又慢慢悠悠回来了，按 FIFO
就会被当成第 2 项的截图，张冠李戴。

所以带 `guard_late=True` 创建的请求超时后会留下一张「迟到条」(LateGuard)，
之后到达的第一张图会被丢弃 —— 但只在它「不可能属于当前请求」时才丢：
即当前没有等待中的请求，或当前请求刚发出去还不到 LATE_MIN_ROUND_TRIP 秒
（邮件推送 + 快捷指令截屏 + 上传不可能这么快）。迟到条本身也有有效期，
过期自动作废，避免误伤后面的正常截图。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from astrbot.api import logger


MAX_PENDING_REQUESTS = 8
# 从发触发邮件到截图回传的最短现实耗时。比这更快到达的图，不可能属于当前请求。
LATE_MIN_ROUND_TRIP = 8.0


@dataclass
class PendingRequest:
    req_id: str
    seq: int = 0
    label: str = ""
    guard_late: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)
    data: bytes | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class LateGuard:
    """一张「迟到条」：某个已超时请求的截图可能还在路上。"""

    seq: int
    label: str
    expire_at: float


class PendingManager:
    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}
        self._counter = itertools.count(1)
        self._late_guards: list[LateGuard] = []

    def create(self, label: str = "", guard_late: bool = False) -> str:
        """创建一个等待条目，返回其请求 id。

        Args:
            label: 这次查岗的项目名，只用于日志与迟到条的可读性
            guard_late: 超时后是否留一张迟到条，把迟到的截图丢掉（整合查岗用）

        """
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise RuntimeError(
                f"待处理查岗请求已达到上限（{MAX_PENDING_REQUESTS}）"
            )
        seq = next(self._counter)
        req_id = f"req-{seq}"
        self._pending[req_id] = PendingRequest(
            req_id=req_id, seq=seq, label=label, guard_late=guard_late
        )
        logger.debug(
            f"待处理查岗请求已创建: {req_id}（{label or '未命名'}），"
            f"当前待处理数: {len(self._pending)}"
        )
        return req_id

    async def wait(
        self, req_id: str, timeout: float, late_grace: float | None = None
    ) -> bytes:
        """阻塞等待对应请求的截图数据，超时抛出 asyncio.TimeoutError。

        Args:
            late_grace: 迟到条的有效期（秒），默认与 timeout 相同。仅对
                `guard_late=True` 的请求有意义。

        """
        req = self._pending.get(req_id)
        if req is None:
            raise asyncio.TimeoutError("请求条目不存在或已被清理")
        try:
            await asyncio.wait_for(req.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            if req.guard_late:
                self._arm_late_guard(
                    req, timeout if late_grace is None else late_grace
                )
            raise
        finally:
            # 无论成功或超时，消费完毕后都清理条目
            self.cancel(req_id)
        if req.data is None:
            raise asyncio.TimeoutError("请求已被满足但没有数据")
        return req.data

    def fulfill(self, data: bytes) -> str | None:
        """收到截图时调用。

        从最旧的待处理请求开始满足（FIFO），返回被满足的请求 id；
        如果没有待处理请求、或这张图被判定为迟到件，返回 None。
        """
        if self._consume_late_guard():
            return None
        if not self._pending:
            logger.warning("收到截图但没有待处理的查岗请求，已丢弃。")
            return None
        oldest_id = min(self._pending, key=lambda k: self._pending[k].created_at)
        req = self._pending[oldest_id]
        req.data = data
        req.event.set()
        logger.info(
            f"✅ 截图已到达，满足请求 {oldest_id}（{req.label or '未命名'}）。"
        )
        return oldest_id

    def cancel(self, req_id: str) -> None:
        if req_id in self._pending:
            del self._pending[req_id]
            logger.debug(f"待处理请求已清理: {req_id}")

    def active_count(self) -> int:
        return len(self._pending)

    def clear(self) -> None:
        """清理所有等待条目与迟到条（插件重载/停用时调用）。"""
        self._pending.clear()
        self._late_guards.clear()

    # -------------------------------------------------------------- #
    # 迟到截图的专门处理                                                #
    # -------------------------------------------------------------- #

    def _arm_late_guard(self, req: PendingRequest, grace: float) -> None:
        self._late_guards.append(
            LateGuard(seq=req.seq, label=req.label, expire_at=time.time() + grace)
        )
        logger.warning(
            f"⏱️ 第 {req.seq} 号查岗请求（{req.label or '未命名'}）已超时。"
            f"接下来 {grace:.0f} 秒内若有截图赶到，会被当作它的迟到件丢弃，"
            "避免被后面的项目误领。"
        )

    def _consume_late_guard(self) -> bool:
        """刚到的这张图是不是「已超时请求的迟到件」？是则消费迟到条并返回 True。"""
        now = time.time()
        while self._late_guards and self._late_guards[0].expire_at <= now:
            stale = self._late_guards.pop(0)
            logger.debug(
                f"迟到条已过期作废: 第 {stale.seq} 号（{stale.label or '未命名'}）"
            )
        if not self._late_guards:
            return False

        if self._pending:
            # fulfill() 会认领最旧的那个请求，就拿它来判断时间是否说得通
            target = min(self._pending.values(), key=lambda r: r.created_at)
            if now - target.created_at >= LATE_MIN_ROUND_TRIP:
                # 当前请求已经发出足够久，这张图更像是它的：正常认领，顺手作废迟到条
                dropped = self._late_guards.pop(0)
                logger.info(
                    f"第 {dropped.seq} 号（{dropped.label or '未命名'}）的迟到件一直没来，"
                    f"迟到条作废，这张图判给 {target.label or target.req_id}。"
                )
                return False

        guard = self._late_guards.pop(0)
        logger.warning(
            f"🗑️ 丢弃迟到的截图/数据：它属于已超时的第 {guard.seq} 号查岗请求"
            f"（{guard.label or '未命名'}），不能算作当前项目的。"
        )
        return True


# 全局单例：便于 main.py 与 receiver.py 共享
pending_manager = PendingManager()
