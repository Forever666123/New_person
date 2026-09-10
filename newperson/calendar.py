"""学期日历与出行。

一个留学生的一年不是均匀的。开学、期末周、感恩节、寒假、春假、暑假，
每一段的作息和心情都不一样。放假会出去玩，长假可能飞得很远，
人在别的时区，回消息的时间自然就跟平时不一样。

这个模块提供两件事：

- :meth:`AcademicCalendar.period_for` 某一天处在学期的哪一段
- :meth:`AcademicCalendar.trip_for` 某一天在不在外面，在哪

出行是确定性生成的：给定假期和 ``seed``，抽一次就固定下来，
所以她"上周去了纽约"这件事不会下次问起来就变了。
"""

from __future__ import annotations

import random
import zlib
from datetime import date, timedelta

from .models import AcademicPeriod, Trip
from .persona import AcademicConfig

_PRIORITY = {"break": 0, "finals": 1, "summer": 2, "in_session": 3}
"""重叠时谁说了算。感恩节放假嵌在秋季学期里，假期优先。"""


class AcademicCalendar:
    def __init__(self, config: AcademicConfig, seed: int = 0) -> None:
        self.config = config
        self.seed = seed
        self._trip_cache: dict[str, list[Trip]] = {}

    # -- 学期 ---------------------------------------------------------------

    def period_for(self, day: date) -> AcademicPeriod:
        """这一天属于学期日历的哪一段。

        显式配置的区间优先；超出配置范围的年份按 ``fallback`` 里的典型模式推算，
        这样人物跑过一个学年也不会突然没有日历。
        """
        hits = [p for p in self.config.periods if p.contains(day)]
        if hits:
            return min(hits, key=lambda p: _PRIORITY.get(p.kind, 9))
        return self._fallback_period(day)

    def _fallback_period(self, day: date) -> AcademicPeriod:
        """没有显式配置的年份，按典型的美国大学学年推算。"""
        fb = self.config.fallback
        year = day.year

        def md(spec: str, y: int) -> date:
            month, dom = (int(x) for x in spec.split("-"))
            return date(y, month, dom)

        fall_start, fall_end = md(fb.fall[0], year), md(fb.fall[1], year)
        spring_start, spring_end = md(fb.spring[0], year), md(fb.spring[1], year)

        if fall_start <= day <= fall_end:
            return AcademicPeriod(
                name=f"{year} 秋季学期", kind="in_session", start=fall_start, end=fall_end
            )
        if spring_start <= day <= spring_end:
            return AcademicPeriod(
                name=f"{year} 春季学期", kind="in_session", start=spring_start, end=spring_end
            )
        if day > fall_end:  # 年底的寒假
            return AcademicPeriod(
                name="寒假",
                kind="break",
                start=fall_end + timedelta(days=1),
                end=date(year + 1, 1, 1) + (md(fb.spring[0], year + 1) - date(year + 1, 1, 1)) - timedelta(days=1),
                sleep_bonus_hours=1.0,
                travel="long",
            )
        if day < spring_start:  # 年初的寒假尾巴
            return AcademicPeriod(
                name="寒假",
                kind="break",
                start=date(year, 1, 1),
                end=spring_start - timedelta(days=1),
                sleep_bonus_hours=1.0,
                travel="long",
            )
        return AcademicPeriod(
            name="暑假",
            kind="summer",
            start=spring_end + timedelta(days=1),
            end=fall_start - timedelta(days=1),
            sleep_bonus_hours=1.0,
            travel="long",
        )

    def in_session(self, day: date) -> bool:
        """今天要不要上课。期末周和假期都没有正课。"""
        return self.period_for(day).kind == "in_session"

    # -- 出行 ---------------------------------------------------------------

    def _trips_in(self, period: AcademicPeriod) -> list[Trip]:
        """某个假期里生成的出行。同一个假期永远抽出同样的结果。"""
        if period.travel == "none":
            return []
        key = f"{period.name}:{period.start}:{period.end}"
        cached = self._trip_cache.get(key)
        if cached is not None:
            return cached

        cfg = self.config.travel
        pool = cfg.long_trips if period.travel == "long" else cfg.short_trips
        probability = cfg.long_probability if period.travel == "long" else cfg.short_probability
        span = (period.end - period.start).days + 1
        # 用 crc32 而不是内置 hash：内置 hash 对字符串会随进程变化，
        # 那样她"上次去了哪"每次重启都不一样。
        rng = random.Random(self.seed * 31 + zlib.crc32(key.encode("utf-8")))

        trips: list[Trip] = []
        if pool and span >= 4 and rng.random() < probability:
            spot = rng.choice(pool)
            if period.travel == "long":
                length = rng.randint(cfg.long_min_days, min(cfg.long_max_days, span - 2))
            else:
                length = rng.randint(cfg.short_min_days, min(cfg.short_max_days, span - 1))
            length = max(2, length)
            latest_offset = max(0, span - length)
            offset = rng.randint(0, latest_offset)
            start = period.start + timedelta(days=offset)
            trips.append(
                Trip(
                    start=start,
                    end=start + timedelta(days=length - 1),
                    place=spot.place,
                    timezone=spot.timezone or self.config.home_timezone,
                    note=spot.note or f"你在{spot.place}。",
                    kind=period.travel,
                    activity_multiplier=(
                        cfg.long_activity_multiplier
                        if period.travel == "long"
                        else cfg.short_activity_multiplier
                    ),
                )
            )

        self._trip_cache[key] = trips
        return trips

    def trip_for(self, day: date) -> Trip | None:
        """这一天在不在外面。"""
        for trip in self._trips_in(self.period_for(day)):
            if trip.contains(day):
                return trip
        return None

    def timezone_for(self, day: date) -> str:
        """这一天她人在哪个时区。"""
        trip = self.trip_for(day)
        return trip.timezone if trip else self.config.home_timezone

    def upcoming_trip(self, day: date, within_days: int = 21) -> Trip | None:
        """接下来一段时间有没有安排出门，用来让她提前提一句。"""
        for offset in range(1, within_days + 1):
            future = day + timedelta(days=offset)
            for trip in self._trips_in(self.period_for(future)):
                if trip.start == future:
                    return trip
        return None
