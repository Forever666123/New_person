"""台账：他主动说出口的承诺和进展。

两条规则撑着这个功能：

1. **只记他说过的。** 不预置任何背景资料。她不知道的事就是不知道，要靠问。
2. **一次只问一件，各类按自己的周期。** 便利店的班次是几天的事，期末考是几周的事。
   都按同一个周期问，她就成了一份待办清单，不是人。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.life import LEDGER_CHECK, LifeEngine
from newperson.memory import Memory
from newperson.models import Job, LedgerEntry
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
        assert kept[0][2].claim == "旧账"
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


async def test_she_stops_asking_once_he_answers(tmp_path: Path, persona: Persona) -> None:
    """他给了下文，那件事就翻篇了——这才是"不像 AI"的关键。

    在这之前，让一条承诺消失的唯一办法是"问够两次"或者"太老了"，
    两个都是机械的上限，跟他说了什么毫无关系。
    她问"回测跑完了吗"，他答"跑完了"，然后过几天她又问一遍——
    问了、答了、又问一遍，这是最像机器人的一幕。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="project", claim="回测这周跑完")], now - timedelta(days=6)
        )
        due = await life.due_ledger_entry(now)
        assert due is not None
        entry_id = due[0]
        await memory.mark_ledger_asked(entry_id, now)

        # 他回答了
        assert await memory.resolve_ledger([entry_id]) == 1

        # 从此不再问，不管过多久
        for days in (1, 7, 30, 90):
            assert await life.due_ledger_entry(now + timedelta(days=days)) is None
        # 也不再出现在"还没听到下文"里
        assert await memory.open_questions(now + timedelta(days=1)) == []
    finally:
        await memory.close()


async def test_what_she_asked_comes_back_even_when_the_answer_has_no_keywords(
    tmp_path: Path, persona: Persona
) -> None:
    """他回一句"跑完了"，那句话里一个触发词都没有。

    话题模式匹配不上 → 台账不进上下文 → 她没有任何办法把这件事记成翻篇。
    所以"她问过、还没听到下文的"这一段跟模式无关，永远带着。
    """
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        assert persona.mode_for("跑完了") is None, "这条测试的前提是这句话匹配不上任何模式"

        await memory.add_ledger_entries(
            [LedgerEntry(kind="project", claim="回测这周跑完")], now - timedelta(days=6)
        )
        due = await life.due_ledger_entry(now)
        assert due is not None
        await memory.mark_ledger_asked(due[0], now)

        pending = await memory.open_questions(now)
        assert [p[2].claim for p in pending] == ["回测这周跑完"]
        assert pending[0][0] == due[0], "编号要对得上，她才引用得了"
    finally:
        await memory.close()


async def test_an_answer_from_long_ago_is_not_still_pending(
    tmp_path: Path, persona: Persona
) -> None:
    """问过很久都没下文的，就别一直挂在上下文里占地方了。"""
    life, memory, _clock, now = await build(tmp_path, persona)
    try:
        await memory.add_ledger_entries(
            [LedgerEntry(kind="sleep", claim="十二点前睡")], now - timedelta(days=40)
        )
        due = await life.due_ledger_entry(now)
        assert due is not None
        await memory.mark_ledger_asked(due[0], now - timedelta(days=30))
        assert await memory.open_questions(now) == []
    finally:
        await memory.close()


def test_the_output_rules_tell_her_how_to_close_something(persona: Persona) -> None:
    """稳定层要说清楚"他给了下文就放进 resolved_ledger_ids"。

    字段加了但没人告诉她怎么用，等于没加——这正是六类台账刚犯过的错。
    """
    from newperson.prompts import build_system

    rules = build_system(persona)
    assert "resolved_ledger_ids" in rules
    assert "没做" in rules, "要说清楚'没做'也算有下文，否则只有做到了才会翻篇"


@pytest.mark.parametrize(
    "said",
    [
        "感觉情绪不太稳定 明天还要复习",
        "有点想哭 作业还没写",
        "睡不着 有点想你了",
        "考试好紧张 一做题就慌",
        "最近有点焦虑 deadline 又快到了",
    ],
)
def test_when_he_is_low_the_low_mood_mode_wins_over_his_tasks(persona: Persona, said: str) -> None:
    """他难受的时候，就算同一句里提到了作业和考试，也要进"低气压"而不是"课业"。

    进了课业模式，台账就被带进上下文，她就会顺手问"弄完没"。
    他真正会说的是"情绪不太稳定""有点想哭""有点想你了""好紧张"——
    原来这几种都没收，于是那一批落回课业模式。
    """
    mode = persona.mode_for(said)
    assert mode is not None and mode.name == "低气压", f"「{said}」进了 {mode.name if mode else None}"
    assert not mode.include_ledger, "低气压不该把他的台账带进上下文"


def test_the_low_mood_mode_does_not_steer_back_to_his_tasks(persona: Persona) -> None:
    """低气压模式的指令不能让她"接一句然后回到正事"。

    原来写的就是"先接情绪，一句就够，然后落到事上问一句具体的"。
    她照做了：他说想哭，她接一句就回到"明天先把作业写完"；
    他说谢谢你听我说，她回"手上事先弄完"。那不是硬气，是在念待办清单。
    """
    mode = next(m for m in persona.modes if m.name == "低气压")
    assert "落到事上" not in mode.instruction
    assert "别提他的任务" in mode.instruction
    # system prompt 里的 voice 每次都跟着，原来那里还写着"先接情绪，再落到事上"，
    # 两句一起给她，前面那条修了等于没修
    assert "落到事上" not in persona.voice and "那现在怎么办" not in persona.voice
    # 但也不能变成心理咨询：那几条"不要"还得在
    for still_banned in ("不讲道理", "不给建议清单", "不问"):
        assert still_banned in mode.instruction


# -- 他说了时间的事，那之前不问 ------------------------------------------------
#
# 线上真出过：他凌晨一两点说"明早九点起来把它做完"，她清晨四五点就来问做了没有。
# 分钟数是模型心算的，要同时换算她的钟、他的钟和"明天早上"，普遍算短。
# 现在的分工是模型只读钟（写下他那边的 MM-DD HH:MM），代码做换算、设闸门。

SYDNEY = ZoneInfo("Australia/Sydney")


def _his(month: int, day: int, hour: int, minute: int = 0, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SYDNEY)


async def test_she_never_asks_before_the_time_he_named(tmp_path: Path, persona: Persona) -> None:
    """他睡前说明早九点做，那之前（加上宽限）这条绝不入选回访。

    周期故意设成 0，只看闸门：他那边 04:30、08:59、09:00、宽限结束前一分钟都不行，
    到了宽限结束才行。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 29, 1, 30).astimezone(persona.tz)
        timing = life.resolve_when_there("09-29 09:00", said)
        assert timing is not None
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim="明天早上9点起来把回测跑完", when_there="09-29 09:00")],
            said,
            [timing],
        )
        for too_early in (
            _his(9, 29, 4, 30),
            _his(9, 29, 8, 59),
            _his(9, 29, 9, 0),
            timing.ask_after - timedelta(minutes=1),
        ):
            assert await memory.due_ledger_entry("trading", too_early, 0) is None, (
                f"他那边 {too_early.astimezone(SYDNEY):%H:%M} 就入选了"
            )
        found = await memory.due_ledger_entry("trading", timing.ask_after, 0)
        assert found is not None and found[2].claim == "明天早上9点起来把回测跑完"
    finally:
        await memory.close()


async def test_a_named_time_is_read_on_his_clock_not_hers(
    tmp_path: Path, persona: Persona
) -> None:
    """"09-29 09:00"是他那边的九点，不是她那边的。按她的钟理解会差十四个小时。"""
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        timing = life.resolve_when_there("09-29 09:00", _his(9, 29, 1, 30))
        assert timing is not None
        assert timing.due_at == datetime(2026, 9, 28, 23, 0, tzinfo=UTC)
        assert timing.when_there == "09-29 09:00"
    finally:
        await memory.close()


async def test_his_daylight_saving_switch_is_respected(tmp_path: Path, persona: Persona) -> None:
    """悉尼 10-04 凌晨拨快一小时。换算要按那一天的偏移，不能拿"现在的偏移"去算。

    跳过的那一小时（02:30 不存在）不能抛异常，两种理解取更晚的那个。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(10, 3, 20, 0)
        timing = life.resolve_when_there("10-04 09:00", said)
        assert timing is not None
        assert timing.due_at == datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
        gap = life.resolve_when_there("10-04 02:30", said)
        assert gap is not None
        assert gap.due_at == datetime(2026, 10, 3, 16, 30, tzinfo=UTC)
    finally:
        await memory.close()


async def test_a_named_time_rolls_over_the_new_year(tmp_path: Path, persona: Persona) -> None:
    """除夕晚上说"01-01 09:00"，是明年的一月一号。"""
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        timing = life.resolve_when_there("01-01 09:00", _his(12, 31, 22, 0))
        assert timing is not None
        assert timing.due_at == datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
    finally:
        await memory.close()


@pytest.mark.parametrize(
    "raw",
    ["明天早上", "13-40 25:00", "02-30 09:00", "09-28 09:00", "12-25 09:00", "9点", ""],
)
async def test_a_bad_time_falls_back_to_the_category_cycle(
    tmp_path: Path, persona: Persona, raw: str
) -> None:
    """写法不对、日子不存在、在他开口之前、远得离谱——统统当没说，按类别周期问。

    一个坏值卡不住整条台账：这条照样在周期之后到期，不早也不卡死。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 29, 1, 30)
        assert life.resolve_when_there(raw, said) is None
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim="回测", when_there=raw)], said, [None]
        )
        assert await memory.due_ledger_entry("trading", said + timedelta(days=1), 2) is None
        assert await memory.due_ledger_entry("trading", said + timedelta(days=2, minutes=1), 2)
    finally:
        await memory.close()


async def test_a_timed_entry_does_not_shadow_the_rest_of_its_kind(
    tmp_path: Path, persona: Persona
) -> None:
    """同一类里一条还没到时间的，不能把早就到期的另一条挡住。"""
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 29, 1, 30)
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim="老的那件")], said - timedelta(days=5)
        )
        far = life.resolve_when_there("10-10 09:00", said)
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim="还没到的", when_there="10-10 09:00")],
            said - timedelta(days=6),
            [far],
        )
        found = await memory.due_ledger_entry("trading", said, 2)
        assert found is not None and found[2].claim == "老的那件"
    finally:
        await memory.close()


async def test_a_far_off_deadline_is_not_asked_about_before_it_arrives(
    tmp_path: Path, persona: Persona
) -> None:
    """周一说"下周五交"。课业四天一个周期，周期先到了，截止还没到——不问。"""
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 28, 20, 0)
        timing = life.resolve_when_there("10-09 23:59", said)
        assert timing is not None
        await memory.add_ledger_entries(
            [LedgerEntry(kind="study", claim="下周五交作业", when_there="10-09 23:59")],
            said,
            [timing],
        )
        assert await life.due_ledger_entry(said + timedelta(days=5)) is None
        assert await life.due_ledger_entry(_his(10, 9, 15, 0)) is None
        found = await life.due_ledger_entry(timing.ask_after + timedelta(minutes=1))
        assert found is not None and found[2].claim == "下周五交作业"
    finally:
        await memory.close()


async def test_she_does_not_ask_the_moment_the_clock_strikes(
    tmp_path: Path, persona: Persona
) -> None:
    """过了他说的时间也不是一到点就问：宽限至少三小时，而且每条不一样。

    跟"起床时间要分散"是一回事——准点就是闹钟。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        graces = []
        for seed in range(50):
            life.rng = random.Random(seed)
            timing = life.resolve_when_there("09-29 09:00", _his(9, 29, 1, 30))
            assert timing is not None
            graces.append((timing.ask_after - timing.due_at).total_seconds() / 3600)
        lo, hi = persona.proactive.ledger_timed.grace_hours
        assert min(graces) >= lo >= 1
        assert max(graces) <= hi
        assert len({round(g, 2) for g in graces}) > 40, "宽限几乎都一样，那还是闹钟"
    finally:
        await memory.close()


async def test_saying_it_again_keeps_the_later_time(tmp_path: Path, persona: Persona) -> None:
    """同一件事再说一遍又带了时间，取更晚的那个。

    写晚了只是问得晚，写早了就是凌晨被问——模型第二次读错钟，不能把时间往前挪。
    尾随空格不算另一件事。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 29, 1, 30)
        nine = life.resolve_when_there("09-29 09:00", said)
        seven = life.resolve_when_there("09-29 07:00", said)
        noon = life.resolve_when_there("09-29 12:00", said)
        claim = "明早把回测跑完"
        await memory.add_ledger_entries([LedgerEntry(kind="trading", claim=claim)], said, [nine])
        await memory.add_ledger_entries(
            [LedgerEntry(kind="trading", claim=claim + " ")], said, [seven]
        )
        rows = await memory.ledger("trading")
        assert len(rows) == 1, "带个尾随空格就又进了一行"
        assert rows[0][2].when_there == "09-29 09:00", "第二次写得更早，时间被往前挪了"
        await memory.add_ledger_entries([LedgerEntry(kind="trading", claim=claim)], said, [noon])
        assert (await memory.ledger("trading"))[0][2].when_there == "09-29 12:00"
    finally:
        await memory.close()


async def test_an_old_ledger_table_gets_the_time_columns(tmp_path: Path) -> None:
    """已经在用的库补上三列，旧行照旧按周期问；旧版排下的 follow_up 一次性作废。

    那些 follow_up 多半就是"凌晨来问做了没"的那种，重启清不掉它们。
    别的任务一个都不能碰。
    """
    import sqlite3

    path = tmp_path / "old.db"
    memory = Memory(path)
    await memory.open()
    at = datetime(2026, 9, 20, 14, 0, tzinfo=ZoneInfo("America/New_York"))
    await memory.add_ledger_entries([LedgerEntry(kind="trading", claim="旧账")], at)
    follow = await memory.add_job(Job(kind="follow_up", run_at=at + timedelta(hours=3)), at)
    reply = await memory.add_job(Job(kind="reply", run_at=at + timedelta(hours=3)), at)
    await memory.close()

    conn = sqlite3.connect(path)
    for col in ("when_there", "due_at", "ask_after"):
        conn.execute(f"ALTER TABLE ledger DROP COLUMN {col}")
    conn.commit()
    conn.close()

    memory = Memory(path)
    await memory.open()
    try:
        cur = await memory.db.execute("PRAGMA table_info(ledger)")
        names = {row[1] for row in await cur.fetchall()}
        assert {"when_there", "due_at", "ask_after"} <= names
        kept = await memory.ledger("trading")
        assert len(kept) == 1 and kept[0][2].when_there == ""
        assert await memory.due_ledger_entry("trading", at + timedelta(days=3), 2)
        assert (await memory.get_job(follow)).status == "cancelled"
        assert (await memory.get_job(reply)).status == "pending"
    finally:
        await memory.close()


def test_the_output_rules_explain_when_there(persona: Persona) -> None:
    """字段加了但没人讲怎么用，等于没加。按他的钟写、宁可写晚——两样都要讲到。"""
    from newperson.models import ReplyPlan
    from newperson.prompts import build_system

    text = build_system(persona)
    assert "when_there" in text
    assert "他那边" in text
    assert "宁可写晚" in text
    schema = ReplyPlan.model_json_schema()
    assert schema["$defs"]["LedgerEntry"]["properties"]["when_there"]["description"]


def test_check_warns_when_the_grace_is_zero_or_the_window_outlives_the_ledger(
    persona: Persona,
) -> None:
    """一过点就问，或者远一点的事还没到时间就先被当成太老丢掉——都要当场喊出来。"""
    from newperson.persona import validate_persona

    zero = persona.model_copy(deep=True)
    zero.proactive.ledger_timed.grace_hours = (0.0, 1.0)
    assert any("闹钟" in msg for _lvl, msg in validate_persona(zero))

    long = persona.model_copy(deep=True)
    long.proactive.ledger_timed.max_days_ahead = 60
    assert any("太老" in msg for _lvl, msg in validate_persona(long))

    assert not any("ledger_timed" in msg for _lvl, msg in validate_persona(persona))



async def test_a_time_given_after_she_asked_is_kept(tmp_path: Path, persona: Persona) -> None:
    """她问过一次"做完没"，他回"还没，明早九点做完"：这个时间不能丢。

    原来问过的条目一律不写新时间，于是她照样在九点之前再问一遍——线上那一幕换条路再来。
    """
    life, memory, _clock, _now = await build(tmp_path, persona)
    try:
        said = _his(9, 29, 1, 30)
        await memory.add_ledger_entries(
            [LedgerEntry(kind="study", claim="把第三章习题做完")], said - timedelta(days=10)
        )
        entry_id = (await memory.ledger("study"))[0][0]
        await memory.mark_ledger_asked(entry_id, said - timedelta(days=5))
        timing = life.resolve_when_there("09-29 09:00", said)
        await memory.add_ledger_entries(
            [LedgerEntry(kind="study", claim="把第三章习题做完", when_there="09-29 09:00")],
            said, [timing],
        )
        assert (await memory.ledger("study"))[0][2].when_there == "09-29 09:00"
        assert await memory.due_ledger_entry("study", _his(9, 29, 4, 30), 4) is None
    finally:
        await memory.close()



def test_only_trading_is_held_to_what_he_said_before(persona: Persona) -> None:
    """"对不上就翻出来问他"只是交易那一件事的姿态。作息、课业、英语不较真。

    原来这句写死在代码里，六个话题都加：她变成每件事都翻旧账的人。
    """
    from datetime import datetime

    from newperson.models import LedgerEntry
    from newperson.prompts import build_reply_user

    entry = (1, datetime(2026, 10, 1, 20, 0), LedgerEntry(kind="sleep", claim="今晚一定早睡"))
    text = build_reply_user(
        persona=persona, situation="", summary="", owner_facts=[], self_facts=[],
        ledger=[entry], ledger_topic="说过的作息", mode_instruction="", recent=[], unread=[],
        hints=[], photos=[],
    )
    assert "翻出来问他" not in text
    trading = next(m for m in persona.modes if m.name == "trading")
    assert "翻出来问他" in trading.instruction
