"""作息：任一时刻处于 sleeping / busy / free / winding_down 哪个状态。

规则见 DESIGN.md 2.1。所有输入输出都是带时区的 datetime；内部按 persona 时区解释 HH:MM。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .models import RhythmSnapshot
from .persona import DaySchedule, RhythmConfig


class Rhythm:
    def __init__(self, config: RhythmConfig, tz: ZoneInfo) -> None:
        self.config = config
        self.tz = tz

    def schedule_for(self, day: date) -> DaySchedule:
        """某一天用工作日还是周末的作息（周六、周日算周末）。"""
        raise NotImplementedError

    def sleep_window_containing(self, dt: datetime) -> tuple[datetime, datetime] | None:
        """若 dt 处于某个睡眠区间，返回 (入睡时间, 起床时间)；否则 None。

        睡眠区间按"入睡那天"的作息决定（周五 23:30 入睡到周六早上，按工作日作息）。
        跨午夜必须正确。
        """
        raise NotImplementedError

    def next_sleep_start(self, dt: datetime) -> datetime:
        """dt 之后（含 dt）下一次入睡时间。"""
        raise NotImplementedError

    def next_wake(self, dt: datetime) -> datetime:
        """dt 之后下一次起床时间；若 dt 正在睡，则是这一觉的起床时间。"""
        raise NotImplementedError

    def state_at(self, dt: datetime) -> RhythmSnapshot:
        """核心：返回 dt 时刻的作息快照（状态、起止、下次起床/入睡、busy 标题）。"""
        raise NotImplementedError

    def is_sleeping(self, dt: datetime) -> bool:
        return self.state_at(dt).state == "sleeping"
