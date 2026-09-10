"""端到端：一条消息从进来到发出去，走完整条链路。

不连 Discord，不连模型。假 channel 记录发了什么，假 client 按脚本返回。
这个测试的价值在于抓组装错误：单个模块都对，串起来不对。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from newperson.attention import AttentionPolicy
from newperson.brain import Brain
from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.config import Settings
from newperson.delivery import Deliverer
from newperson.discord_bot import CONVERSATION_ID, App
from newperson.life import LifeEngine
from newperson.media import MediaService, NullImageGenerator, PhotoLibrary
from newperson.memory import Memory
from newperson.models import (
    DayPlan,
    IncomingMessage,
    PlanEvent,
    ProactivePlan,
    ReplyPart,
    ReplyPlan,
)
from newperson.persona import Persona
from newperson.rhythm import Rhythm
from newperson.scheduler import Scheduler

TZ = ZoneInfo("America/New_York")
EVENING = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)

_OPEN: list[Memory] = []


@pytest.fixture(autouse=True)
async def _close_databases():
    """每个测试跑完把连接关掉，否则 aiosqlite 的后台线程会挂住 pytest。"""
    yield
    for mem in _OPEN:
        await mem.close()
    _OPEN.clear()


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeChannel:
    """记录她发了什么。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str | None, bool]] = []
        self._next_id = 500

    def typing(self):
        return FakeTyping()

    async def send(self, content=None, *, file=None, reference=None):
        self._next_id += 1
        self.sent.append((content, file is not None))
        return SimpleNamespace(id=self._next_id)

    async def fetch_message(self, message_id: int):
        return SimpleNamespace(id=message_id, add_reaction=self._noop)

    async def _noop(self, *_a, **_kw):
        return None

    @property
    def texts(self) -> list[str]:
        return [c for c, _ in self.sent if c]


class ScriptedLLM:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            parsed_output=item,
            usage=SimpleNamespace(
                input_tokens=100,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=0,
                output_tokens=20,
            ),
            stop_reason="end_turn",
        )


async def build(tmp_path: Path, persona: Persona, script: list, *, now=EVENING):
    settings = Settings(
        discord_bot_token="x",
        owner_user_id=42,
        db_path=tmp_path / "e2e.db",
        downloads_dir=tmp_path / "dl",
        generated_dir=tmp_path / "gen",
        photos_index=tmp_path / "none.yaml",
        delay_scale=0.0001,  # 让测试不用真的等
    )
    clock = FakeClock(now)
    rng = random.Random(7)
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    attention = AttentionPolicy(persona, rhythm, settings.delay_scale)
    memory = Memory(settings.db_path)
    await memory.open()
    _OPEN.append(memory)
    scheduler = Scheduler(memory, clock, settings.delay_scale)
    llm = ScriptedLLM(script)
    brain = Brain(SimpleNamespace(messages=llm), settings, persona, memory)
    library = PhotoLibrary(settings.photos_index)
    library.load()
    media = MediaService(library, NullImageGenerator(), settings.generated_dir)
    life = LifeEngine(persona, rhythm, calendar, memory, scheduler, brain, clock, rng)
    deliverer = Deliverer(clock, attention, rng, lambda p: p)

    app = App(
        settings=settings,
        persona=persona,
        clock=clock,
        rhythm=rhythm,
        calendar=calendar,
        attention=attention,
        memory=memory,
        scheduler=scheduler,
        brain=brain,
        media=media,
        life=life,
        deliverer=deliverer,
        rng=rng,
    )
    channel = FakeChannel()
    app._channel = channel
    app.client = SimpleNamespace(user=SimpleNamespace(id=999))
    scheduler.register("reply", app.handle_reply_job)
    scheduler.register("proactive", app.handle_proactive_job)
    scheduler.register("follow_up", app.handle_proactive_job)
    scheduler.register("memory_update", app.handle_memory_update_job)
    return app, channel, llm, clock, memory


async def send(app: App, text: str, *, at: datetime, msg_id: int = 1) -> None:
    """跳过 discord.Message，直接走存库和排期。"""
    await app.memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=msg_id,
            author_id=42,
            author_name="Leo",
            content=text,
            created_at=at,
        )
    )
    await app._schedule_reply(at)


async def drain(app: App, clock: FakeClock, hops: int = 6) -> None:
    """把到期的任务跑完，时间不够就往前拨。"""
    for _ in range(hops):
        if await app.scheduler.run_due_once():
            continue
        nxt = await app.memory.next_job_run_at()
        if nxt is None:
            return
        clock.set(nxt)


# ---------------------------------------------------------------------------


async def test_a_message_gets_a_reply(tmp_path: Path, persona: Persona) -> None:
    plan = ReplyPlan(parts=[ReplyPart(text="在"), ReplyPart(text="怎么了")])
    app, channel, _llm, clock, _mem = await build(tmp_path, persona, [plan])
    await send(app, "在吗", at=EVENING)
    await drain(app, clock)
    assert channel.texts == ["在", "怎么了"]


async def test_she_does_not_reply_instantly(tmp_path: Path, persona: Persona) -> None:
    """收到消息不会当场就发出去，任务的时间必须在未来。"""
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await send(app, "在吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert jobs and jobs[0].run_at > EVENING


async def test_what_she_said_is_remembered(tmp_path: Path, persona: Persona) -> None:
    """她自己说过的话要进历史，否则下一轮会前后矛盾。"""
    plan = ReplyPlan(parts=[ReplyPart(text="知道了")])
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await send(app, "我加仓了", at=EVENING)
    await drain(app, clock)
    history = await memory.recent_messages(CONVERSATION_ID, 10)
    assert [(m.author_kind, m.content) for m in history] == [("user", "我加仓了"), ("bot", "知道了")]


async def test_messages_are_marked_read(tmp_path: Path, persona: Persona) -> None:
    app, _channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING)
    await drain(app, clock)
    assert await memory.unread_messages(CONVERSATION_ID) == []


async def test_an_empty_plan_sends_nothing(tmp_path: Path, persona: Persona) -> None:
    """有些消息本来就不用回。空的 parts 不该发出空消息。"""
    app, channel, _llm, clock, _mem = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await send(app, "晚安", at=EVENING)
    await drain(app, clock)
    assert channel.sent == []


async def test_a_model_failure_is_silent_and_retried(tmp_path: Path, persona: Persona) -> None:
    """模型挂了她就是没回，绝不能发出错误提示。"""
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [None])
    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=2)
    assert channel.sent == []
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert jobs, "失败之后应该排了重试"


async def test_rapid_fire_gets_one_reply(tmp_path: Path, persona: Persona) -> None:
    """他连发三条，她回一次，不是三次。"""
    plan = ReplyPlan(parts=[ReplyPart(text="慢点说")])
    app, channel, llm, clock, _mem = await build(tmp_path, persona, [plan])
    for i in range(3):
        await send(app, f"第{i}条", at=EVENING + timedelta(seconds=i * 5), msg_id=100 + i)
    await drain(app, clock)
    assert len(llm.calls) == 1
    assert channel.texts == ["慢点说"]


async def test_the_ledger_records_what_he_said(tmp_path: Path, persona: Persona) -> None:
    """交易上说过的话要存下来，以后用来指出前后矛盾。"""
    from newperson.models import LedgerEntry

    plan = ReplyPlan(
        parts=[ReplyPart(text="记了没")],
        ledger_entries=[LedgerEntry(claim="这次一定设止损", committed_to="写进日志")],
    )
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await send(app, "我又加仓了", at=EVENING)
    await drain(app, clock)
    assert (await memory.ledger("trading"))[0][1].claim == "这次一定设止损"


async def test_a_follow_up_is_scheduled(tmp_path: Path, persona: Persona) -> None:
    """她说"我看完告诉你"就真的要记得回来说。"""
    from newperson.models import FollowUp

    plan = ReplyPlan(
        parts=[ReplyPart(text="我看看")],
        follow_up=FollowUp(delay_minutes=90, note="看完他那段回测代码"),
    )
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await send(app, "帮我看下这个回测", at=EVENING)
    await drain(app, clock)
    assert await memory.jobs_of_kind("follow_up", CONVERSATION_ID)


async def test_her_inner_note_goes_to_the_diary(tmp_path: Path, persona: Persona) -> None:
    plan = ReplyPlan(parts=[ReplyPart(text="嗯")], inner_note="他今天听起来很累")
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await send(app, "今天上班好累", at=EVENING)
    await drain(app, clock)
    day = app.rhythm.local_date(EVENING)
    assert any("很累" in note for _, note in await memory.diary_notes(day))


async def test_trading_talk_brings_the_ledger_into_context(
    tmp_path: Path, persona: Persona
) -> None:
    """聊到交易时，他之前说过的话要进提示词，她才能对质。"""
    from newperson.models import LedgerEntry

    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="上次你也这么说")])]
    )
    await memory.add_ledger_entries(
        [LedgerEntry(claim="以后不追高了")], EVENING - timedelta(days=3)
    )
    await send(app, "我今天又追高了", at=EVENING)
    await drain(app, clock)
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "以后不追高了" in prompt


async def test_style_is_enforced_end_to_end(tmp_path: Path, persona: Persona) -> None:
    """句号在真正发出去之前被去掉。"""
    app, channel, _llm, clock, _mem = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="知道了。")])]
    )
    await send(app, "我先睡了", at=EVENING)
    await drain(app, clock)
    assert channel.texts == ["知道了"]


async def test_owner_commands_never_reach_her(tmp_path: Path, persona: Persona) -> None:
    """`!np` 是给程序看的，不能进她的记忆，也不能触发回复。"""
    from newperson import owner as owner_cmds

    app, _channel, llm, _clock, memory = await build(tmp_path, persona, [])
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    text = await owner_cmds.handle("!np status", ctx)
    assert "状态" in text or "有空" in text or "睡" in text
    assert await memory.recent_messages(CONVERSATION_ID, 10) == []
    assert llm.calls == []


async def test_pause_stops_her_from_replying(tmp_path: Path, persona: Persona) -> None:
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="在")])]
    )
    await memory.kv_set("paused", "1")
    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=2)
    assert channel.sent == []
    assert await memory.unread_messages(CONVERSATION_ID), "暂停时消息要留着，恢复后她能看到"


async def test_owner_can_force_a_reply_now(tmp_path: Path, persona: Persona) -> None:
    from newperson import owner as owner_cmds

    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="在")])]
    )
    await send(app, "在吗", at=EVENING)
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    assert "催了" in await owner_cmds.handle("!np now", ctx)
    await app.scheduler.run_due_once()
    assert channel.texts == ["在"]


async def test_a_proactive_message_can_be_sent(tmp_path: Path, persona: Persona) -> None:
    plan = ProactivePlan(send=True, parts=[ReplyPart(text="今天下雪了")])
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await app.scheduler.schedule(
        "proactive",
        EVENING,
        conversation_id=CONVERSATION_ID,
        payload={"kind": "own_life", "note": "说一句自己的事"},
    )
    await drain(app, clock, hops=2)
    assert channel.texts == ["今天下雪了"]


async def test_she_can_decide_to_stay_quiet(tmp_path: Path, persona: Persona) -> None:
    app, channel, _llm, clock, _mem = await build(tmp_path, persona, [ProactivePlan(send=False)])
    await app.scheduler.schedule(
        "proactive", EVENING, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    await drain(app, clock, hops=2)
    assert channel.sent == []


async def test_unread_messages_cancel_a_proactive(tmp_path: Path, persona: Persona) -> None:
    """有他的消息没回，就不该另起话头。"""
    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=7,
            author_id=42,
            content="在吗",
            created_at=EVENING - timedelta(minutes=30),
        )
    )
    await app.scheduler.schedule(
        "proactive", EVENING, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    await app.scheduler.run_due_once()
    assert channel.sent == []
    assert llm.calls == []


async def test_a_proactive_while_asleep_is_dropped(tmp_path: Path, persona: Persona) -> None:
    """她睡着的时候不会突然冒出来说话。"""
    app0, _c, _l, _clk, _m = await build(tmp_path, persona, [])
    night = next(
        t
        for t in (datetime(2026, 10, 13, 4, 0, tzinfo=TZ) + timedelta(days=d) for d in range(14))
        if app0.rhythm.is_sleeping(t)
    )
    app, channel, llm, clock, _mem = await build(
        tmp_path / "b", persona, [ProactivePlan(send=True, parts=[ReplyPart(text="睡不着")])], now=night
    )
    await app.scheduler.schedule(
        "proactive", night, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    await app.scheduler.run_due_once()
    assert channel.sent == []
    assert llm.calls == []


async def test_proactive_decays_after_being_ignored(tmp_path: Path, persona: Persona) -> None:
    """她主动说了他没回，计数要涨，下次概率就低了。"""
    plan = ProactivePlan(send=True, parts=[ReplyPart(text="图书馆没位置")])
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    await app.scheduler.schedule(
        "proactive", EVENING, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    await drain(app, clock, hops=2)
    assert (await memory.get_conversation(CONVERSATION_ID)).unanswered_initiations == 1


async def test_his_reply_resets_the_decay(tmp_path: Path, persona: Persona) -> None:
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await memory.update_conversation(CONVERSATION_ID, unanswered_initiations=2)
    await send(app, "刚看到", at=EVENING)
    assert (await memory.get_conversation(CONVERSATION_ID)).unanswered_initiations == 0


async def test_the_day_plan_reaches_the_reply_context(tmp_path: Path, persona: Persona) -> None:
    """她说的和日程上写的要对得上，不能一边说在图书馆一边说在家。"""
    plan = DayPlan(
        date="2026-10-12",
        mood="有点烦",
        events=[
            PlanEvent(
                start="19:00",
                end="22:00",
                title="在图书馆赶 project",
                detail="Snell 三楼，位置很难抢",
                shareable=True,
            )
        ],
    )
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="在图书馆")])]
    )
    await memory.save_day_plan(app.rhythm.local_date(EVENING), plan)
    await send(app, "在干嘛", at=EVENING)
    await drain(app, clock)
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "图书馆" in prompt


async def test_the_prompt_carries_both_clocks(tmp_path: Path, persona: Persona) -> None:
    """她知道有时差，但不迁就他的作息。"""
    app, _channel, llm, clock, _mem = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING)
    await drain(app, clock)
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "他那边是" in prompt
    assert "不迁就" in prompt


async def test_the_system_prompt_is_cached_across_calls(
    tmp_path: Path, persona: Persona
) -> None:
    """两次调用的 system 必须字节相同，否则缓存永远不命中。"""
    app, _channel, llm, clock, _mem = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="嗯")]), ReplyPlan(parts=[ReplyPart(text="哦")])],
    )
    await send(app, "在吗", at=EVENING, msg_id=1)
    await drain(app, clock)
    await send(app, "睡了没", at=EVENING + timedelta(hours=2), msg_id=2)
    clock.set(EVENING + timedelta(hours=2))
    await drain(app, clock)
    assert len(llm.calls) == 2
    assert llm.calls[0]["system"] == llm.calls[1]["system"]
    assert llm.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_usage_is_tracked(tmp_path: Path, persona: Persona) -> None:
    app, _channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING)
    await drain(app, clock)
    used = await memory.usage_for(app.rhythm.local_date(EVENING))
    assert used["calls"] == 1
    assert used["cache_read_tokens"] == 80


async def test_a_model_failure_does_not_swallow_the_message(
    tmp_path: Path, persona: Persona
) -> None:
    """模型第一次没给出结果，重试时这批消息还要在。

    早先的写法在调模型之前就把消息标成已读，重试时未读是空的，
    整批消息就永远回不出去了。这个测试守着这件事。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [None, ReplyPlan(parts=[ReplyPart(text="刚看到")])]
    )
    await send(app, "帮我看下这个", at=EVENING)

    await drain(app, clock, hops=2)
    assert channel.sent == []
    assert await memory.unread_messages(CONVERSATION_ID), "失败之后消息不能被标成已读"

    await drain(app, clock, hops=4)
    assert channel.texts == ["刚看到"]


async def test_delivery_resumes_without_calling_the_model_again(
    tmp_path: Path, persona: Persona
) -> None:
    """发到一半崩了，重启接着发，不重新想一遍。"""
    plan = ReplyPlan(parts=[ReplyPart(text="第一条"), ReplyPart(text="第二条")])
    app, channel, llm, clock, memory = await build(tmp_path, persona, [plan])
    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=1)

    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    job_id = jobs[0].id
    # 假装第一条发出去之后进程挂了
    await memory.save_job_progress(
        job_id, {"plan": plan.model_dump(mode="json"), "sent_parts": 1}, 1
    )
    clock.set(jobs[0].run_at)
    await app.scheduler.run_due_once()

    assert channel.texts == ["第二条"], "第一条不该重发"
    assert llm.calls == [], "续发不该再调模型"


async def test_corrupt_progress_falls_back_to_thinking_again(
    tmp_path: Path, persona: Persona
) -> None:
    """存下来的回复读不出来时，重新生成，而不是卡死。"""
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    await memory.save_job_progress(jobs[0].id, {"plan": {"parts": "这不是列表"}}, 1)
    clock.set(jobs[0].run_at)
    await app.scheduler.run_due_once()
    assert channel.texts == ["嗯"]
