"""主动消息的分寸。

这类东西最容易崩在"太黏人"上：每种主动各自掷骰子，合起来就变成
每天都要找你说话。这些测试守着频率和衰减。
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.life import LifeEngine
from newperson.memory import Memory
from newperson.models import DayPlan, PlanEvent
from newperson.persona import Persona
from newperson.rhythm import Rhythm
from newperson.scheduler import Scheduler

TZ = ZoneInfo("America/New_York")
TERM_START = datetime(2026, 10, 1, tzinfo=TZ)
WINTER_START = datetime(2026, 12, 21, tzinfo=TZ)

PLAN = DayPlan(
    date="x",
    mood="还行",
    events=[
        PlanEvent(start="19:00", end="21:00", title="在图书馆", detail="位置难抢", shareable=True)
    ],
)


class Harness:
    def __init__(self, life: LifeEngine, memory: Memory, clock: FakeClock, calendar) -> None:
        self.life, self.memory, self.clock, self.calendar = life, memory, clock, calendar

    async def sweep(self, days: int, start: datetime) -> tuple[list[int], Counter, int]:
        counts, kinds, away = [], Counter(), 0
        for i in range(days):
            day = (start + timedelta(days=i)).date()
            if self.calendar.trip_for(day):
                away += 1
            self.clock.set(datetime.combine(day, datetime.min.time(), TZ).replace(hour=7))
            got = await self.life.candidate_moments(PLAN, day, "owner")
            counts.append(len(got))
            for _, kind, _ in got:
                kinds[kind.name] += 1
        return counts, kinds, away


@pytest.fixture
async def harness(tmp_path: Path, persona: Persona):
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    memory = Memory(tmp_path / "life.db")
    await memory.open()
    clock = FakeClock(TERM_START.replace(hour=7))
    life = LifeEngine(
        persona,
        rhythm,
        calendar,
        memory,
        Scheduler(memory, clock),
        None,
        clock,
        random.Random(11),
    )
    yield Harness(life, memory, clock, calendar)
    await memory.close()


async def test_she_is_not_clingy(harness: Harness) -> None:
    """平均下来两三天主动一次。每天都找你说话就不是她了。"""
    counts, _kinds, _ = await harness.sweep(120, TERM_START)
    per_day = sum(counts) / len(counts)
    assert 0.2 <= per_day <= 0.6, f"平均每天主动 {per_day:.2f} 次"


async def test_most_days_she_says_nothing_first(harness: Harness) -> None:
    counts, _kinds, _ = await harness.sweep(120, TERM_START)
    assert counts.count(0) / len(counts) > 0.5


async def test_she_rarely_double_texts(harness: Harness) -> None:
    """开了口不代表要说一整天。"""
    counts, _kinds, _ = await harness.sweep(120, TERM_START)
    spoke = [c for c in counts if c]
    assert sum(1 for c in spoke if c >= 2) / max(len(spoke), 1) < 0.5


async def test_weights_decide_how_she_opens(harness: Harness) -> None:
    """人设里 callback 权重最高，她最常用的方式就该是提一句旧事。"""
    _counts, kinds, _ = await harness.sweep(200, TERM_START)
    assert kinds["callback"] > kinds["trading_check"]


async def test_being_ignored_makes_her_back_off(harness: Harness) -> None:
    """追着说话是这类东西最容易崩掉的地方。"""
    baseline, _k, _ = await harness.sweep(120, TERM_START)
    await harness.memory.update_conversation("owner", unanswered_initiations=2)
    after, _k2, _ = await harness.sweep(120, TERM_START)
    assert sum(after) < sum(baseline) * 0.4


async def test_travel_talk_only_happens_while_away(harness: Harness) -> None:
    """没出门的时候不该冒出"这边怎么样"。"""
    _counts, kinds, away = await harness.sweep(25, WINTER_START)
    assert away > 0, "这段寒假她没出过门，换个种子再测"
    assert kinds["travel_note"] > 0

    _c2, term_kinds, term_away = await harness.sweep(60, datetime(2027, 1, 20, tzinfo=TZ))
    assert term_away == 0
    assert term_kinds["travel_note"] == 0


async def test_she_never_schedules_a_message_while_asleep(harness: Harness) -> None:
    for i in range(60):
        day = (TERM_START + timedelta(days=i)).date()
        harness.clock.set(datetime.combine(day, datetime.min.time(), TZ).replace(hour=7))
        for moment, _kind, _note in await harness.life.candidate_moments(PLAN, day, "owner"):
            assert not harness.life.rhythm.is_sleeping(moment)


async def test_chattiness_turns_her_down(harness: Harness) -> None:
    """`!np chatty 0.2` 之后她该明显安静下来。"""
    loud, _k, _ = await harness.sweep(120, TERM_START)
    await harness.memory.kv_set("chattiness", "0.2")
    quiet, _k2, _ = await harness.sweep(120, TERM_START)
    assert sum(quiet) < sum(loud) * 0.5


async def test_the_day_plan_feeds_what_she_shares(harness: Harness) -> None:
    """她说的自己的事要来自今天的日程，不是凭空编的。"""
    for i in range(60):
        day = (TERM_START + timedelta(days=i)).date()
        harness.clock.set(datetime.combine(day, datetime.min.time(), TZ).replace(hour=7))
        for _moment, kind, note in await harness.life.candidate_moments(PLAN, day, "owner"):
            if kind.name == "own_life":
                assert "在图书馆" in note
                return
    pytest.fail("六十天里她一次都没提过自己的事")


async def test_the_current_event_is_found(harness: Harness, persona: Persona) -> None:
    now = datetime(2026, 10, 1, 20, 0, tzinfo=TZ)
    assert harness.life.current_event(PLAN, now).title == "在图书馆"
    assert harness.life.current_event(PLAN, now.replace(hour=15)) is None


async def test_a_just_finished_event_still_counts(harness: Harness) -> None:
    """用来说"我刚……"。"""
    just_after = datetime(2026, 10, 1, 22, 30, tzinfo=TZ)
    assert harness.life.recent_event(PLAN, just_after).title == "在图书馆"
    assert harness.life.recent_event(PLAN, just_after + timedelta(hours=3)) is None


async def test_a_broken_time_in_the_plan_does_not_crash(harness: Harness) -> None:
    """模型偶尔会写出 25:00 这种时间，不能让她整个挂掉。"""
    bad = DayPlan(
        date="x",
        mood="",
        events=[PlanEvent(start="晚上", end="25:00", title="乱写的", detail="", shareable=False)],
    )
    assert harness.life.current_event(bad, datetime(2026, 10, 1, 20, tzinfo=TZ)) is None


async def test_a_follow_up_lands_when_she_is_awake(harness: Harness) -> None:
    """她说"我查完告诉你"，不能在她睡着的时候冒出来。"""
    harness.clock.set(datetime(2026, 10, 1, 23, 30, tzinfo=TZ))
    await harness.life.schedule_follow_up("owner", 300, "看完他那段代码")
    jobs = await harness.memory.pending_jobs("follow_up", "owner")
    assert jobs
    assert not harness.life.rhythm.is_sleeping(jobs[0].run_at)


def test_proactive_moments_follow_her_activity_curve(persona: Persona) -> None:
    """她主动开口的时刻要偏向"她本来就在看手机"的那几段。

    原来是在整个清醒时段里均匀抽，于是她可能在自己那三个小时的课上到一半时
    忽然说一句"今天雪好大"——而那一刻她的 Discord 状态明明写着在上课。
    状态和行为对不上是最直白的一种露馅。

    这条测试自己算一遍均匀抽的基线再比，所以改人设参数不会把它弄红。
    """
    import statistics

    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    life = LifeEngine.__new__(LifeEngine)
    life.rhythm, life.persona = rhythm, persona
    kind = next(k for k in persona.proactive.kinds if k.name == "own_life")

    weighted: list[float] = []
    uniform: list[float] = []
    for day_offset in range(60):
        day = (datetime(2026, 9, 15, tzinfo=persona.tz) + timedelta(days=day_offset)).date()
        daily = rhythm.for_day(day)
        span = (daily.sleep_start - daily.wake).total_seconds()
        if span <= 0:
            continue
        for seed in range(10):
            life.rng = random.Random(day_offset * 100 + seed)
            moment = life._sample_moment(kind, daily.wake, daily.sleep_start, day)
            if moment is not None:
                assert not rhythm.is_sleeping(moment), "抽到了她睡着的时候"
                weighted.append(rhythm.activity_at(moment))
            rng = random.Random(day_offset * 100 + seed)
            uniform.append(
                rhythm.activity_at(daily.wake + timedelta(seconds=rng.uniform(0, span)))
            )

    assert statistics.mean(weighted) > statistics.mean(uniform), (
        f"加权之后平均活跃度没有提高：{statistics.mean(weighted):.3f} "
        f"vs 均匀 {statistics.mean(uniform):.3f}"
    )
