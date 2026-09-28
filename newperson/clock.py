"""时钟抽象。

整个项目里不允许直接调用 ``datetime.now()``，一律通过 :class:`Clock`，
这样测试可以用 :class:`FakeClock` 把时间拨到任意时刻。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


def later(dt: datetime, delta: timedelta) -> datetime:
    """``dt`` 之后真实过去 ``delta`` 的那一刻，时区保持不变。

    带 ZoneInfo 的时刻直接加 timedelta 是**按墙钟加**：夏令时结束那一夜，
    第二个 01:05 加十分钟会回到第一个 01:15——往回走了一小时。
    任务到期是按真实时刻比的，这样排出来的任务一入库就已经过期了：
    重试十几秒内烧光、开机打散失效、答应"晚点告诉你"三分钟就说。
    **往后排任务一律用它**，别直接加。
    """
    return (dt.astimezone(UTC) + delta).astimezone(dt.tzinfo)


class Clock(Protocol):
    tz: ZoneInfo

    def now(self) -> datetime:
        """返回带 ``tz`` 时区的当前时间。"""
        ...

    async def sleep(self, seconds: float) -> None:
        """异步等待。"""
        ...


class RealClock:
    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(tz=self.tz)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """测试用：时间只在 ``advance``/``set``/``sleep`` 时前进。"""

    def __init__(self, start: datetime, tz: ZoneInfo | None = None) -> None:
        if start.tzinfo is None:
            if tz is None:
                raise ValueError("FakeClock 需要带时区的起始时间或显式 tz")
            start = start.replace(tzinfo=tz)
        self.tz = tz or start.tzinfo  # type: ignore[assignment]
        self._now = start.astimezone(self.tz)
        self.slept: list[float] = []
        """记录每次 sleep 的秒数，方便断言"等了多久"。"""

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(self.tz)

    # 往前走一律按真实时间走，跟 RealClock 一样。直接在带 ZoneInfo 的时刻上
    # 加 timedelta 是按墙钟加：夏令时结束那一夜会倒退一小时，或者整个跳过
    # 重复的那一小时——测试就永远到不了线上真会经过的那一段。

    def _forward(self, delta: timedelta) -> None:
        self._now = (self._now.astimezone(UTC) + delta).astimezone(self.tz)

    def advance(self, seconds: float = 0, **kwargs: float) -> None:
        self._forward(timedelta(seconds=seconds, **kwargs))

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds > 0:
            self._forward(timedelta(seconds=seconds))
        # 让出事件循环，避免死循环里一直不切换
        await asyncio.sleep(0)
