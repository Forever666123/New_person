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


def moment_engine(persona: Persona) -> LifeEngine:
    """只为了打 _opener_moment：它是纯函数，不碰数据库。

    直接构造省掉每次建库的开销，于是同一条测试能跑几千个样本——
    而"几千个样本"正是抓住这个 bug 所必需的。
    """
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    return LifeEngine(persona, rhythm, calendar, None, None, None, None, random.Random(0))


def test_the_opener_never_lands_in_her_sleep_at_any_start_time(persona: Persona) -> None:
    """整天每半小时 × 每个时刻二十个种子，一次都不能排进睡眠里。

    这条要密集采样才有意义。原来那版按两小时一档、一个种子，
    正好跳过了 01:30 —— 而那个时刻的兜底分支有约四分之一的种子
    会把开场排到她睡着的时候。三个种子也不够：漏检率还有四成。

    为什么这么较真：开场只有一次机会。真到了那一刻她在睡，
    handle_proactive_job 直接返回，任务标记 done，
    kv 标记加上全局唯一的 dedupe_key 让它再也排不上——**这一句就永远没有了**。
    """
    life = moment_engine(persona)
    cfg = persona.proactive.opener
    bad = []
    for hour in range(24):
        for minute in (0, 30):
            for seed in range(20):
                life.rng = random.Random(seed * 101 + hour * 7 + minute)
                now = datetime(2026, 9, 14, hour, minute, tzinfo=persona.tz)
                moment = life._opener_moment(now, cfg)
                if moment is None or life.rhythm.is_sleeping(moment):
                    bad.append(f"{hour:02d}:{minute:02d} 种子 {seed}")
    assert not bad, f"{len(bad)} 个组合把开场排进了睡眠里（或排不出来），例如：{bad[:5]}"


def test_the_opener_always_respects_the_minimum_delay(persona: Persona) -> None:
    """兜底那条路也不能绕过"不能刚启动就说话"。"""
    life = moment_engine(persona)
    cfg = persona.proactive.opener
    for hour in range(24):
        for seed in range(10):
            life.rng = random.Random(seed * 31 + hour)
            now = datetime(2026, 9, 14, hour, 0, tzinfo=persona.tz)
            moment = life._opener_moment(now, cfg)
            assert moment is not None
            gap = (moment - now).total_seconds() / 60
            assert gap >= cfg.min_delay_minutes, f"{hour} 点启动只等了 {gap:.0f} 分钟"


async def test_it_never_lands_while_she_is_asleep(tmp_path: Path, persona: Persona) -> None:
    """走完整条路（建库、排任务）再确认一次落点是醒着的。

    密集采样交给上面那条纯函数测试，这里只保证接起来也是对的。
    """
    for hour in (1, 4, 9, 15, 21):
        now = datetime(2026, 9, 14, hour, 30, tzinfo=persona.tz)
        life, memory, rhythm, _clock = await build(tmp_path / f"h{hour}", persona, now)
        try:
            moment = await life.ensure_opener(CONV, None)
            assert moment is not None, f"{hour}:30 排不出开场"
            assert not rhythm.is_sleeping(moment), f"{hour}:30 把开场排进了睡眠里"
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


def test_greetings_are_blocked_by_the_guard_not_just_by_the_prompt(persona: Persona) -> None:
    """提示词只是请求，never_say 才是拦截。

    开场是她说的第一句话，而且上下文全空——模型手里没有别的东西可抓，
    最容易滑到寒暄上去。只在提示词里写"不要打招呼"是不够的：
    那句话跟其他几十行指示挤在一起，而这一条错了就没有第二次机会。
    """
    from newperson import style_guard
    from newperson.models import ReplyPart

    for greeting in ("在吗", "你好", "好久没聊", "最近怎么样", "终于加上你了"):
        _fixed, bad = style_guard.enforce(
            [ReplyPart(text=greeting)], persona.style, persona.boundaries
        )
        assert bad, f"{greeting!r} 没有被拦下来"

    # 普通的一句自己的事要能干净地过去，别把话堵死了
    for fine in ("今天雪大到地铁都停了", "图书馆一个位置都没有"):
        _fixed, bad = style_guard.enforce(
            [ReplyPart(text=fine)], persona.style, persona.boundaries
        )
        assert not bad, f"{fine!r} 被误伤了"
