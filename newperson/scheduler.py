"""持久化定时任务：到点了把 Job 交给对应的 handler。

几个要点：

- **租约认领**。任务只能被抢到一次；抢到的一方拿着有期限的租约。
  进程崩在任务中间，租约过期后任务自己回到队列，不会永远卡在 running。
- **每会话单飞**。同一段对话同时只跑一个任务，免得回复和主动消息在同一个频道里交错发出。
- **失败重试**。最多三次，间隔一分钟、三分钟、九分钟。对用户表现为"这会儿没看手机"，
  绝不发任何错误文本出去。
- **过期策略**。停机很久再启动时，堆积的回复要立刻处理，但过期太久的主动消息就作废了。
  半夜想说的话第二天中午再冒出来会很怪。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock
from .memory import Memory
from .models import Job, JobKind

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[None]]

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (60, 180, 540)
LEASE_SECONDS = 300.0
IDLE_POLL_SECONDS = 60.0
"""没有任何待办时的兜底轮询间隔。正常情况下靠事件唤醒。"""

BACKLOG_AFTER = timedelta(minutes=2)
"""过期超过这么久才算"停机期间攒下来的"，才需要打散。

刚到点的任务不是积压，是正常轮到它了——那种必须立刻执行，
否则每一条回复都要平白多等几分钟。
"""

STALE_AFTER = {
    "proactive": timedelta(hours=2),
    "follow_up": timedelta(hours=6),
    "sign_off": timedelta(minutes=30),
}
"""过期这么久就作废。半夜想说的话，第二天中午再发出来会很怪。"""


class Scheduler:
    def __init__(
        self,
        memory: Memory,
        clock: Clock,
        delay_scale: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self.memory = memory
        self.clock = clock
        self.delay_scale = delay_scale
        self.rng = rng or random.Random()
        """恢复时把积压的任务打散用的。测试要复现就传一个进来。"""
        self._handlers: dict[str, Handler] = {}
        self._wake = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self._stopped = False

    # -- 注册与入队 ---------------------------------------------------------

    def register(self, kind: JobKind, handler: Handler) -> None:
        self._handlers[kind] = handler

    async def schedule(
        self,
        kind: JobKind,
        run_at: datetime,
        conversation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        reason: str = "",
    ) -> int:
        """新建任务并唤醒循环。带 ``dedupe_key`` 的重复入队返回 0。"""
        job_id = await self.memory.add_job(
            Job(
                kind=kind,
                run_at=run_at,
                conversation_id=conversation_id,
                payload=payload or {},
                dedupe_key=dedupe_key,
                reason=reason,
            ),
            self.clock.now(),
        )
        if job_id:
            log.info(
                "[job] 排上 %s#%s %s %s", kind, job_id, run_at.strftime("%m-%d %H:%M"), reason
            )
            self._wake.set()
        return job_id

    async def reschedule(
        self, job_id: int, run_at: datetime, payload: dict[str, Any] | None = None
    ) -> None:
        await self.memory.reschedule_job(job_id, run_at, payload)
        self._wake.set()

    async def cancel(self, job_id: int, reason: str = "") -> None:
        await self.memory.set_job_status(job_id, "cancelled", reason or None)
        self._wake.set()

    # -- 启动与执行 ---------------------------------------------------------

    async def recover(self) -> None:
        """启动时清理上一次没跑完的东西。"""
        revived = await self.memory.sweep_expired_leases(self.clock.now())
        if revived:
            log.info("[job] 上次崩溃遗留的 %d 个任务放回队列", revived)
        stale = await self._expire_stale_jobs()
        if stale:
            log.info("[job] %d 个过期太久的任务作废", stale)
        spread = await self._spread_overdue_jobs()
        if spread:
            log.info("[job] %d 个已经到点的任务往后挪了挪，免得一起涌出来", spread)

    async def _spread_overdue_jobs(self) -> int:
        """已经过了执行时刻的任务，别在开机后一分钟内全部涌出来。

        **这是整个系统里最容易露馅的一幕。** 部署或者机器重启要几分钟，
        期间到点的回复、日程、记忆整理会在 recover() 之后的第一轮里一起执行——
        于是她在进程起来三十秒后，回了一条你几小时前发的消息。
        没有人是这样的：一个人重新拿起手机，是过一会儿才看到的。

        往后挪的量随机，且按任务种类分开：回复要像"过一会儿才看到"，
        后台任务（日程、记忆整理）挪多久都无所谓，纯粹是让它们别挤在一起。
        """
        now = self.clock.now()
        spread = {
            "reply": (150.0, 900.0),
            "proactive": (300.0, 1800.0),
            "follow_up": (300.0, 1800.0),
        }
        moved = 0
        for job in await self.memory.pending_jobs():
            if now - job.run_at <= BACKLOG_AFTER:
                continue
            low, high = spread.get(job.kind, (30.0, 300.0))
            delay = self.rng.uniform(low, high) * max(self.delay_scale, 0.0)
            await self.memory.reschedule_job(job.id or 0, now + timedelta(seconds=delay))
            moved += 1
        return moved

    async def _expire_stale_jobs(self) -> int:
        """停机太久之后，有些任务已经没有意义了。"""
        now = self.clock.now()
        count = 0
        for job in await self.memory.pending_jobs():
            limit = STALE_AFTER.get(job.kind)
            if limit and now - job.run_at > limit:
                await self.memory.set_job_status(job.id or 0, "cancelled", "停机太久，作废")
                count += 1
        return count

    def _lock_for(self, conversation_id: str | None) -> asyncio.Lock:
        key = conversation_id or "__global__"
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def run_due_once(self) -> int:
        """把到期的任务跑一轮，返回真的执行了几个。"""
        now = self.clock.now()
        await self.memory.sweep_expired_leases(now)
        ran = 0
        for job in await self.memory.due_jobs(now):
            if self._stopped:
                break
            if await self._run_job(job):
                ran += 1
        return ran

    async def _run_job(self, job: Job) -> bool:
        job_id = job.id or 0
        lock = self._lock_for(job.conversation_id)
        if lock.locked():
            # 同一段对话已经有任务在跑，等下一轮。不能让两条消息在同一个频道里交错发出。
            return False

        async with lock:
            claimed = await self.memory.claim_job(job_id, self.clock.now(), LEASE_SECONDS)
            if claimed is None:
                return False

            handler = self._handlers.get(claimed.kind)
            if handler is None:
                log.warning("[job] %s 没有注册 handler，跳过", claimed.kind)
                await self.memory.set_job_status(job_id, "cancelled", "没有 handler")
                return False

            try:
                await handler(claimed)
            except asyncio.CancelledError:
                await self.memory.set_job_status(job_id, "pending")
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底：任何失败都不能让人物崩掉
                await self._handle_failure(claimed, exc)
                return False

            # handler 可能自己把任务重排了（暂停期间、预算用完顺延到明天）。
            # 那种情况下不能盖成 done，否则那条回复就永远发不出去了。
            current = await self.memory.get_job(job_id)
            if current is not None and current.status != "running":
                return False
            await self.memory.set_job_status(job_id, "done")
            return True

    async def _handle_failure(self, job: Job, exc: Exception) -> None:
        """失败了就当"这会儿没看手机"，过一阵再试。绝不把错误发给对方。"""
        job_id = job.id or 0
        if job.attempts >= MAX_ATTEMPTS:
            log.error("[job] %s#%s 试了 %d 次都失败：%s", job.kind, job_id, job.attempts, exc)
            await self.memory.set_job_status(job_id, "failed", str(exc)[:200])
            return

        backoff = RETRY_BACKOFF_SECONDS[min(job.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        retry_at = self.clock.now() + timedelta(seconds=backoff * self.delay_scale)
        log.warning(
            "[job] %s#%s 第 %d 次失败，%s 后重试：%s",
            job.kind,
            job_id,
            job.attempts,
            retry_at.strftime("%H:%M"),
            exc,
        )
        await self.memory.reschedule_job(job_id, retry_at)
        self._wake.set()

    # -- 主循环 -------------------------------------------------------------

    async def run_forever(self) -> None:
        """算出下一个任务什么时候到期，睡到那时候，或者被新任务唤醒。

        单个任务失败已经在 :meth:`_run_job` 里兜住了。这里再兜一层，
        是因为循环本身出意外（比如数据库暂时读不了）不能让她从此彻底不说话，
        那种故障没有任何征兆，你只会觉得她再也不理你了。
        """
        try:
            await self.recover()
        except Exception:  # noqa: BLE001 - 恢复失败也不能让循环没起来就死了
            log.exception("[job] 启动恢复出意外，继续跑")
        while not self._stopped:
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("[job] 调度循环出意外，歇一分钟再来")
                await self.clock.sleep(60)
                continue
            if self._stopped:
                break
            try:
                await self._wait_for_next()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("[job] 等待下一个任务时出意外")
                await self.clock.sleep(60)

    async def _wait_for_next(self) -> None:
        next_at = await self.memory.next_job_run_at()
        now = self.clock.now()
        if next_at is None:
            timeout = IDLE_POLL_SECONDS
        else:
            timeout = max(0.5, min((next_at - now).total_seconds(), IDLE_POLL_SECONDS))

        self._wake.clear()
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()
