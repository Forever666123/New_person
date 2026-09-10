"""第一次上线的那一句。

它守着两件相反的事：**要发**（不发的话第一天看起来像坏了），
但**只发一次**、**不在启动那一刻发**、**已经聊过就绝不发**。

最后一条最要紧：老库升级上来突然冒出一句开场白，比没有开场白难看得多。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.life import OPENER_KEY, LifeEngine
from newperson.memory import Memory
from newperson.models import IncomingMessage
from newperson.persona import Persona
from newperson.rhythm import Rhythm
from newperson.scheduler import Scheduler

CONV = "dm"


async def build(tmp_path: Path, persona: Persona, now: datetime):
    memory = Memory(tmp_path / "opener.db")
    await memory.open()
    clock = FakeClock(now)
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    scheduler = Scheduler(memory, clock, 1.0)
    life = LifeEngine(
        persona, rhythm, calendar, memory, scheduler, None, clock, random.Random(3)
    )
    return life, memory, rhythm, clock


def awake_moment(rhythm: Rhythm, persona: Persona) -> datetime:
    """找一个她确实醒着的时刻，别把作息写死在测试里。"""
    probe = datetime(2026, 9, 14, 9, 0, tzinfo=persona.tz)
    for _ in range(24 * 4):
        if not rhythm.is_sleeping(probe):
            return probe
        probe += timedelta(minutes=15)
    raise AssertionError("找不到她醒着的时刻")


async def test_it_schedules_something_on_a_fresh_install(tmp_path: Path, persona: Persona) -> None:
    """空库 + 第一次启动 = 排一句开场。

    不排的话，第一天是他发消息进去然后干等，看起来像坏了。
    """
    life, memory, rhythm, _clock = await build(
        tmp_path, persona, datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    )
    try:
        moment = await life.ensure_opener(CONV, None)
        assert moment is not None
        jobs = await memory.pending_jobs("proactive", CONV)
        assert len(jobs) == 1
        assert jobs[0].reason == "opener"
    finally:
        await memory.close()


async def test_it_never_fires_twice(tmp_path: Path, persona: Persona) -> None:
    """她重启多少次都只开一次场。

    容器会因为部署、宿主机维护、OOM 反复重启。每次重启都来一句"随口说说"
    是这个功能最容易崩掉的方式。
    """
    life, memory, _rhythm, _clock = await build(
        tmp_path, persona, datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    )
    try:
        first = await life.ensure_opener(CONV, None)
        assert first is not None
        for _ in range(5):
            assert await life.ensure_opener(CONV, None) is None
        assert len(await memory.pending_jobs("proactive", CONV)) == 1
    finally:
        await memory.close()


async def test_an_existing_conversation_never_gets_an_opener(
    tmp_path: Path, persona: Persona
) -> None:
    """库里已经有话了就绝不开场。

    老库升级上来突然冒出一句"随口说说"，比没有开场白难看得多——
    你们聊了三个月，她忽然像刚认识一样开口。
    """
    life, memory, _rhythm, _clock = await build(
        tmp_path, persona, datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    )
    try:
        await memory.add_user_message(
            IncomingMessage(
                conversation_id=CONV,
                discord_message_id=1,
                author_id=42,
                author_name="Leo",
                content="在吗",
                created_at=datetime(2026, 9, 13, 20, 0, tzinfo=persona.tz),
            )
        )
        assert await life.ensure_opener(CONV, None) is None
        assert await memory.pending_jobs("proactive", CONV) == []
        # 而且要记下来，免得每次启动都去查一遍
        assert (await memory.kv_get(OPENER_KEY) or "").startswith("skipped")
    finally:
        await memory.close()


async def test_it_does_not_fire_the_moment_the_container_starts(
    tmp_path: Path, persona: Persona
) -> None:
    """启动后一分钟就冒出一句，那是程序开机的样子，不是人。

    这条是整个功能的分界线：同样一句话，来得太准时就露馅。
    """
    now = datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    life, memory, _rhythm, _clock = await build(tmp_path, persona, now)
    try:
        cfg = persona.proactive.opener
        moment = await life.ensure_opener(CONV, None)
        assert moment is not None
        gap_minutes = (moment - now).total_seconds() / 60
        assert gap_minutes >= cfg.min_delay_minutes
    finally:
        await memory.close()


async def test_it_never_lands_while_she_is_asleep(tmp_path: Path, persona: Persona) -> None:
    """半夜把机器装起来，开场要等到她醒着。

    抽了很多个起始时刻，因为作息本身是抽签出来的——
    写死"凌晨四点她在睡"那种测试，换个种子就假了。
    """
    for hour in range(0, 24, 2):
        now = datetime(2026, 9, 14, hour, 0, tzinfo=persona.tz)
        life, memory, rhythm, _clock = await build(tmp_path / f"h{hour}", persona, now)
        try:
            moment = await life.ensure_opener(CONV, None)
            assert moment is not None, f"{hour} 点启动时排不出开场"
            assert not rhythm.is_sleeping(moment), f"{hour} 点启动，开场排在了她睡觉的时候"
        finally:
            await memory.close()


async def test_turning_it_off_means_off(tmp_path: Path, persona: Persona) -> None:
    """人设里关掉就真的不发。行为参数在 yaml 里，不在代码里。"""
    persona.proactive.opener.enabled = False
    life, memory, _rhythm, _clock = await build(
        tmp_path, persona, datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    )
    try:
        assert await life.ensure_opener(CONV, None) is None
        assert await memory.pending_jobs("proactive", CONV) == []
    finally:
        await memory.close()


async def test_the_note_forbids_saying_hello(persona: Persona) -> None:
    """开场白的措辞是这个功能的全部风险所在。

    他们认识一年了。任何形式的"你好""好久没聊""在吗"都是聊天机器人的味道，
    而这恰好是她说的第一句话——第一印象就定在那儿了。
    """
    note = persona.proactive.opener.note
    assert note.strip(), "开场没有给模型任何指示，它会默认去打招呼"
    for banned in ("不要打招呼", "不要自我介绍"):
        assert banned in note


@pytest.mark.parametrize("seed", [1, 7, 13, 29])
async def test_the_moment_is_not_always_the_same(
    tmp_path: Path, persona: Persona, seed: int
) -> None:
    """不同的机器、不同的时候装，开场落点不该总是同一个偏移。

    固定偏移是另一种"每天同一分钟上下线"——最容易看出是程序的地方。
    """
    now = datetime(2026, 9, 14, 12, 0, tzinfo=persona.tz)
    gaps = []
    for i in range(6):
        life, memory, _rhythm, _clock = await build(tmp_path / f"s{seed}-{i}", persona, now)
        life.rng = random.Random(seed * 100 + i)
        try:
            moment = await life.ensure_opener(CONV, None)
            assert moment is not None
            gaps.append(round((moment - now).total_seconds() / 60))
        finally:
            await memory.close()
    assert len(set(gaps)) > 1, f"六次抽出来一样的偏移：{gaps}"
