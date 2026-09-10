"""作息：每天抽一次签，而不是照表执行。

核心想法：真人的作息有中心倾向，但没有精确规律。所以这里
**不存在**"每天 23:30 睡觉"这样的常量。每一天会：

1. 按 ``rhythm.variants`` 的权重抽一个当日变体（普通 / 熬夜 / 忙 / 消失 …）。
2. 从分布里采样这一晚的入睡和起床时刻。
3. 按 ``probability`` 决定今天每节课是不是真的去了。

抽签用的是 ``(persona.seed, 日期)`` 派生的随机数，所以同一天反复查询结果一致，
重启进程也一致，但不同的日子互不相关。

对外主要提供三个东西：

- :meth:`Rhythm.state_at` 某一刻处于什么状态
- :meth:`Rhythm.activity_at` 某一刻"会看手机"的活跃度
- :meth:`Rhythm.next_glance_after` 下一次瞄手机是什么时候
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import ClassInstance, DailyRhythm, RhythmSnapshot
from .persona import DayVariant, RhythmConfig, hhmm_to_minutes

_SEARCH_LIMIT = 400
"""向前搜索的最大步数，防止配置写错时死循环。"""


class Rhythm:
    def __init__(self, config: RhythmConfig, tz: ZoneInfo, seed: int = 0) -> None:
        self.config = config
        self.tz = tz
        self.seed = seed
        self._cache: dict[date, DailyRhythm] = {}

    # -- 抽签 ---------------------------------------------------------------

    def _rng_for(self, day: date) -> random.Random:
        """同一天永远得到同一个随机序列。"""
        return random.Random(self.seed * 1_000_003 + day.toordinal())

    def _at(self, day: date, minutes: float) -> datetime:
        """把"当天第 N 分钟"变成带时区的时刻，允许跨日。"""
        base = datetime.combine(day, time(0, 0), tzinfo=self.tz)
        return base + timedelta(minutes=minutes)

    def for_day(self, day: date) -> DailyRhythm:
        """返回这一天抽出来的作息。结果会缓存。"""
        cached = self._cache.get(day)
        if cached is not None:
            return cached

        rng = self._rng_for(day)
        cfg = self.config

        # 1. 当日变体
        variants = cfg.variants
        if variants:
            total = sum(v.weight for v in variants)
            roll = rng.uniform(0, total)
            acc = 0.0
            chosen = variants[-1]
            for v in variants:
                acc += v.weight
                if roll <= acc:
                    chosen = v
                    break
        else:  # 没配变体就等于每天都一样
            chosen = DayVariant(name="普通")

        # 2. 这一晚的入睡与起床
        sleep_median = hhmm_to_minutes(cfg.sleep.start_median)
        wake_median = hhmm_to_minutes(cfg.sleep.wake_median)
        # 入睡时刻若早于起床时刻（如 01:20 < 08:40），说明是躺到了次日凌晨
        sleep_offset = sleep_median + (24 * 60 if sleep_median < wake_median else 0)

        sleep_minutes = (
            sleep_offset
            + rng.gauss(0, cfg.sleep.start_sigma_minutes)
            + chosen.sleep_start_shift_hours * 60
        )
        wake_minutes = (
            24 * 60
            + wake_median
            + rng.gauss(0, cfg.sleep.wake_sigma_minutes)
            + chosen.wake_shift_hours * 60
        )

        # 睡眠时长夹到合理范围内
        duration = wake_minutes - sleep_minutes
        low, high = cfg.sleep.min_hours * 60, cfg.sleep.max_hours * 60
        if duration < low:
            wake_minutes = sleep_minutes + low
        elif duration > high:
            wake_minutes = sleep_minutes + high

        # 3. 今天真的去了的课
        classes: list[ClassInstance] = []
        for block in cfg.classes:
            if day.weekday() not in block.days:
                continue
            if rng.random() > block.probability:
                continue
            classes.append(
                ClassInstance(
                    start=self._at(day, hhmm_to_minutes(block.start)),
                    end=self._at(day, hhmm_to_minutes(block.end)),
                    title=block.title,
                )
            )

        daily = DailyRhythm(
            day=day,
            variant=chosen.name,
            variant_note=chosen.note,
            night_sleep_start=self._at(day, sleep_minutes),
            night_sleep_end=self._at(day, wake_minutes),
            activity_multiplier=chosen.activity_multiplier,
            reply_probability=(
                chosen.reply_probability
                if chosen.reply_probability is not None
                else cfg.reply_probability
            ),
            classes=classes,
        )
        self._cache[day] = daily
        return daily

    # -- 查询 ---------------------------------------------------------------

    def wake_of(self, day: date) -> datetime:
        """``day`` 这一天的起床时刻（由前一晚那一觉决定）。"""
        return self.for_day(day - timedelta(days=1)).night_sleep_end

    def logical_day(self, dt: datetime) -> date:
        """凌晨还没睡的时间算作前一天。用来取当日变体。"""
        d = dt.astimezone(self.tz).date()
        return d - timedelta(days=1) if dt < self.wake_of(d) else d

    def daily_for(self, dt: datetime) -> DailyRhythm:
        return self.for_day(self.logical_day(dt))

    def sleep_window_containing(self, dt: datetime) -> tuple[datetime, datetime] | None:
        """若 dt 处于某一觉之中，返回 ``(入睡, 起床)``；否则 None。"""
        d = dt.astimezone(self.tz).date()
        for offset in (-1, 0):
            daily = self.for_day(d + timedelta(days=offset))
            if daily.night_sleep_start <= dt < daily.night_sleep_end:
                return daily.night_sleep_start, daily.night_sleep_end
        return None

    def is_sleeping(self, dt: datetime) -> bool:
        return self.sleep_window_containing(dt) is not None

    def class_containing(self, dt: datetime) -> ClassInstance | None:
        for block in self.daily_for(dt).classes:
            if block.start <= dt < block.end:
                return block
        return None

    def activity_at(self, dt: datetime) -> float:
        """此刻"会看手机"的活跃度，0 到 1。睡着时为 0。"""
        if self.is_sleeping(dt):
            return 0.0
        daily = self.daily_for(dt)
        local = dt.astimezone(self.tz)
        minute_of_day = local.hour * 60 + local.minute

        base = (
            self.config.class_activity
            if self.class_containing(dt) is not None
            else self.config.activity.at_minutes(minute_of_day)
        )
        return max(0.0, min(1.0, base * daily.activity_multiplier))

    def next_wake_after(self, dt: datetime) -> datetime:
        """dt 之后的下一次起床；若 dt 正在睡，就是这一觉的起床时刻。"""
        window = self.sleep_window_containing(dt)
        if window is not None:
            return window[1]
        d = dt.astimezone(self.tz).date()
        for offset in range(0, _SEARCH_LIMIT):
            daily = self.for_day(d + timedelta(days=offset))
            if daily.night_sleep_end > dt:
                return daily.night_sleep_end
        raise RuntimeError("找不到下一次起床时间，检查 rhythm.sleep 配置")

    def next_sleep_after(self, dt: datetime) -> datetime:
        """dt 之后的下一次入睡。"""
        d = dt.astimezone(self.tz).date()
        for offset in range(-1, _SEARCH_LIMIT):
            daily = self.for_day(d + timedelta(days=offset))
            if daily.night_sleep_start > dt:
                return daily.night_sleep_start
        raise RuntimeError("找不到下一次入睡时间，检查 rhythm.sleep 配置")

    def state_at(self, dt: datetime) -> RhythmSnapshot:
        """某一时刻的作息快照。"""
        daily = self.daily_for(dt)
        next_wake = self.next_wake_after(dt)
        next_sleep = self.next_sleep_after(dt)
        activity = self.activity_at(dt)

        window = self.sleep_window_containing(dt)
        if window is not None:
            return RhythmSnapshot(
                state="sleeping",
                at=dt,
                until=window[1],
                next_wake=next_wake,
                next_sleep=next_sleep,
                activity=0.0,
                variant=daily.variant,
                variant_note=daily.variant_note,
            )

        block = self.class_containing(dt)
        if block is not None:
            return RhythmSnapshot(
                state="busy",
                at=dt,
                until=min(block.end, next_sleep),
                next_wake=next_wake,
                next_sleep=next_sleep,
                activity=activity,
                variant=daily.variant,
                variant_note=daily.variant_note,
                block_title=block.title,
            )

        winding = next_sleep - dt <= timedelta(minutes=self.config.winding_down_minutes)
        # 下一个状态切换点：入睡，或者下一节课开始
        upcoming = [next_sleep]
        upcoming += [c.start for c in daily.classes if c.start > dt]
        return RhythmSnapshot(
            state="winding_down" if winding else "free",
            at=dt,
            until=min(upcoming),
            next_wake=next_wake,
            next_sleep=next_sleep,
            activity=activity,
            variant=daily.variant,
            variant_note=daily.variant_note,
        )

    # -- 看手机 -------------------------------------------------------------

    def _sample_interval_minutes(self, activity: float, rng: random.Random) -> float:
        """活跃度越低，两次看手机隔得越久。对数正态，避免出现固定间隔。"""
        median = self.config.base_glance_minutes / max(activity, 0.01)
        return max(0.5, median * math.exp(rng.gauss(0, self.config.glance_sigma)))

    def first_glance_after_waking(self, wake: datetime, rng: random.Random) -> datetime:
        """醒来之后第一次看手机。多数人是躺床上就看了。"""
        if rng.random() < 0.6:
            return wake + timedelta(minutes=rng.uniform(0, 25))
        return wake + timedelta(minutes=rng.uniform(25, 90))

    def next_glance_after(self, dt: datetime, rng: random.Random) -> datetime:
        """从 dt 起，下一次瞄手机是什么时候。睡着就顺延到醒来之后。"""
        t = dt
        for _ in range(_SEARCH_LIMIT):
            activity = self.activity_at(t)
            if activity < self.config.min_activity_to_glance:
                return self.first_glance_after_waking(self.next_wake_after(t), rng)
            nxt = t + timedelta(minutes=self._sample_interval_minutes(activity, rng))
            if self.activity_at(nxt) >= self.config.min_activity_to_glance:
                return nxt
            t = nxt  # 落进了睡眠，下一轮会跳到醒来之后
        return t

    def glances_between(self, start: datetime, end: datetime, rng: random.Random) -> list[datetime]:
        """区间内所有看手机的时刻，主要给 ``simulate`` 命令用。"""
        out: list[datetime] = []
        t = start
        while t < end and len(out) < 5000:
            t = self.next_glance_after(t, rng)
            if t >= end:
                break
            out.append(t)
        return out
