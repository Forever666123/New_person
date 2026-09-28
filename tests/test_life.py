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


async def test_only_one_follow_up_is_ever_waiting(harness: Harness) -> None:
    """同一时间最多挂一个"回头再说"。

    线上真出过：他说了几件"明天早上我要做 X"，她每条回复都给自己定一个
    "过 N 分钟问问他"，队列里同时挂着三个。分钟数是模型自己心算的，
    个个偏早——他还没起床她就来问"做了吗"。三个叠在一起，
    她就成了一张待办清单。
    """
    harness.clock.set(datetime(2026, 10, 1, 14, 0, tzinfo=TZ))
    first = await harness.life.schedule_follow_up("owner", 60, "看完他那段代码")
    second = await harness.life.schedule_follow_up("owner", 90, "问问他做了没")
    third = await harness.life.schedule_follow_up("owner", 30, "提醒他")

    assert first, "第一个该排上"
    assert not second and not third, "已经挂着一个了，后面的不该再叠上去"
    assert len(await harness.memory.pending_jobs("follow_up", "owner")) == 1


def test_his_plans_are_not_her_follow_ups(persona: Persona) -> None:
    """提示词要讲清楚：他说他要做什么，那是他的计划，记进台账，不是她的"回头再说"。

    台账那一套是他自己定的规矩——"只记我主动说出口的，追问的时机按事情本身的
    周期走"。让模型把他的计划塞进 follow_up，等于绕开这条规矩，
    用一个它自己心算的分钟数去追问他，而心算的分钟数总是偏早。
    """
    from newperson.prompts import build_system

    rules = build_system(persona)
    assert "只用于你自己答应过的事" in rules
    assert "不是你的 follow_up" in rules


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


async def test_an_empty_photo_library_does_not_eat_the_day(harness: Harness) -> None:
    """照片库空着的时候，要照片的那几种不进候选。

    原来 window_photo 照样抽签、占掉当天的名额，到点才发现没照片、跳过——
    大约六分之一本该开口的日子，她一句话都不说。
    """
    harness.life.has_photos = lambda: False
    _counts, kinds, _ = await harness.sweep(150, TERM_START)
    needs_photo = {k.name for k in harness.life.persona.proactive.kinds if k.requires_photo}
    assert needs_photo, "前提不成立：人设里没有要照片的主动种类"
    assert not (needs_photo & set(kinds)), f"没照片还排了：{kinds}"


async def test_a_failed_day_plan_is_tried_again_the_same_day(harness: Harness) -> None:
    """起床那次日程没生成出来，当天再试。原来一整天没有日程，也就一整天不主动。"""
    from newperson.models import Job

    life, memory, clock = harness.life, harness.memory, harness.clock
    day = life.rhythm.local_date(clock.now())
    wake = life.rhythm.for_day(day).wake
    clock.set(wake + timedelta(minutes=10))

    class Failing:
        async def generate_day_plan(self, _req, _day):
            return None

    life.brain = Failing()
    await life.handle_day_plan_job(Job(kind="day_plan", run_at=clock.now()))
    retries = [j for j in await memory.pending_jobs("day_plan") if j.payload.get("retry")]
    assert len(retries) == 1, "没人再试"
    assert retries[0].run_at > clock.now()
    assert life.rhythm.local_date(retries[0].run_at) == day


async def test_a_hand_saved_plan_still_gets_her_proactive_moments(harness: Harness) -> None:
    """`plan --save` 存进去的日程，她起床时照样补排主动时刻。

    主动时刻原来只在"新生成日程"那条路上排：手动存过的那一天，
    她起床看见已经有日程就直接返回，一次都不会主动开口。
    """
    from newperson.life import PROACTIVE_PENDING

    life, memory, clock = harness.life, harness.memory, harness.clock
    scheduled = 0
    for offset in range(20):
        day_start = TERM_START + timedelta(days=offset)
        day = day_start.date()
        clock.set(life.rhythm.for_day(day).wake + timedelta(minutes=5))
        await memory.save_day_plan(day, PLAN)
        await memory.kv_set(f"{PROACTIVE_PENDING}{day}", "1")
        await life.ensure_today_plan("owner")
        assert await memory.kv_get(f"{PROACTIVE_PENDING}{day}") is None
        scheduled += len(await memory.pending_jobs("proactive"))
    assert scheduled > 0, "手动存过日程的日子一次都没排主动"
