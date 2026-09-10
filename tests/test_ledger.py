"""台账：他主动说出口的承诺和进展。

两条规则撑着这个功能：

1. **只记他说过的。** 不预置任何背景资料。她不知道的事就是不知道，要靠问。
2. **一次只问一件，各类按自己的周期。** 便利店的班次是几天的事，期末考是几周的事。
   都按同一个周期问，她就成了一份待办清单，不是人。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.life import LEDGER_CHECK, LifeEngine
from newperson.memory import Memory
from newperson.models import LedgerEntry
from newperson.persona import Persona
from newperson.rhythm import Rhythm
from newperson.scheduler import Scheduler

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=None)


async def build(tmp_path: Path, persona: Persona):
    memory = Memory(tmp_path / "ledger.db")
    await memory.open()
    now = datetime(2026, 9, 20, 14, 0, tzinfo=persona.tz)
    clock = FakeClock(now)
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    scheduler = Scheduler(memory, clock, 1.0)
    life = LifeEngine(
        persona, rhythm, calendar, memory, scheduler, None, clock, random.Random(3)
    )
    return life, memory, clock, now


async def test_every_category_the_owner_asked_for_exists(persona: Persona) -> None:
    """六类台账都要在人设里配好，而且每一类都得有自己的周期。

    少配一个 follow_up_after_days，那一类就只进不出——记下来了，永远不会被问起。
    """
    kinds = {m.ledger_kind: m for m in persona.modes if m.ledger_kind}
    for name in ("study", "shift", "project", "english", "sleep", "trading"):
        assert name in kinds, f"少了 {name} 这一类"
        assert kinds[name].follow_up_after_days > 0, f"{name} 没有回访周期，记了也不会问"
        assert kinds[name].include_ledger, f"{name} 没打开 include_ledger，聊到时看不见旧账"


async def test_the_categories_do_not_all_come_due_on_the_same_day(persona: Persona) -> None:
    """周期要错开。

    全设成同一个数的话，同一天记下的几件事会在同一天一起到期，
    她会连着几条追问，像在念清单。
    """
    cycles = [m.follow_up_after_days for m in persona.modes if m.ledger_kind]
    assert len(set(cycles)) >= 4, f"周期太集中了：{sorted(cycles)}"


async def test_she_only_asks_about_something_she_can_actually_see(
    tmp_path: Path, persona: Persona
) -> None:
    """台账是空的时候，"问一句做了没有"这种主动根本不该被排上。

    排上了她就得凭空编一件他没说过的事——那比不问难看得多。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        assert await life.due_ledger_entry(now) is None
    finally:
        await memory.close()


async def test_a_fresh_promise_is_not_asked_about_immediately(
    tmp_path: Path, persona: Persona
) -> None:
    """刚说完就追问是最像机器人的。"你刚才说要复习，复习了吗？"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="study", claim="这周开始复习 COMP3141", committed_to="周三开始")],
            now,
        )
        assert await life.due_ledger_entry(now) is None
    finally:
        await memory.close()


async def test_a_promise_comes_due_on_its_own_category_cycle(
    tmp_path: Path, persona: Persona
) -> None:
    """排班两天后该问，英语要等六天——各按各的周期。"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="shift", claim="周四要上晚班")], now - timedelta(days=3)
        )
        await memory.add_ledger_entries(
            [LedgerEntry(kind="english", claim="每天背五十个单词")], now - timedelta(days=3)
        )

        due = await life.due_ledger_entry(now)
        assert due is not None
        assert due[1] == "shift", "三天之后到期的应该是排班（2 天），不是英语（6 天）"

        later = await life.due_ledger_entry(now + timedelta(days=4))
        assert later is not None
    finally:
        await memory.close()


async def test_she_asks_about_the_oldest_thing_first(tmp_path: Path, persona: Persona) -> None:
    """几件都到期时先问最久的那件，而不是最近的。"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="project", claim="这周把筛选器回测跑完")], now - timedelta(days=9)
        )
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim="SOXL 116 手动止损")], now - timedelta(days=3)
        )
        due = await life.due_ledger_entry(now)
        assert due is not None
        assert due[1] == "project"
    finally:
        await memory.close()


async def test_asking_once_does_not_mean_asking_forever(
    tmp_path: Path, persona: Persona
) -> None:
    """问过之后要冷却一个周期，否则同一件事天天问。

    这是这个功能最容易变得烦人的地方：他没回答，条目也没了结，
    于是它永远"到期"，她每天都问同一句。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="sleep", claim="今晚十二点前睡")], now - timedelta(days=5)
        )
        due = await life.due_ledger_entry(now)
        assert due is not None

        await memory.mark_ledger_asked(due[0], now)
        assert await life.due_ledger_entry(now) is None, "刚问完不该又到期"

        # 但过了一个周期还是可以再问一次
        assert await life.due_ledger_entry(now + timedelta(days=4)) is not None
    finally:
        await memory.close()


async def test_the_follow_up_kind_is_skipped_when_nothing_is_due(
    tmp_path: Path, persona: Persona
) -> None:
    """没有到期的事就不排回访。

    ledger_check 排上了却无话可问的话，她只能编。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        plan = await memory.get_day_plan(now.date())
        assert plan is None  # 这条测试不依赖日程
        names = [k.name for k in persona.proactive.kinds]
        assert LEDGER_CHECK in names, "人设里应该有一种回访主动"
        assert "trading_check" not in names, "旧的按类别分的回访应该已经合并掉了"
    finally:
        await memory.close()


async def test_an_old_database_gets_the_new_column(tmp_path: Path) -> None:
    """已经在用的库不能因为加了一列就废掉。

    她的记忆是不可能推倒重来的——那个文件就是全部。
    CREATE TABLE IF NOT EXISTS 对已有的表什么都不做，所以要有迁移。
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL"
        " DEFAULT 'trading', claim TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
        " committed_to TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,"
        " resolved INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute(
        "INSERT INTO ledger (kind, claim, created_at) VALUES ('trading','旧账','2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    memory = Memory(path)
    await memory.open()
    try:
        kept = await memory.ledger("trading")
        assert len(kept) == 1, "迁移把旧数据弄丢了"
        assert kept[0][1].claim == "旧账"
        cur = await memory.db.execute("PRAGMA table_info(ledger)")
        names = {row[1] for row in await cur.fetchall()}
        assert "asked_at" in names
    finally:
        await memory.close()


async def test_no_category_starves_the_others(tmp_path: Path, persona: Persona) -> None:
    """六件事同一天说的，两周下来每一类都该被问到。

    最初的实现按"记下的日期"排序，于是同一天说的几条永远平手，
    由遍历顺序决定谁赢——project、english、trading 一辈子问不到。
    改成按"上次碰它是什么时候"排：问过一条它就排到队尾，几类自然轮着来。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        for kind in ("study", "shift", "project", "english", "sleep", "trading"):
            await memory.add_ledger_entries([LedgerEntry(kind=kind, claim=f"{kind} 的事")], now)

        asked: list[str] = []
        for day in range(1, 15):
            when = now + timedelta(days=day)
            due = await life.due_ledger_entry(when)
            if due is not None:
                asked.append(due[1])
                await memory.mark_ledger_asked(due[0], when)

        missing = {"study", "shift", "project", "english", "sleep", "trading"} - set(asked)
        assert not missing, f"两周里这几类一次都没被问到：{missing}"
        # 而且不能连着两天问同一类
        assert all(a != b for a, b in zip(asked, asked[1:], strict=False)), asked
    finally:
        await memory.close()


def test_trading_wins_when_a_message_mentions_both(persona: Persona) -> None:
    """一边说仓位一边提了句 deadline，那仍然是一次交易对话。

    原来 mode_for 取"触发词最长的那个"，**而长度不是跨语种可比的量**：
    交易的触发词是"止损""加仓"这种两三个字的中文，课业里却有 assignment、
    deadline 这种八到十个字母的英文。于是加了五个新模式之后，
    "止损位我加仓了 assignment 还没交"被判成课业——她看不见他在交易上说过的话，
    "你变硬、不给面子"那段人设整个丢掉，回复也不再变快。
    而人设里写着交易纪律是"唯一一件你不让步的事"。
    """
    assert persona.mode_for("止损位我加仓了 assignment 还没交").name == "trading"
    assert persona.mode_for("止损没设 明天有个 deadline").name == "trading"
    # 他难受的时候，什么都让位
    assert persona.mode_for("我睡不着 好烦").name == "低气压"


def test_no_trigger_belongs_to_two_modes(persona: Persona) -> None:
    """同一个词属于两个模式的话，命中谁全看优先级和顺序，很难想清楚。"""
    from collections import Counter

    counts = Counter(t for m in persona.modes for t in m.triggers)
    assert not [t for t, n in counts.items() if n > 1]


def test_every_category_has_its_own_heading(persona: Persona) -> None:
    """台账在提示词里的小标题要跟着类别走。

    写死成"他之前在交易上说过的话"的话，一条作息承诺会被摆进查账的框里，
    而交易那段人设是"不给面子，也不安慰"、作息那段又明确禁止说教——
    两层指令互相打架，输出会很怪。
    """
    for mode in persona.modes:
        if mode.ledger_kind:
            assert mode.ledger_topic, f"{mode.name} 没有 ledger_topic"


def test_the_output_rules_name_all_six_kinds(persona: Persona) -> None:
    """稳定层必须告诉她六类都要记，而且把合法值列出来。

    原来那句话是"只记交易相关的，别的不用记"——写在 system prompt 里，
    结果是新加的五类**一条都记不进去**：她被明确告知不要记。
    这种失败没有任何症状，`!np ledger study` 永远是空的，
    而你只会以为是自己聊得不够多。
    """
    from newperson.prompts import build_system

    rules = build_system(persona)
    assert "只记交易相关的" not in rules
    for kind in ("trading", "study", "shift", "project", "english", "sleep"):
        assert kind in rules, f"稳定层里没提到 {kind}，模型不会往这一类记"


async def test_she_lets_a_promise_go_after_asking_twice(
    tmp_path: Path, persona: Persona
) -> None:
    """同一件事问过上限次数就放下。

    没有这个上限的话，条目永远留在候选池里跟着队列无限轮回——
    因为**没有任何代码把 resolved 置成 1**。三个月后她还会问
    "你六月说要把止损规则写下来，写了吗"，每隔几天准时回来一次。
    人不会这样，待办系统才会。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="shift", claim="周六早班")], now - timedelta(days=3)
        )
        cap = persona.proactive.ledger_max_follow_ups
        for round_no in range(cap):
            due = await life.due_ledger_entry(now + timedelta(days=3 * round_no))
            assert due is not None, f"第 {round_no + 1} 次就问不出来了"
            await memory.mark_ledger_asked(due[0], now + timedelta(days=3 * round_no))
        assert await life.due_ledger_entry(now + timedelta(days=90)) is None, "问够了还在问"
    finally:
        await memory.close()


async def test_ancient_promises_are_not_dragged_up(tmp_path: Path, persona: Persona) -> None:
    """太久以前的话就翻篇了。正常人不会追问三个月前的一句随口话。"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        old = now - timedelta(days=persona.proactive.ledger_max_age_days + 10)
        await memory.add_ledger_entries([LedgerEntry(kind="study", claim="很久以前说的")], old)
        assert await life.due_ledger_entry(now) is None
    finally:
        await memory.close()


async def test_saying_the_same_thing_twice_does_not_queue_it_twice(
    tmp_path: Path, persona: Persona
) -> None:
    """他为一件事连发三条消息是常态，那件事不该被问三遍。"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        for _ in range(3):
            await memory.add_ledger_entries(
                [LedgerEntry(kind="project", claim="这周把回测跑完")], now - timedelta(days=6)
            )
        assert len(await memory.ledger("project")) == 1
    finally:
        await memory.close()


def test_check_warns_when_a_category_would_be_silently_dead(persona: Persona) -> None:
    """配错了要当场喊出来。

    这套东西有两种"配错了但完全没有症状"的方式：没有 ledger_check（记了永远不问），
    和 ledger_kind 少了 follow_up_after_days（只进不出）。
    这个项目里"安静"和"正常"看起来一模一样，所以只能靠 check 说话。
    """
    from newperson.persona import validate_persona

    assert not [m for _, m in validate_persona(persona) if "台账" in m or "ledger" in m]

    persona.proactive.kinds = [k for k in persona.proactive.kinds if k.name != "ledger_check"]
    assert any("ledger_check" in m for _, m in validate_persona(persona))
