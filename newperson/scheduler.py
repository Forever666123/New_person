"""持久化定时任务：把 Job 存进 Memory，到点交给注册的 handler 执行。

- 单协程循环：取到期任务 → 标 running → 执行 → done / 失败重试（最多 3 次，间隔 1、3、9 分钟，乘 delay_scale）。
- 有新任务加入或改时间时通过 ``asyncio.Event`` 唤醒循环，不用轮询太勤。
- 启动时 ``memory.reset_running_jobs()``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .clock import Clock
from .memory import Memory
from .models import Job, JobKind

Handler = Callable[[Job], Awaitable[None]]

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (60, 180, 540)


class Scheduler:
    def __init__(self, memory: Memory, clock: Clock, delay_scale: float = 1.0) -> None:
        self.memory = memory
        self.clock = clock
        self.delay_scale = delay_scale
        self._handlers: dict[str, Handler] = {}
        self._wake = asyncio.Event()
        self._stopped = False

    def register(self, kind: JobKind, handler: Handler) -> None:
        raise NotImplementedError

    async def schedule(self, kind: JobKind, run_at: datetime, conversation_id: str | None = None,
                       payload: dict[str, Any] | None = None) -> int:
        """新建任务并唤醒循环，返回 job id。"""
        raise NotImplementedError

    async def reschedule(self, job_id: int, run_at: datetime, payload: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def cancel(self, job_id: int) -> None:
        raise NotImplementedError

    async def run_due_once(self) -> int:
        """执行所有到期任务一轮，返回执行条数。测试与 run_forever 都用它。"""
        raise NotImplementedError

    async def run_forever(self) -> None:
        """循环：算下一个任务时间 → 等待（或被唤醒）→ run_due_once。"""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
