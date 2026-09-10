"""作息：每天抽一次签，而不是照表执行。

核心想法：真人的作息有中心倾向，但没有精确规律。所以这里
**不存在**"每天 23:30 睡觉"这样的常量。每一天会：

0. 先确定这一天处在哪个**阶段**。阶段跨好几天（考试周、赶 project、刚放假），
   因为真人的忙是成片的，不是每天独立掷骰子。
1. 在阶段之上按 ``rhythm.variants`` 的权重抽一个**当日变体**（普通 / 熬夜 / 早起 / 累 …）。
2. 从分布里采样这一晚的入睡和起床时刻。
3. 按 ``probability`` 决定今天每节课是不是真的去了。

所以"她今天一直没回"通常是几件事叠在一起：这周本来就忙、昨晚熬到很晚、
他刚好在她睡着的时候发的。没有哪个开关叫"今天不理人"。

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
from .persona import DayVariant, LifePhase, RhythmConfig, hhmm_to_minutes

_SEARCH_LIMIT = 400
"""向前搜索的最大步数，防止配置写错时死循环。"""

_PHASE_EPOCH = date(2025, 1, 1)
"""阶段序列的推演起点。改它等于把人物的整条时间线重排。"""


class Rhythm:
    def __init__(self, config: RhythmConfig, tz: ZoneInfo, seed: int = 0) -> None:
        self.config = config
        self.tz = tz
        self.seed = seed
        self._cache: dict[date, DailyRhythm] = {}
        self._phase_spans: list[tuple[int, int, LifePhase]] = []

    # -- 抽签 ---------------------------------------------------------------

    def _rng_for(self, day: date) -> random.Random:
        """同一天永远得到同一个随机序列。"""
        return random.Random(self.seed * 1_000_003 + day.toordinal())

    def _weighted_choice(self, items, rng: random.Random):
        """按 weight 抽一个。"""
        total = sum(i.weight for i in items)
        roll = rng.uniform(0, total)
        acc = 0.0
        for item in items:
            acc += item.weight
            if roll <= acc:
                return item
        return items[-1]

    def phase_for(self, day: date) -> LifePhase:
        """这一天处在哪个阶段。阶段序列从 ``_PHASE_EPOCH`` 起确定性推演。"""
        phases = self.config.phases
        if not phases:
            return LifePhase(name="平常")
        if day < _PHASE_EPOCH:
            return phases[0]

        target = (day - _PHASE_EPOCH).days
        if self._phase_spans and self._phase_spans[-1][1] > target:
            for start, end, phase in self._phase_spans:
                if start <= target < end:
                    return phase

        cursor = self._phase_spans[-1][1] if self._phase_spans else 0
        while cursor <= target and len(self._phase_spans) < 100_000:
            rng = random.Random(self.seed * 7_919 + cursor)
            phase = self._weighted_choice(phases, rng)
            length = rng.randint(phase.min_days, max(phase.min_days, phase.max_days))
            self._phase_spans.append((cursor, cursor + length, phase))
            cursor += length
        return self._phase_spans[-1][2]

    def _at(self, day: date, minutes: float) -> datetime:
        """把"当天第 N 分钟"变成带时区的时刻，允许跨日。"""
        base = datetime.combine(day, time(0, 0), tzinfo=self.tz)
        return base + timedelta(minutes=minutes)

    def _raw_night(self, day: date) -> tuple[datetime, datetime, DayVariant, LifePhase]:
        """只用这一天自己的随机数采样，不看邻近的日子。

        返回 ``(今早起床, 今晚入睡, 当日变体, 阶段)``。跨天的睡眠时长校正在
        :meth:`for_day` 里做，那里才同时知道昨晚和今早。
        """
        rng = self._rng_for(day)
        cfg = self.config
        phase = self.phase_for(day)
        chosen = self._weighted_choice(cfg.variants, rng) if cfg.variants else DayVariant(name="普通")

        wake_median = hhmm_to_minutes(cfg.sleep.wake_median)
        sleep_median = hhmm_to_minutes(cfg.sleep.start_median)
        # 入睡时刻若早于起床时刻（如 01:20 < 08:40），说明是躺到了次日凌晨
        sleep_offset = sleep_median + (24 * 60 if sleep_median < wake_median else 0)

        wake_minutes = (
            wake_median + rng.gauss(0, cfg.sleep.wake_sigma_minutes) + chosen.wake_shift_hours * 60
        )
        sleep_minutes = (
            sleep_offset
            + rng.gauss(0, cfg.sleep.start_sigma_minutes)
            + chosen.sleep_start_shift_hours * 60
        )
        return self._at(day, wake_minutes), self._at(day, sleep_minutes), chosen, phase

    def for_day(self, day: date) -> DailyRhythm:
        """返回这一天抽出来的作息：今早几点起、今晚几点睡、今天什么状态。

        当日变体影响的是**当天**：抽到"熬夜"就是今晚睡得晚，抽到"早起"就是今天早上起得早。
        """
        cached = self._cache.get(day)
        if cached is not None:
            return cached

        cfg = self.config
        wake, sleep_start, chosen, phase = self._raw_night(day)

        # 起床时刻不能和昨晚的入睡时刻各管各的，否则会抽出"四点睡七点起"这种。
        # 真人睡得晚就起得晚，但也不是全跟着走：有课有闹钟，生物钟会把人拉回来。
        # 所以在"跟着昨晚走"和"跟着生物钟走"之间做加权混合。只回溯一天，不递归。
        prev_sleep_start = self._raw_night(day - timedelta(days=1))[1]
        target_sleep = timedelta(
            minutes=(hhmm_to_minutes(cfg.sleep.wake_median) - hhmm_to_minutes(cfg.sleep.start_median))
            % (24 * 60)
        )
        follow = prev_sleep_start + target_sleep
        w = cfg.sleep_follow_weight
        wake = follow + (wake - follow) * (1 - w)

        low = timedelta(hours=cfg.sleep.min_hours)
        high = timedelta(hours=cfg.sleep.max_hours)
        if wake - prev_sleep_start < low:
            wake = prev_sleep_start + low
        elif wake - prev_sleep_start > high:
            wake = prev_sleep_start + high

        # 入睡不能早于起床，至少醒着待一会儿
        sleep_start = max(sleep_start, wake + timedelta(hours=2))

        classes: list[ClassInstance] = []
        rng = random.Random(self.seed * 1_000_003 + day.toordinal() + 17)
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

        engage = (
            chosen.engage_probability
            if chosen.engage_probability is not None
            else cfg.engage_probability
        )
        daily = DailyRhythm(
            day=day,
            phase=phase.name,
            phase_note=phase.note,
            variant=chosen.name,
            variant_note=chosen.note,
            wake=wake,
            sleep_start=sleep_start,
            activity_multiplier=chosen.activity_multiplier * phase.activity_multiplier,
            engage_probability=max(0.05, min(1.0, engage * phase.engage_multiplier)),
            classes=classes,
        )
        self._cache[day] = daily
        return daily

    # -- 查询 ---------------------------------------------------------------

    def wake_of(self, day: date) -> datetime:
        """``day`` 这一天早上的起床时刻。"""
        return self.for_day(day).wake

    def logical_day(self, dt: datetime) -> date:
        """凌晨还没睡的时间算作前一天。用来取当日变体。"""
        d = dt.astimezone(self.tz).date()
        return d - timedelta(days=1) if dt < self.wake_of(d) else d

    def daily_for(self, dt: datetime) -> DailyRhythm:
        return self.for_day(self.logical_day(dt))

    def sleep_window_containing(self, dt: datetime) -> tuple[datetime, datetime] | None:
        """若 dt 处于某一觉之中，返回 ``(入睡, 起床)``；否则 None。"""
        d = dt.astimezone(self.tz).date()
        for offset in (-1, 0, 1):
            start = self.for_day(d + timedelta(days=offset)).sleep_start
            end = self.for_day(d + timedelta(days=offset + 1)).wake
            if start <= dt < end:
                return start, end
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
            wake = self.for_day(d + timedelta(days=offset)).wake
            if wake > dt:
                return wake
        raise RuntimeError("找不到下一次起床时间，检查 rhythm.sleep 配置")

    def next_sleep_after(self, dt: datetime) -> datetime:
        """dt 之后的下一次入睡。"""
        d = dt.astimezone(self.tz).date()
        for offset in range(-1, _SEARCH_LIMIT):
            start = self.for_day(d + timedelta(days=offset)).sleep_start
            if start > dt:
                return start
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
                phase=daily.phase,
                variant=daily.variant,
                mood_notes=daily.mood_notes,
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
                phase=daily.phase,
                variant=daily.variant,
                mood_notes=daily.mood_notes,
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
            phase=daily.phase,
            variant=daily.variant,
            mood_notes=daily.mood_notes,
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
