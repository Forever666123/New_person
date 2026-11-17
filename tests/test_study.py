"""催学：读他的学习日历，到点问一句。

这组测试守着几条线：iCal 认得准；问的时刻落在人设给的区间里、不成固定周期；
该闭嘴的时候闭嘴（她睡着、他那边太晚、他说过难受、关了、考完了）；
同一块同一步一辈子只问一次；日历地址绝不外泄；不占主动消息的额度；
体检数出来的跟真实发生的一样。
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from newperson import doctor, owner
from newperson.config import Settings
from newperson.discord_bot import CONVERSATION_ID, App
from newperson.models import IncomingMessage, ProactivePlan, ReplyPart
from newperson.persona import Persona
from newperson.prompts import build_system
from newperson.study import (
    LOW_MOOD_KEY,
    CalendarFetchError,
    HttpIcsSource,
    StudyBlock,
    StudyWatch,
    parse_ics,
)
from tests.conftest import ROOT
from tests.test_integration import _OPEN, build

SYD = ZoneInfo("Australia/Sydney")
NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _close_databases():
    """每个测试跑完把连接关掉，否则 aiosqlite 的后台线程会把进程挂住、退不出去。"""
    yield
    for mem in _OPEN:
        await mem.close()
    _OPEN.clear()


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def vevent(uid: str, start: datetime, end: datetime, title: str = "SENG2011 复习 W5", extra: str = "") -> str:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(end)}",
        f"SUMMARY:{title}",
    ]
    if extra:
        lines.append(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def calendar(*events: str) -> str:
    return "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", *events, "END:VCALENDAR", ""]
    )


class FakeSource:
    """假的日历来源。``fail`` 为真时像网断了一样抛错。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.fail = False
        self.calls = 0

    async def fetch(self) -> str:
        self.calls += 1
        if self.fail:
            raise CalendarFetchError("ClientConnectorError")
        return self.text


def said(text: str = "开始了没") -> ProactivePlan:
    return ProactivePlan(send=True, parts=[ReplyPart(text=text)])


async def make(tmp_path: Path, persona: Persona, script: list, now: datetime, text: str = ""):
    app, channel, llm, clock, memory = await build(tmp_path, persona, script, now=now)
    source = FakeSource(text)
    app.study = StudyWatch(persona, app.rhythm, memory, app.scheduler, clock, source, CONVERSATION_ID)
    app.scheduler.register("study_check", app.handle_study_check_job)
    return app, channel, llm, clock, memory, source


async def live(app: App, clock, until: datetime) -> None:
    """像线上一样跑：每半小时读一次日历，到点的任务照常执行。"""
    for _ in range(2000):
        if clock.now() >= until:
            break
        await app.study.sync_once()
        await app.scheduler.run_due_once()
        nxt = await app.memory.next_job_run_at()
        target = min(t for t in (nxt, clock.now() + timedelta(minutes=30), until) if t is not None)
        clock.set(max(target, clock.now() + timedelta(seconds=1)))
    else:
        raise AssertionError(f"跑不到 {until}，卡在 {clock.now()}：有任务到点了却一直没跑")
    await app.study.sync_once()
    await app.scheduler.run_due_once()


async def he_says(app: App, at: datetime, msg_id: int, text: str = "嗯 在学了") -> None:
    """他说了一句，她（假装）已经看过了。只为了让"他回过话"成立，不排回复。"""
    await app.memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID, discord_message_id=msg_id, author_id=42,
            author_name="Leo", content=text, created_at=at,
        )
    )
    ids = [m.id for m in await app.memory.unread_messages(CONVERSATION_ID)]
    await app.memory.mark_read(ids, at)


def day_block(rhythm, day: date, hours: float = 2.0) -> tuple[datetime, datetime]:
    """他那边这一天里找一块：开始、开始后一刻钟、结束后二十分钟，她都醒着，他那边也不晚。"""
    for hour in range(9, 20):
        start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=SYD)
        end = start + timedelta(hours=hours)
        probes = [start, start + timedelta(minutes=15), start + timedelta(minutes=50),
                  end, end + timedelta(minutes=25)]
        if all(not rhythm.is_sleeping(p) for p in probes) and end.astimezone(SYD).hour < 23:
            return start, end
    raise AssertionError("这一天找不到她醒着的块")


def night_block(rhythm, day: date) -> tuple[datetime, datetime]:
    """他那边晚上、她正在睡的一块。"""
    for hour in (19, 20, 18, 21):
        start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=SYD)
        end = start + timedelta(hours=1, minutes=30)
        if rhythm.is_sleeping(start) and rhythm.is_sleeping(end):
            return start, end
    raise AssertionError("这一天找不到她在睡的块")


DAY = date(2026, 11, 18)


def _noon_before(start: datetime) -> datetime:
    """块开始前两小时，她那边的时刻。"""
    return (start - timedelta(hours=2)).astimezone(NY)


async def statuses(memory) -> dict[tuple[str, str], tuple[str, str]]:
    rows = await memory._fetch_all("SELECT target, step, status, outcome FROM study_log", ())
    return {(r["target"], r["step"]): (r["status"], r["outcome"]) for r in rows}


# ---------------------------------------------------------------------------
# iCal
# ---------------------------------------------------------------------------


def test_ics_parsing_understands_folding_escapes_and_three_time_forms() -> None:
    """Google 导出的三种时间写法、行折叠、转义都要认对，认错一个就是在错的时刻问他。"""
    text = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Australia/Sydney\r\nBEGIN:STANDARD\r\n"
        "DTSTART:19700405T030000\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20261118T000000Z\r\nDTEND:20261118T020000Z\r\n"
        "SUMMARY:COMP2511 写 lab\\, 然后\\; 复习\\n第二行 很长的标题被折\r\n  叠了\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\nDTSTART;TZID=Australia/Sydney:20261118T140000\r\n"
        "DTEND;TZID=Australia/Sydney:20261118T153000\r\nSUMMARY:SENG2011 W5\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT10M\r\nDESCRIPTION:x\r\nEND:VALARM\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:c\r\nDTSTART:20261118T190000\r\nDURATION:PT1H30M\r\nSUMMARY:浮动\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    cal = parse_ics(text, SYD)
    by_title = {b.title.split()[0]: b for b in cal.blocks}
    a = by_title["COMP2511"]
    assert a.start == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
    assert a.title == "COMP2511 写 lab, 然后; 复习\n第二行 很长的标题被折 叠了"
    b = by_title["SENG2011"]
    assert b.start == datetime(2026, 11, 18, 14, 0, tzinfo=SYD)
    assert b.end - b.start == timedelta(minutes=90)
    c = by_title["浮动"]
    assert c.start == datetime(2026, 11, 18, 19, 0, tzinfo=SYD), "浮动时间按他的时区算"
    assert c.end - c.start == timedelta(minutes=90)
    assert len(cal.blocks) == 3, "VTIMEZONE 里的 DTSTART 不是一个事件"


def test_ics_parsing_skips_cancelled_all_day_and_counts_recurring() -> None:
    """取消的、全天的不算学习块；重复事件先跳过，但要数出来提醒他。"""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=SYD)
    text = calendar(
        vevent("ok", start, start + timedelta(hours=1)),
        vevent("gone", start, start + timedelta(hours=1), extra="STATUS:CANCELLED"),
        "BEGIN:VEVENT\r\nUID:allday\r\nDTSTART;VALUE=DATE:20261118\r\nDTEND;VALUE=DATE:20261119\r\nSUMMARY:x\r\nEND:VEVENT",
        vevent("rep", start, start + timedelta(hours=1), extra="RRULE:FREQ=DAILY;COUNT=5"),
        vevent("rep2", start, start + timedelta(hours=1), extra="RRULE:FREQ=WEEKLY"),
    )
    cal = parse_ics(text, SYD)
    assert [b.start for b in cal.blocks] == [start]
    assert cal.recurring_skipped == 2
    assert cal.all_day_skipped == 1
    assert cal.cancelled_skipped == 1


def test_a_moved_block_is_a_different_block() -> None:
    """改期之后标识要变：旧时刻排下的那几步才会被认出来作废。"""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=SYD)
    a = parse_ics(calendar(vevent("u", start, start + timedelta(hours=1))), SYD).blocks[0]
    b = parse_ics(calendar(vevent("u", start + timedelta(hours=2), start + timedelta(hours=3))), SYD).blocks[0]
    assert a.key != b.key
    assert "SENG" not in a.key, "标识里看不出标题"


# ---------------------------------------------------------------------------
# 时刻
# ---------------------------------------------------------------------------


async def test_ask_times_fall_in_their_ranges_and_do_not_repeat_a_pattern(
    tmp_path: Path, persona: Persona
) -> None:
    """开始后 2–12 分钟、结束后 3–20 分钟；每块都不一样，不成固定周期。

    固定"开始后 5 分钟准时问"的话，几天下来他一眼就看出是闹钟。
    """
    app, *_ = await make(tmp_path, persona, [], datetime(2026, 11, 10, 12, 0, tzinfo=NY))
    study = app.study
    blocks = []
    for i in range(40):
        day = date(2026, 11, 10) + timedelta(days=i % 20)
        start = datetime(day.year, day.month, day.day, 10 + (i % 3), 0, tzinfo=SYD)
        blocks.append(StudyBlock(f"k{i:02d}", start.astimezone(UTC), (start + timedelta(hours=2)).astimezone(UTC), "x"))
    starts, ends = [], []
    for b in blocks:
        plan = study.plan([b], b.start - timedelta(hours=1))
        for item in plan:
            step = item.components[0][0]
            if step == "start":
                starts.append((item.run_at - b.start).total_seconds() / 60)
            elif step == "end":
                ends.append((item.run_at - b.end).total_seconds() / 60)
    assert starts and ends
    assert all(2 <= s <= 12 for s in starts), starts
    assert all(3 <= e <= 20 for e in ends), ends
    assert statistics.pstdev(starts) > 1.5 and statistics.pstdev(ends) > 2.5
    assert len({round(s, 1) for s in starts}) > len(starts) * 0.8

    again = StudyWatch(persona, app.rhythm, app.memory, app.scheduler, app.clock, None)
    assert [p.run_at for p in again.plan(blocks[:5], blocks[0].start - timedelta(hours=1))] == [
        p.run_at for p in study.plan(blocks[:5], blocks[0].start - timedelta(hours=1))
    ], "同样的块重启之后要排在同样的时刻"


async def test_a_daytime_block_gets_start_nudge_and_end_and_nothing_more(
    tmp_path: Path, persona: Persona
) -> None:
    """他一直不回：开始了没、追问一次、学到哪了，一块最多三条，追问更晚但在块里。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    text = calendar(vevent("blk", start, end))
    app, channel, llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没"), said("？"), said("学到哪了"), said("多的")],
        _noon_before(start), text,
    )
    await live(app, clock, end + timedelta(hours=2))
    assert channel.texts == ["开始了没", "？", "学到哪了"]
    log = await statuses(memory)
    assert {k[1]: v[0] for k, v in log.items() if k[1] != "seen"} == {
        "start": "sent", "nudge": "sent", "end": "sent",
    }
    rows = await memory._fetch_all("SELECT step, planned_at, sent_at FROM study_log WHERE status='sent'", ())
    sent = {r["step"]: datetime.fromisoformat(r["sent_at"]) for r in rows}
    assert timedelta(minutes=2) <= sent["start"] - start <= timedelta(minutes=12)
    assert timedelta(minutes=20) <= sent["nudge"] - sent["start"] <= timedelta(minutes=35)
    assert sent["nudge"] < end
    assert timedelta(minutes=3) <= sent["end"] - end <= timedelta(minutes=20)


async def test_she_does_not_nudge_once_he_has_said_anything(tmp_path: Path, persona: Persona) -> None:
    """他回过任何一句就不追问——哪怕说的不是学习。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没"), said("学到哪了")], _noon_before(start),
        calendar(vevent("blk", start, end)),
    )
    await live(app, clock, start + timedelta(minutes=13))
    assert channel.texts == ["开始了没"]
    await he_says(app, clock.now() + timedelta(minutes=1), 900, "晚饭吃什么")
    await live(app, clock, end + timedelta(hours=1))
    assert channel.texts == ["开始了没", "学到哪了"]
    log = await statuses(memory)
    nudge = next(v for k, v in log.items() if k[1] == "nudge")
    assert nudge == ("skipped", "he_replied")


# ---------------------------------------------------------------------------
# 晚上的块
# ---------------------------------------------------------------------------


async def test_night_blocks_get_one_evening_question_and_one_next_morning(
    tmp_path: Path, persona: Persona
) -> None:
    """她睡着时的块：睡前问一次（几块也只一次），第二天他那边 8 点后、她醒着时再问一次。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    s1, e1 = night_block(probe.rhythm, DAY)
    s2, e2 = s1 + timedelta(hours=2), e1 + timedelta(hours=2)
    assert probe.rhythm.is_sleeping(s2)
    window = probe.rhythm.sleep_window_containing(s1)
    assert window is not None
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("今晚打算学到哪"), said("昨晚学到哪了"), said("多的")],
        window[0] - timedelta(hours=4), calendar(vevent("n1", s1, e1), vevent("n2", s2, e2)),
    )
    await live(app, clock, window[1] + timedelta(hours=20))
    assert channel.texts == ["今晚打算学到哪", "昨晚学到哪了"]
    rows = await memory._fetch_all("SELECT step, sent_at FROM study_log WHERE status='sent'", ())
    sent = {r["step"]: datetime.fromisoformat(r["sent_at"]) for r in rows}
    assert timedelta(minutes=30) <= window[0] - sent["evening"] <= timedelta(minutes=90)
    morning = sent["morning"].astimezone(SYD)
    assert morning > e2 and morning.hour >= 8, morning
    assert morning - e2 < timedelta(days=1, hours=3)
    assert not app.rhythm.is_sleeping(sent["morning"])


async def test_the_morning_question_rides_along_with_a_nearby_start_question(
    tmp_path: Path, persona: Persona
) -> None:
    """第二天早上那句前后 45 分钟里正好有"开始了没"，就并成一条，不连发两条。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    s1, e1 = night_block(probe.rhythm, DAY)
    study = probe.study
    window = probe.rhythm.sleep_window_containing(s1)
    assert window is not None
    night = StudyBlock("night", s1.astimezone(UTC), e1.astimezone(UTC), "x")
    alone = study.plan([night], window[0] - timedelta(hours=3))
    morning = next(p for p in alone if p.components[0][0] == "morning").run_at
    # 让一个白天块的"开始了没"正好落在早上那句前后
    day_start = morning - timedelta(minutes=7)
    if probe.rhythm.is_sleeping(day_start):
        pytest.skip("这一天早上那句附近她在睡")
    day = StudyBlock("day", day_start.astimezone(UTC), (day_start + timedelta(hours=1)).astimezone(UTC), "y")
    plan = study.plan([night, day], window[0] - timedelta(hours=3))
    host = next(p for p in plan if p.components[0] == ("start", "day"))
    assert ("morning", study.night_id(window)) in host.components
    assert not any(p.components[0][0] == "morning" for p in plan), "并进去了就不再单独问"

    # 从那一晚已经开始之后起跑：睡前那句已经错过了，第一句就是并起来的那条
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("昨晚学到哪了 今天这块开始没"), said("？"), said("学到哪了")],
        s1 + timedelta(minutes=10),
        calendar(vevent("night", s1, e1), vevent("day", day_start, day_start + timedelta(hours=1))),
    )
    await live(app, clock, day_start + timedelta(minutes=50))
    assert channel.texts[0] == "昨晚学到哪了 今天这块开始没"
    # 两步都记在同一个时刻上，体检里算一次开口
    rows = await memory._fetch_all(
        "SELECT step, sent_stamp FROM study_log WHERE status='sent' AND step IN ('start','morning')", ()
    )
    assert {r["step"] for r in rows} == {"start", "morning"}
    assert len({r["sent_stamp"] for r in rows}) == 1


# ---------------------------------------------------------------------------
# 该闭嘴的时候
# ---------------------------------------------------------------------------


async def _one_start_check(tmp_path: Path, persona: Persona, prepare=None):
    """跑一块白天的块，返回 (发了什么, study_log)。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没"), said("？"), said("学到哪了")], _noon_before(start),
        calendar(vevent("blk", start, end)),
    )
    if prepare is not None:
        await prepare(app, start, end)
    await live(app, clock, end + timedelta(hours=1))
    return channel.texts, await statuses(app.memory), (start, end)


async def test_she_says_nothing_about_study_after_he_said_he_is_down(
    tmp_path: Path, persona: Persona
) -> None:
    """他说了自己难受（触发低气压），之后一段时间她一个字都不提学习。"""

    async def down(app: App, start: datetime, _end: datetime) -> None:
        said_at = start - timedelta(hours=3)
        await app.memory.add_user_message(
            IncomingMessage(
                conversation_id=CONVERSATION_ID, discord_message_id=7, author_id=42,
                author_name="Leo", content="好累 有点想哭", created_at=said_at,
            )
        )
        await app._schedule_reply(said_at)
        await app.memory.mark_read([m.id for m in await app.memory.unread_messages(CONVERSATION_ID)], said_at)
        for job in await app.memory.pending_jobs("reply", CONVERSATION_ID):
            await app.scheduler.cancel(job.id or 0)

    texts, log, _ = await _one_start_check(tmp_path, persona, down)
    assert texts == []
    assert {v for k, v in log.items() if k[1] != "seen"} == {("skipped", "low_mood")}


async def test_low_mood_quiet_period_ends(tmp_path: Path, persona: Persona) -> None:
    """难受之后的静默有尽头：过了 low_mood_hours 照常问，不是从此不问。"""

    async def long_ago(app: App, start: datetime, _end: datetime) -> None:
        await app.memory.kv_set(LOW_MOOD_KEY, (start - timedelta(hours=19)).isoformat())

    texts, _log, _ = await _one_start_check(tmp_path, persona, long_ago)
    assert texts and texts[0] == "开始了没"


@pytest.mark.parametrize(
    ("why", "setup"),
    [
        ("owner_off", lambda app: app.memory.kv_set("study_off", "1")),
        ("paused", lambda app: app.memory.kv_set("paused", "1")),
        ("away", lambda app: app.memory.kv_set("away_note", "出差")),
    ],
)
async def test_switches_keep_her_quiet(tmp_path: Path, persona: Persona, why: str, setup) -> None:
    """`!np study off`、`!np pause`、`!np away` 期间一句都不问。"""

    async def flip(app: App, _s: datetime, _e: datetime) -> None:
        await setup(app)
        if why == "away":
            await app.memory.kv_set("away_until", "2026-12-31")

    texts, log, _ = await _one_start_check(tmp_path, persona, flip)
    assert texts == []
    assert {v[1] for k, v in log.items() if k[1] != "seen"} == {why}


async def test_turned_off_in_the_persona_means_nothing_happens(tmp_path: Path, persona: Persona) -> None:
    """人设里关着：不读日历、不排任何东西。"""
    off = persona.model_copy(deep=True)
    off.study_watch.enabled = False
    app, channel, _llm, clock, memory, source = await make(
        tmp_path, off, [said()], datetime(2026, 11, 17, 12, 0, tzinfo=NY),
        calendar(vevent("blk", datetime(2026, 11, 18, 10, tzinfo=SYD), datetime(2026, 11, 18, 12, tzinfo=SYD))),
    )
    await live(app, clock, datetime(2026, 11, 18, 23, tzinfo=NY))
    assert source.calls == 0 and channel.texts == []
    assert await memory.pending_jobs("study_check") == []


async def test_after_the_exam_day_she_stops_asking(tmp_path: Path, persona: Persona) -> None:
    """过了截止日（按他那边的日子）就不问了，日历上还有块也一样。"""
    early = persona.model_copy(deep=True)
    early.study_watch.until = DAY - timedelta(days=1)
    texts, log, _ = await _one_start_check(tmp_path, early)
    assert texts == []
    assert {v[1] for k, v in log.items() if k[1] != "seen"} == {"past_deadline"}


async def test_she_does_not_ask_while_she_sleeps_or_in_his_quiet_hours(
    tmp_path: Path, persona: Persona
) -> None:
    """她在睡、他那边 23:30–08:00，一律不问；错过的不顺延到醒来。"""
    app, *_ = await make(tmp_path, persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    study = app.study
    assert study.in_his_quiet(datetime(2026, 11, 18, 23, 45, tzinfo=SYD))
    assert study.in_his_quiet(datetime(2026, 11, 19, 7, 59, tzinfo=SYD))
    assert not study.in_his_quiet(datetime(2026, 11, 19, 8, 0, tzinfo=SYD))

    # 她搬到跟他同一个时区，他那边快半夜时她还醒着：这时问"开始了没"就落在他的静默时段里
    moved = persona.model_copy(deep=True)
    moved.timezone = "Australia/Sydney"
    moved.academic.home_timezone = "Australia/Sydney"
    probe, *_ = await make(tmp_path / "p", moved, [], datetime(2026, 11, 17, 12, 0, tzinfo=SYD))
    for offset in range(14):
        day = DAY + timedelta(days=offset)
        start = datetime(day.year, day.month, day.day, 23, 35, tzinfo=SYD)
        if not any(probe.rhythm.is_sleeping(start + timedelta(minutes=m)) for m in range(0, 70, 5)):
            break
    else:
        pytest.skip("两周里她都没醒到那么晚")
    end = start + timedelta(minutes=30)
    app2, channel, _llm, clock, memory, _src = await make(
        tmp_path / "q", moved, [said(), said(), said()], start - timedelta(hours=2),
        calendar(vevent("late", start, end)),
    )
    await live(app2, clock, end + timedelta(hours=10))
    assert channel.texts == []
    outcomes = {v[1] for k, v in (await statuses(memory)).items() if k[1] != "seen"}
    assert outcomes == {"his_quiet"}, outcomes


async def test_a_question_that_falls_into_her_sleep_is_dropped_not_postponed(
    tmp_path: Path, persona: Persona
) -> None:
    """块结束时她已经睡了：那句"学到哪了"就不问，醒来也不补。晚到的催促没有意义。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    rhythm = probe.rhythm
    found = None
    for offset in range(14):
        day = DAY + timedelta(days=offset)
        for minutes in range(12 * 60, 22 * 60, 10):
            start = datetime(day.year, day.month, day.day, tzinfo=SYD) + timedelta(minutes=minutes)
            end = start + timedelta(hours=1)
            awake = all(not rhythm.is_sleeping(start + timedelta(minutes=m)) for m in range(0, 16))
            asleep = all(rhythm.is_sleeping(end + timedelta(minutes=m)) for m in range(0, 25))
            if awake and asleep:
                found = (start, end)
                break
        if found:
            break
    if found is None:
        pytest.skip("两周里找不到块中间她睡着的那种")
    start, end = found
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没"), said("？"), said("学到哪了")], start - timedelta(hours=1),
        calendar(vevent("blk", start, end)),
    )
    await live(app, clock, end + timedelta(hours=14))
    assert channel.texts[0] == "开始了没"
    assert "学到哪了" not in channel.texts
    log = await statuses(memory)
    assert next(v for k, v in log.items() if k[1] == "end") == ("skipped", "she_sleeps")


async def test_a_cancelled_or_moved_block_is_not_asked_about(tmp_path: Path, persona: Persona) -> None:
    """排好之后他把块删了或者挪了：旧时刻不问；挪到的新时刻照常问。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, source = await make(
        tmp_path, persona, [said("开始了没"), said("？"), said("学到哪了")], _noon_before(start),
        calendar(vevent("blk", start, end)),
    )
    await app.study.sync_once()
    assert len(await memory.pending_jobs("study_check")) == 2
    source.text = calendar()  # 删掉了
    await live(app, clock, end + timedelta(hours=1))
    assert channel.texts == []
    assert await memory.pending_jobs("study_check") == []

    later_day = DAY + timedelta(days=1)
    s2, e2 = day_block(app.rhythm, later_day)
    source.text = calendar(vevent("blk", s2, e2))  # 同一个事件挪到第二天
    await live(app, clock, e2 + timedelta(hours=1))
    assert channel.texts[0] == "开始了没"


# ---------------------------------------------------------------------------
# 读日历
# ---------------------------------------------------------------------------


async def test_a_failed_fetch_is_silent_and_keeps_the_last_calendar(
    tmp_path: Path, persona: Persona, caplog
) -> None:
    """读失败：不出声、不发任何东西，沿用上一次；连着失败太久才当作没有学习块。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, source = await make(
        tmp_path, persona, [said("开始了没")], start - timedelta(hours=1),
        calendar(vevent("blk", start, end)),
    )
    assert await app.study.sync_once()
    source.fail = True
    with caplog.at_level(logging.INFO):
        await live(app, clock, start + timedelta(minutes=15))
    assert channel.texts == ["开始了没"], "上一次读到的照用"
    assert await memory.kv_get("study_cal_fail_since")

    # 连着失败超过 stale_after_hours：当作没有学习块，排着的作废
    clock.set(start + timedelta(hours=5))
    await app.study.sync_once()
    assert app.study.blocks(clock.now()) == []
    left = await memory.pending_jobs("study_check")
    assert all(j.payload.get("dynamic") for j in left), "按日历排的都该作废"
    await app.scheduler.run_due_once()
    assert channel.texts == ["开始了没"], "日历太久没读成，追问也不发"
    report = doctor.run(app.settings.db_path, clock.now(), days=7)
    assert any("没读成功" in f.line for f in report.findings), report.render()


SECRET = "https://calendar.google.com/calendar/ical/leo%40example.com/private-SECRETTOKEN123/basic.ics"


class FakeResponse:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self._chunks = chunks
        self.content = self

    async def iter_chunked(self, _n: int):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """假的 aiohttp 会话。``error`` 给了就在 get 时抛出一条**带完整地址**的异常。"""

    def __init__(self, response: FakeResponse | None = None, error: bool = False) -> None:
        self.response = response
        self.error = error

    def __call__(self, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url: str, **_kw):
        if self.error:
            raise OSError(f"Cannot connect to host for {url}")
        return self.response


def _chain(exc: BaseException | None) -> str:
    """异常连同整条 __context__ / __cause__ 链的文字。"""
    out = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        out += [str(exc), repr(exc)]
        exc = exc.__cause__ or exc.__context__
    return "\n".join(out)


async def test_a_chunked_calendar_is_read_to_the_end(monkeypatch) -> None:
    """分几块传过来的日历要读完整：只读到前半截的话，后面的块会被当成"日历上没有了"作废。"""
    start = datetime(2026, 11, 18, 10, 0, tzinfo=SYD)
    text = calendar(*(vevent(f"u{i}", start + timedelta(days=i), start + timedelta(days=i, hours=1))
                      for i in range(30))).encode("utf-8")
    pieces = [text[i:i + 97] for i in range(0, len(text), 97)]
    monkeypatch.setattr(HttpIcsSource, "session_factory", staticmethod(FakeSession(FakeResponse(200, pieces))))
    got = await HttpIcsSource(SecretStr(SECRET)).fetch()
    assert len(parse_ics(got, SYD).blocks) == 30

    big = [b"x" * (3 * 1024 * 1024)] * 2
    monkeypatch.setattr(HttpIcsSource, "session_factory", staticmethod(FakeSession(FakeResponse(200, big))))
    with pytest.raises(CalendarFetchError):
        await HttpIcsSource(SecretStr(SECRET)).fetch()


async def test_the_calendar_address_never_appears_anywhere(
    tmp_path: Path, persona: Persona, monkeypatch, caplog, capsys
) -> None:
    """地址是密钥：连不上、而且底层异常文本里带着完整地址的时候，
    往外抛的异常（整条链）、日志、check、体检、status 里都不能有它。"""
    from newperson.__main__ import _check_study

    monkeypatch.setattr(HttpIcsSource, "session_factory", staticmethod(FakeSession(error=True)))
    source = HttpIcsSource(SecretStr(SECRET))
    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarFetchError) as info:
        await source.fetch()
    assert "SECRETTOKEN123" not in _chain(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert "SECRETTOKEN123" not in repr(source)

    settings = Settings(study_calendar_ics_url=SecretStr(SECRET))
    assert "SECRETTOKEN123" not in repr(settings) and "SECRETTOKEN123" not in str(settings)
    await asyncio.to_thread(_check_study, settings, persona, True)  # check 是同步的，自己开事件循环
    printed = capsys.readouterr().out
    assert "日历读不了（OSError）" in printed and "SECRETTOKEN123" not in printed

    now = datetime(2026, 11, 17, 12, 0, tzinfo=NY)
    app, *_rest = await make(tmp_path, persona, [], now)
    app.study.source = HttpIcsSource(SecretStr(SECRET))
    with caplog.at_level(logging.DEBUG):
        assert not await app.study.sync_once()
    ctx = owner.OwnerContext(
        memory=app.memory, rhythm=app.rhythm, scheduler=app.scheduler, life=app.life,
        conversation_id=CONVERSATION_ID, now=now, study=app.study,
    )
    status = await owner.handle("!np status", ctx)
    report = doctor.run(app.settings.db_path, now + timedelta(minutes=1), days=7).render()
    for text in (caplog.text, status, report):
        assert "SECRETTOKEN123" not in text
    assert "OSError" in report


async def test_sync_failure_logs_only_the_kind_of_error(tmp_path: Path, persona: Persona, caplog) -> None:
    """就算来源抛出的异常里带着地址，日志里也只有类型名。"""

    class Leaky:
        async def fetch(self) -> str:
            raise OSError("connect to https://calendar.google.com/private-SECRETTOKEN123 failed")

    app, *_ = await make(tmp_path, persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    app.study.source = Leaky()
    with caplog.at_level(logging.DEBUG):
        assert not await app.study.sync_once()
    assert "SECRETTOKEN123" not in caplog.text
    assert "OSError" in (await app.memory.kv_get("study_cal_fail_kind") or "")
    report = doctor.run(app.settings.db_path, datetime(2026, 11, 17, 13, 0, tzinfo=NY), days=7)
    assert "SECRETTOKEN123" not in report.render()


# ---------------------------------------------------------------------------
# 跟别的机制的关系
# ---------------------------------------------------------------------------


async def test_study_checks_do_not_use_up_proactive_quota_or_count_as_ignored(
    tmp_path: Path, persona: Persona
) -> None:
    """催学不占主动的名额，他没回也不算被晾。不然几天下来她平时就不开口了。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没"), said("？"), said("学到哪了")], _noon_before(start),
        calendar(vevent("blk", start, end)),
    )
    await live(app, clock, end + timedelta(hours=1))
    assert len(channel.texts) == 3
    conv = await memory.get_conversation(CONVERSATION_ID)
    assert conv.unanswered_initiations == 0
    assert conv.last_initiation_date is None
    assert await app.life.can_initiate_today(CONVERSATION_ID, app.rhythm.local_date(clock.now()))
    rows = await memory._fetch_all("SELECT key FROM kv WHERE key LIKE 'last_proactive:%'", ())
    assert rows == []


async def test_a_restart_never_asks_the_same_step_twice(tmp_path: Path, persona: Persona) -> None:
    """问过的一步落在库里：重启、重新读日历、重新排，都不会再问一遍。

    发到一半进程死了的那一步停在 sending，也不再问——宁可少问一句。
    """
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    text = calendar(vevent("blk", start, end))
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [said("开始了没")], _noon_before(start), text,
    )
    await live(app, clock, start + timedelta(minutes=13))
    assert channel.texts == ["开始了没"]
    key = parse_ics(text, SYD).blocks[0].key
    # 假装 end 那一步发到一半进程死了
    assert await memory.study_claim(key, "end", end, clock.now())
    await memory.close()
    _OPEN.remove(memory)

    app2, channel2, _l, clock2, memory2, _s = await make(
        tmp_path, persona, [said("又问一遍"), said("又问一遍"), said("又问一遍")], clock.now(), text,
    )
    await live(app2, clock2, end + timedelta(hours=1))
    log = await statuses(memory2)
    assert log[(key, "start")][0] == "sent"
    assert log[(key, "end")][0] == "sending", "发到一半的那一步不再问"
    # 重启前排下的追问是唯一还能发的一句；开始了没、学到哪了都不会再来
    nudged = log.get((key, "nudge"), ("", ""))[0] == "sent"
    assert len(channel2.texts) == int(nudged), channel2.texts


async def test_a_failed_first_fetch_after_restart_keeps_what_was_scheduled(
    tmp_path: Path, persona: Persona
) -> None:
    """重启后第一次读日历就失败：手里什么都没有，不能拿"没有"去作废重启前排好的催学。

    原来会全部作废，半小时后再排又因为晚了二十分钟记成 missed——一次网络抖动丢掉一整块。
    到点时它自己再读一次日历、自己过闸。
    """
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    text = calendar(vevent("blk", start, end))
    app, _ch, _llm, clock, memory, _src = await make(tmp_path, persona, [], start - timedelta(hours=1), text)
    await app.study.sync_once()
    before = {j.id for j in await memory.pending_jobs("study_check")}
    assert len(before) == 2
    await memory.close()
    _OPEN.remove(memory)

    app2, channel, _l, clock2, memory2, source = await make(
        tmp_path, persona, [said("开始了没")], clock.now(), text,
    )
    source.fail = True
    assert not await app2.study.sync_once()
    assert {j.id for j in await memory2.pending_jobs("study_check")} == before, "一个都不能作废"

    job = min(await memory2.pending_jobs("study_check"), key=lambda j: j.run_at)
    clock2.set(job.run_at)
    source.fail = False  # 网络回来了：到点时自己再读一次
    await app2.scheduler.run_due_once()
    assert channel.texts == ["开始了没"]


async def test_a_broken_database_during_sync_never_raises(tmp_path: Path, persona: Persona, caplog) -> None:
    """同步时库出错（忙、锁住）：只记一行类名，不往外抛——不能拖垮调度和收发消息。"""
    app, *_rest = await make(
        tmp_path, persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY),
        calendar(vevent("blk", datetime(2026, 11, 18, 10, tzinfo=SYD), datetime(2026, 11, 18, 12, tzinfo=SYD))),
    )

    async def locked(*_a, **_kw):
        raise RuntimeError("database is locked")

    app.memory.kv_set = locked  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        assert await app.study.sync_once() is False
    assert "RuntimeError" in caplog.text


async def test_she_waits_for_her_reply_to_go_out_before_asking(tmp_path: Path, persona: Persona) -> None:
    """到点时他有话还没回：先等几分钟，不另起一句；等太久就算了。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, [], _noon_before(start), calendar(vevent("blk", start, end)),
    )
    await app.study.sync_once()
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID, discord_message_id=5, author_id=42, author_name="Leo",
            content="在吗", created_at=start,
        )
    )
    job = (await memory.pending_jobs("study_check"))[0]
    clock.set(job.run_at)
    await app.scheduler.run_due_once()
    again = await memory.get_job(job.id or 0)
    assert again is not None and again.status == "pending" and again.run_at > job.run_at
    assert channel.texts == []
    clock.set(job.run_at + timedelta(minutes=25))
    await app.scheduler.run_due_once()
    assert channel.texts == []
    log = await statuses(memory)
    assert ("skipped", "busy_replying") in log.values() or ("skipped", "too_late") in log.values()


async def test_owner_study_switch_and_status(tmp_path: Path, persona: Persona) -> None:
    """`!np study off/on` 管开关；`!np status` 里有下一个块几点、日历上次读成功是什么时候。"""
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    start, end = day_block(probe.rhythm, DAY)
    app, _ch, _llm, clock, memory, _src = await make(
        tmp_path, persona, [], _noon_before(start), calendar(vevent("blk", start, end, title="机密标题")),
    )
    await app.study.sync_once()
    ctx = owner.OwnerContext(
        memory=memory, rhythm=app.rhythm, scheduler=app.scheduler, life=app.life,
        conversation_id=CONVERSATION_ID, now=clock.now(), study=app.study,
    )
    assert "关了" in await owner.handle("!np study off", ctx)
    assert await memory.kv_get("study_off")
    assert "开了" in await owner.handle("!np study on", ctx)
    assert not await memory.kv_get("study_off")
    status = await owner.handle("!np status", ctx)
    assert start.astimezone(SYD).strftime("%m-%d %H:%M") in status
    assert "上次读成功" in status
    assert "机密标题" not in status, "标题只给模型看"


async def test_she_follows_her_own_timezone_not_a_fixed_offset(tmp_path: Path, persona: Persona) -> None:
    """她搬到他那个时区：他晚上的块就是她醒着的时候，按白天的块问。没有写死的时差。"""
    moved = persona.model_copy(deep=True)
    moved.timezone = "Australia/Sydney"
    moved.academic.home_timezone = "Australia/Sydney"
    app, *_ = await make(tmp_path, moved, [], datetime(2026, 11, 18, 12, 0, tzinfo=SYD))
    start = datetime(2026, 11, 18, 19, 0, tzinfo=SYD)
    assert not app.rhythm.is_sleeping(start)
    plan = app.study.plan([StudyBlock("k", start.astimezone(UTC), (start + timedelta(hours=1)).astimezone(UTC), "x")],
                          start - timedelta(hours=2))
    assert {p.components[0][0] for p in plan} == {"start", "end"}


def test_adding_study_watch_does_not_change_one_byte_of_the_system_prompt() -> None:
    """催学加进人设前后，system prompt 字节级相同（不变量 3）。

    "他把学习安排共享给你了"那句和哪块、几点只进 user 消息。放进 system 的话，
    没配地址、考完以后她还以为看得到他的日历，而且缓存会因此冷一次。
    """
    import yaml

    raw = yaml.safe_load((ROOT / "persona" / "persona.yaml").read_text(encoding="utf-8"))
    assert "study_watch" in raw
    before = dict(raw)
    del before["study_watch"]
    with_it = build_system(Persona.model_validate(raw)).encode("utf-8")
    without = build_system(Persona.model_validate(before)).encode("utf-8")
    assert with_it == without
    knows = raw["study_watch"]["knows"]
    assert knows and knows not in with_it.decode("utf-8")
    assert "学习安排" not in raw["owner"]["knows"], "这句要在 study_watch.knows，不在 owner.knows"


async def test_without_an_address_she_behaves_and_stores_exactly_as_before(
    tmp_path: Path, persona: Persona
) -> None:
    """人设开着、但没配地址：不联网、不排任务、不写 kv、提示词里没有这件事。

    合并前后她的行为和落库要完全一样——没配地址的时候，这个功能就当不存在。
    """
    from tests.test_integration import send

    assert persona.study_watch.enabled
    now = datetime(2026, 11, 18, 20, 0, tzinfo=NY)
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [], now=now)
    assert app.study.source is None and not app.study.configured
    assert not await app.study.sync_once()
    await send(app, "好累 有点想哭", at=now, msg_id=11)  # 低气压：没配的时候连时间戳都不记
    situation = await app._build_situation(now)
    assert persona.study_watch.knows not in situation
    rows = await memory._fetch_all("SELECT key FROM kv WHERE key LIKE 'study%'", ())
    assert rows == []
    assert await memory._fetch_all("SELECT * FROM study_log", ()) == []
    assert await memory.pending_jobs("study_check") == []


async def test_she_only_knows_about_his_schedule_while_the_feature_is_in_use(
    tmp_path: Path, persona: Persona
) -> None:
    """在用的时候，易变层里有那句话和他今天排的块（他那边的时间）；过了截止日就没有了。"""
    start = datetime(2026, 11, 18, 14, 0, tzinfo=SYD)
    now = (start - timedelta(hours=3)).astimezone(NY)
    app, *_rest, source = await make(
        tmp_path, persona, [], now, calendar(vevent("blk", start, start + timedelta(hours=2), title="COMP 复习"))
    )
    await app.study.sync_once()
    situation = await app._build_situation(now)
    assert persona.study_watch.knows in situation
    assert "他那边 11-18 14:00–16:00　COMP 复习" in situation

    past = persona.model_copy(deep=True)
    past.study_watch.until = date(2026, 11, 1)
    app.study.persona = past
    assert persona.study_watch.knows not in await app._build_situation(now)
    assert source.calls == 1


# ---------------------------------------------------------------------------
# 体检和真实发生的事对得上
# ---------------------------------------------------------------------------


async def test_doctor_counts_match_what_actually_happened(tmp_path: Path, persona: Persona) -> None:
    """**走完整条路再用体检去数**：几个块、问了几次、他回了几次，跟真实发生的一致；
    催学也不能被算成她"主动开口"。

    CLAUDE.md 最后那条：合成的库测的是"我以为她怎么工作"。
    """
    probe, *_ = await make(tmp_path / "p", persona, [], datetime(2026, 11, 17, 12, 0, tzinfo=NY))
    days = [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    spans = [day_block(probe.rhythm, d) for d in days]
    text = calendar(*(vevent(f"b{i}", s, e) for i, (s, e) in enumerate(spans)))
    script = [said(f"第{i}句") for i in range(20)]
    app, channel, _llm, clock, memory, _src = await make(
        tmp_path, persona, script, _noon_before(spans[0][0]), text,
    )
    replied = 0
    msg_id = 3000
    for i, (_s, e) in enumerate(spans):
        # 第一块他问完开始了没就回了一句；另外两块他一直没回
        await live(app, clock, _s + timedelta(minutes=13))
        if i == 0:
            await he_says(app, clock.now() + timedelta(minutes=2), msg_id)
            msg_id += 1
            replied += 1
        await live(app, clock, e + timedelta(hours=1))
    asked = len(channel.texts)
    assert asked == 3 * 2 + 2, channel.texts  # 每块开始和结束，没回的两块各追问一次
    await memory.close()
    _OPEN.remove(memory)

    report = doctor.run(app.settings.db_path, clock.now() + timedelta(minutes=1), days=14)
    study = next(f for f in report.findings if f.line.startswith("催学"))
    assert study.line == f"催学：3 个学习块，问了 {asked} 次，你回了 {replied} 次", report.render()
    opened = next((f for f in report.findings if "主动开口" in f.line), None)
    assert opened is None or "0.0 次/天" in opened.line or "没主动开过口" in opened.line, report.render()
