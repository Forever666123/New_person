"""端到端：一条消息从进来到发出去，走完整条链路。

不连 Discord，不连模型。假 channel 记录发了什么，假 client 按脚本返回。
这个测试的价值在于抓组装错误：单个模块都对，串起来不对。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from newperson import owner
from newperson.attention import AttentionPolicy
from newperson.brain import Brain
from newperson.calendar import AcademicCalendar
from newperson.clock import FakeClock
from newperson.config import Settings
from newperson.delivery import Deliverer
from newperson.discord_bot import CONVERSATION_ID, App, NewPersonClient
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
    brain = Brain(SimpleNamespace(messages=llm), settings, persona, memory, clock)
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
    app._default_channel = channel
    app.client = SimpleNamespace(user=SimpleNamespace(id=999))
    scheduler.register("reply", app.handle_reply_job)
    scheduler.register("proactive", app.handle_proactive_job)
    scheduler.register("follow_up", app.handle_proactive_job)
    scheduler.register("sign_off", app.handle_sign_off_job)
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
    assert (await memory.ledger("trading"))[0][2].claim == "这次一定设止损"


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
        tmp_path, persona, [None, ReplyPlan(parts=[ReplyPart(text="那个参数你调了没")])]
    )
    await send(app, "帮我看下这个", at=EVENING)

    await drain(app, clock, hops=2)
    assert channel.sent == []
    assert await memory.unread_messages(CONVERSATION_ID), "失败之后消息不能被标成已读"

    await drain(app, clock, hops=4)
    assert channel.texts == ["那个参数你调了没"]


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


async def test_the_state_line_matches_the_day_plan(tmp_path: Path, persona: Persona) -> None:
    """作息只知道有没有课，日程才知道她此刻在干什么。

    不接上的话，"你现在有空"和"19:00-22:00 在图书馆"会同时摆在她面前，
    她说出来的话就会自相矛盾。
    """
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="图书馆")])]
    )
    day = app.rhythm.local_date(EVENING)
    await memory.save_day_plan(
        day,
        DayPlan(
            date=str(day),
            mood="有点烦",
            events=[
                PlanEvent(
                    start="19:00",
                    end="22:00",
                    title="在图书馆赶 project",
                    detail="Snell 三楼",
                    shareable=True,
                )
            ],
        ),
    )
    await send(app, "在干嘛", at=EVENING)
    await drain(app, clock)
    situation = llm.calls[0]["messages"][0]["content"].split("## ")[1]
    assert "在图书馆赶 project" in situation
    assert "你现在有空" not in situation


async def test_an_emoji_survives_all_the_way_out(tmp_path: Path, persona: Persona) -> None:
    """年轻人不可能一个 emoji 都不用。偶尔一个要能真的发出去。"""
    app, channel, _llm, clock, _mem = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="笑死 😂")])]
    )
    await send(app, "我今天把咖啡打翻在键盘上了", at=EVENING)
    await drain(app, clock)
    assert channel.texts == ["笑死 😂"]


async def test_she_can_just_react_without_speaking(tmp_path: Path, persona: Persona) -> None:
    """只点个表情不说话，那也是一种回应。"""
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[], reaction="👀")]
    )
    await send(app, "你看这个", at=EVENING)
    await drain(app, clock)
    assert channel.sent == []
    assert await memory.unread_messages(CONVERSATION_ID) == [], "点了反应也算处理过了"


async def test_a_whole_english_paragraph_gets_rewritten(
    tmp_path: Path, persona: Persona
) -> None:
    """她夹英文词，但不会整段说英文。"""
    bad = ReplyPlan(parts=[ReplyPart(text="i think you should really stop trading this week")])
    good = ReplyPlan(parts=[ReplyPart(text="这周先别做了")])
    app, channel, llm, clock, _mem = await build(tmp_path, persona, [bad, good])
    await send(app, "我又亏了", at=EVENING)
    await drain(app, clock)
    assert channel.texts == ["这周先别做了"]
    assert len(llm.calls) == 2


async def test_photo_placeholder_without_a_photo_is_cleaned_up(
    tmp_path: Path, persona: Persona
) -> None:
    """照片库是空的时候，占位符不能原样发出去。"""
    app, channel, _llm, clock, _mem = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="外面这样 {photo}"), ReplyPart(text="冷死了")])],
    )
    await send(app, "波士顿下雪了吗", at=EVENING)
    await drain(app, clock)
    assert all("{photo}" not in t for t in channel.texts)
    assert "冷死了" in channel.texts


async def test_a_paused_reply_still_goes_out_after_resume(
    tmp_path: Path, persona: Persona
) -> None:
    """暂停期间到期的回复，恢复之后要能发出去。

    早先的写法把它推后十分钟，但 handler 正常返回后被盖成 done，
    resume 之后干等再也不会发。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="在")])]
    )
    await memory.kv_set("paused", "1")
    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=2)
    assert channel.sent == []

    await memory.kv_delete("paused")
    await drain(app, clock, hops=4)
    assert channel.texts == ["在"], "恢复之后这条回复还是没发出去"


async def test_a_crash_mid_delivery_still_records_what_was_sent(
    tmp_path: Path, persona: Persona
) -> None:
    """网络断在中间时，前几条其实已经到对方手机上了。

    不记下来的话她自己的历史里就少一截，重试续发会重复或者前后矛盾。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="第一条"), ReplyPart(text="第二条")])],
    )

    sent = 0
    original = channel.send

    async def flaky(content=None, *, file=None, reference=None):
        nonlocal sent
        sent += 1
        if sent == 2:
            raise RuntimeError("连接断了")
        return await original(content, file=file, reference=reference)

    channel.send = flaky
    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=3)

    history = [m.content for m in await memory.recent_messages(CONVERSATION_ID, 10)]
    assert "第一条" in history, "已经发出去的话没进她自己的历史"


async def test_corrupt_progress_puts_the_messages_back(
    tmp_path: Path, persona: Persona
) -> None:
    """存下来的回复读不出来时，那批消息已经是已读了。

    不放回未读的话，重新生成时看到的是空的，整批消息就再也不会有人回。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="第一版")]), ReplyPlan(parts=[ReplyPart(text="第二版")])],
    )
    await send(app, "在吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    clock.set(jobs[0].run_at)
    await app.scheduler.run_due_once()
    assert channel.texts == ["第一版"]

    # 模拟升级之后队列里的 progress 读不出来了
    job_id = (await memory.jobs_of_kind("reply", CONVERSATION_ID))[0].id
    await memory.save_job_progress(job_id, {"plan": {"parts": "坏的"}}, 1)
    await memory.set_job_status(job_id, "pending")
    await app.scheduler.run_due_once()
    assert "第二版" in channel.texts, "消息没被放回未读，重新生成时什么都看不到"


async def test_the_daily_cap_defers_instead_of_dropping(
    tmp_path: Path, persona: Persona
) -> None:
    """额度用完不是故障，别拿三次重试把它烧掉然后把消息丢了。"""
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [None])
    app.settings.max_calls_per_day = 1
    await memory.record_usage(app.rhythm.local_date(EVENING), input_tokens=10)

    await send(app, "在吗", at=EVENING)
    await drain(app, clock, hops=2)

    assert channel.sent == []
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert jobs, "任务被丢掉了，这批消息再也不会有人回"
    assert jobs[0].run_at > EVENING + timedelta(hours=1), "应该顺延到明天，不是几分钟后重试"
    assert await memory.unread_messages(CONVERSATION_ID), "消息要留着"


async def test_reconnecting_does_not_start_everything_twice(
    tmp_path: Path, persona: Persona
) -> None:
    """on_ready 会被反复触发。每次都重来一遍会泄漏连接、叠加 presence 循环。"""
    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    app._started = True
    before = len(app._tasks)
    await app.start(app.client)
    assert len(app._tasks) == before


async def test_reconnecting_does_not_stack_presence_loops(
    tmp_path: Path, persona: Persona
) -> None:
    """在线状态那条循环也只能有一条。

    ``App.start`` 早就挡住了重复启动，但 presence 循环是在
    ``NewPersonClient.on_ready`` 里起的，在那个守卫**外面**——
    每次重连都会再叠一条。后果不只是多跑几个协程：它们会一起写在线状态，
    撞上 Discord 的频率限制，而被限流又会导致断线重连，正反馈。
    """
    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    app._started = True  # 让 start 走早返回，这里只关心 presence
    client = NewPersonClient(app)

    await client.on_ready()
    await client.on_ready()

    presence_loops = [t for t in app._tasks if t.get_name() == "presence"]
    assert len(presence_loops) == 1

    for task in presence_loops:
        task.cancel()


async def test_heat_looks_at_the_conversation_before_this_message(
    tmp_path: Path, persona: Persona
) -> None:
    """热度问的是"这批消息到来之前"对话有多热。

    早先用会话表上的 last_user_message_at，但那个字段在消息入库时
    已经被刚收到的这条更新过了，间隔永远是 0 秒，于是**每一条消息
    都被判成正在热聊，她永远秒回**。整个项目最核心的目标就这么没了。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await send(app, "在吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert "cold" in jobs[0].reason, f"三天没说话还判成热聊：{jobs[0].reason}"


async def test_replying_right_after_her_counts_as_hot(
    tmp_path: Path, persona: Persona
) -> None:
    """她刚说完他马上接话，那时候手机确实还在手上。"""
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await memory.add_bot_message(CONVERSATION_ID, "刚说的", EVENING - timedelta(seconds=40))
    await send(app, "对了还有件事", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert "hot" in jobs[0].reason


async def test_an_hour_later_is_not_hot_anymore(tmp_path: Path, persona: Persona) -> None:
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await memory.add_bot_message(CONVERSATION_ID, "刚说的", EVENING - timedelta(hours=2))
    await send(app, "在吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert "cold" in jobs[0].reason


async def test_a_backlog_becomes_one_reply(tmp_path: Path, persona: Persona) -> None:
    """他半夜连发四条，她醒来只回一次，不是四次。"""
    from zoneinfo import ZoneInfo as _TZ

    night = datetime(2026, 9, 10, 3, 13, tzinfo=_TZ("America/New_York"))
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="那个参数你调了没")])] * 5, now=night
    )
    assert app.rhythm.is_sleeping(night)

    for i, minutes in enumerate([0, 109, 110, 190]):
        t = night + timedelta(minutes=minutes)
        clock.set(t)
        await send(app, f"第{i + 1}条", at=t, msg_id=200 + i)
        assert len(await memory.pending_jobs("reply", CONVERSATION_ID)) == 1

    await drain(app, clock)
    assert len(llm.calls) == 1, "积压的消息不该分成好几次回"
    assert channel.texts == ["那个参数你调了没"]

    prompt = llm.calls[0]["messages"][0]["content"]
    for i in range(1, 5):
        assert f"第{i}条" in prompt, "四条都要进上下文，她是一起看到的"


async def test_a_backlog_tells_her_not_to_answer_line_by_line(
    tmp_path: Path, persona: Persona
) -> None:
    """逐条应答是客服，不是朋友。"""
    from zoneinfo import ZoneInfo as _TZ

    night = datetime(2026, 9, 10, 3, 13, tzinfo=_TZ("America/New_York"))
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])] * 3, now=night
    )
    for i, minutes in enumerate([0, 109, 190]):
        t = night + timedelta(minutes=minutes)
        clock.set(t)
        await send(app, f"第{i + 1}条", at=t, msg_id=300 + i)
    await drain(app, clock)
    assert "别逐条回应" in llm.calls[0]["messages"][0]["content"]


async def test_a_quick_burst_is_treated_as_one_thought(
    tmp_path: Path, persona: Persona
) -> None:
    """他一分钟内连发三句，那是一段话，不是三件事。"""
    app, _channel, llm, clock, _memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])] * 3
    )
    for i in range(3):
        t = EVENING + timedelta(seconds=i * 20)
        clock.set(t)
        await send(app, f"第{i + 1}句", at=t, msg_id=400 + i)
    await drain(app, clock)
    assert "当成一段话看" in llm.calls[0]["messages"][0]["content"]


async def test_a_trading_question_gets_an_answer_end_to_end(
    tmp_path: Path, persona: Persona
) -> None:
    """他说他买了什么、亏了多少、有点慌，这种她不会不回。"""
    app, channel, llm, clock, _memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[]), ReplyPlan(parts=[ReplyPart(text="成本 124.5 你止损设哪")])],
    )
    await send(
        app,
        "我前几天买了1万块钱soxl 成本124.5 现在120了 有点焦虑",
        at=EVENING,
    )
    await drain(app, clock)
    assert channel.texts == ["成本 124.5 你止损设哪"]
    assert len(llm.calls) == 2, "第一次给了空，应该逼它重来一次"


async def test_a_plain_question_also_gets_an_answer(
    tmp_path: Path, persona: Persona
) -> None:
    app, channel, llm, _clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[]), ReplyPlan(parts=[ReplyPart(text="还没")])]
    )
    await send(app, "你吃饭了吗", at=EVENING)
    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert jobs[0].payload["is_question"] is True


async def test_good_night_can_go_unanswered(tmp_path: Path, persona: Persona) -> None:
    """不是所有消息都得回。晚安这种接不接都行。"""
    app, channel, llm, clock, _memory = await build(tmp_path, persona, [ReplyPlan(parts=[])])
    await send(app, "睡了 晚安", at=EVENING)
    await drain(app, clock)
    assert channel.sent == []
    assert len(llm.calls) == 1, "闲聊不该被逼着重来"


async def test_the_ledger_is_readable_by_the_owner(tmp_path: Path, persona: Persona) -> None:
    """她记下的交易陈述，你自己也要看得到。那既是她对质的依据，
    也是你的交易日志。"""
    from newperson import owner as owner_cmds
    from newperson.models import LedgerEntry

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await memory.add_ledger_entries(
        [LedgerEntry(claim="买了 1 万块 soxl，成本 124.5", committed_to="设个止损")], EVENING
    )
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    text = await owner_cmds.handle("!np ledger", ctx)
    assert "soxl" in text
    assert "设个止损" in text


async def test_an_empty_ledger_says_how_to_fill_it(tmp_path: Path, persona: Persona) -> None:
    from newperson import owner as owner_cmds

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    assert "还没记下" in await owner_cmds.handle("!np ledger", ctx)


async def test_a_half_started_process_does_not_pretend_to_be_running(
    tmp_path: Path, persona: Persona
) -> None:
    """启动中途炸了，就不能留下一个"已启动"的标记。

    留下的话会变成最难查的一种故障：discord.py 吞掉 on_ready 的异常继续跑，
    网关连着、头像亮着、消息也收得到并入库，但调度循环从来没起来——
    她永远不回你。而重连时 _started 已经是真，直接早返回，永远修不好。
    唯一的症状就是她不说话，而那正是她的正常状态。
    """
    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    app._started = False
    app._tasks.clear()

    async def boom() -> None:
        raise OSError("数据库打不开")

    app.memory.open = boom
    for _ in range(3):
        with contextlib.suppress(OSError):
            await app.start(app.client)
        assert app._started is False, "半途失败之后不能标成已启动"


async def test_being_able_to_send_again_clears_the_undeliverable_flag(
    tmp_path: Path, persona: Persona
) -> None:
    """发得出去就把"发不出去"收回来。

    一次 403（你临时退了共同服务器、或者关了服务器成员私信）之后，
    deliverable 会被置成 False 而**永远回不来**：回复照常发（那条路不看这个标记），
    主动消息全部静默跳过。于是她从此只回话、再也不主动，而你根本不会发现。
    """
    app, _channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await memory.update_conversation(CONVERSATION_ID, deliverable=False)

    await send(app, "在忙吗", at=EVENING)
    await drain(app, clock)

    conv = await memory.get_conversation(CONVERSATION_ID)
    assert conv.deliverable is True, "已经发出去了，这个标记该收回来"


class FakeHistoryChannel(FakeChannel):
    """能翻历史的假频道，用来测停机补抓。"""

    def __init__(self, messages: list | None = None, channel_id: int = 999) -> None:
        super().__init__()
        self.messages = list(messages or [])
        self.id = channel_id
        self.history_calls: list[int] = []

    def history(self, *, limit: int, after, oldest_first: bool = True):
        start_id = getattr(after, "id", 0)
        self.history_calls.append(start_id)
        picked = sorted([m for m in self.messages if m.id > start_id], key=lambda m: m.id)

        async def gen():
            for m in picked[:limit]:
                yield m

        return gen()


def fake_incoming(msg_id: int, text: str, at: datetime, channel_id: int = 999):
    """一条历史消息。"""
    return SimpleNamespace(
        id=msg_id,
        content=text,
        created_at=at,
        author=SimpleNamespace(id=42, display_name="Leo", bot=False),
        channel=SimpleNamespace(id=channel_id),
        attachments=[],
    )


def wire_inbound(app: App, dm: FakeHistoryChannel, public: FakeHistoryChannel | None = None):
    """把假频道接到**真的查找路径**上。

    早先这几条测试是直接 `app._default_channel = channel` 赋值的，绕过了
    频道查找——于是"补抓去错频道"那个 bug 一条测试都没守住。
    现在走 client.get_user().dm_channel，跟线上同一条路。
    """
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda cid: public,
    )
    if public is not None:
        app.settings.proactive_channel_id = public.id
        app._default_channel = public
    # _should_handle 里那句 isinstance(channel, discord.DMChannel) 用假对象糊不过去，
    # 而这几条测试盯的是**去哪个频道找**，不是消息过滤。过滤另有测试守着。
    app._should_handle = lambda _m: True
    return app


async def test_messages_sent_while_she_was_down_are_recovered(
    tmp_path: Path, persona: Persona
) -> None:
    """进程停着的时候他发的话不能永远消失。

    Discord **不补发新会话之前的事件**，而每次重启都是一个新会话。
    所以部署、宿主机维护、崩溃拉起的那几十秒里他说的话，
    原来是不入库、不进未读、她永远不会回，而且他不会收到任何提示。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前说的", at=EVENING, msg_id=100)

    missed = EVENING + timedelta(minutes=30)
    dm = FakeHistoryChannel(
        [
            fake_incoming(101, "停机期间说的第一句", missed),
            fake_incoming(102, "停机期间说的第二句", missed + timedelta(minutes=5)),
        ]
    )
    wire_inbound(app, dm)

    assert await app.catch_up() == 2
    assert dm.history_calls[0] == 100, "要从库里最新那条之后开始补，不是从头拉"
    assert len(await memory.unread_messages(CONVERSATION_ID)) == 3
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "补完要排一条回复"


async def test_it_looks_in_the_dm_even_when_a_public_channel_is_configured(
    tmp_path: Path, persona: Persona
) -> None:
    """**配了 PROACTIVE_CHANNEL_ID 也要翻私聊。**

    补抓一开始用的是"主动消息发去哪"那个频道。配上公开频道之后，
    他在私聊里说的话一条都补不回来——而且游标会被公开频道的消息推过去，
    于是那些私聊消息**永久**跳过。整个功能在一个正常的可选配置下静默失效。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)

    dm = FakeHistoryChannel([fake_incoming(101, "私聊里漏掉的", EVENING + timedelta(minutes=5))])
    public = FakeHistoryChannel([], channel_id=777)
    wire_inbound(app, dm, public)

    assert await app.catch_up() == 1, "私聊那条必须补回来"
    assert dm.history_calls, "私聊压根没被翻过"
    unread = await memory.unread_messages(CONVERSATION_ID)
    assert [m.discord_message_id for m in unread if m.discord_message_id == 101]


async def test_the_reply_goes_back_to_where_he_spoke(tmp_path: Path, persona: Persona) -> None:
    """补抓之后排的回复要回到他说话的地方，不是默认频道。

    不然他在私聊说的话会被回到公开频道里。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    # 先把之前那条任务了结，这样补抓会新排一条——要验的是新排的那条去哪
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    dm = FakeHistoryChannel([fake_incoming(101, "私聊里漏掉的", EVENING + timedelta(minutes=5))])
    wire_inbound(app, dm, FakeHistoryChannel([], channel_id=777))

    await app.catch_up()
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.payload.get("channel_id") == dm.id


async def test_merging_into_a_pending_reply_updates_where_it_goes(
    tmp_path: Path, persona: Persona
) -> None:
    """并进已排的那条回复时，目的地要跟着最新这条消息走。

    不更新的话，先前那条任务记的还是旧频道（或者根本没记），
    于是他在私聊说的话会被回到公开频道去。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "第一句", at=EVENING, msg_id=100)
    assert (await memory.pending_jobs("reply", CONVERSATION_ID))[0].payload["channel_id"] is None

    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=101,
            author_id=42,
            author_name="Leo",
            content="第二句",
            created_at=EVENING + timedelta(seconds=30),
        )
    )
    await app._schedule_reply(EVENING + timedelta(seconds=30), channel_id=555)

    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.payload["channel_id"] == 555


async def test_more_than_one_page_of_missed_messages_is_recovered(
    tmp_path: Path, persona: Persona
) -> None:
    """漏掉的超过一页也要全补回来。

    只翻一页的话，超出的部分不但这次补不到——游标推进之后就**再也**补不到了。
    而分页是从旧往新走的，被丢掉的恰恰是他**最后说的**那几条。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)

    dm = FakeHistoryChannel(
        [
            fake_incoming(101 + i, f"第 {i} 句", EVENING + timedelta(minutes=i))
            for i in range(60)
        ]
    )
    wire_inbound(app, dm)

    assert await app.catch_up(page=25) == 60, "分页没翻到底"
    unread = await memory.unread_messages(CONVERSATION_ID)
    assert max(m.discord_message_id for m in unread) == 160, "他最后说的那几条丢了"


async def test_recovered_messages_keep_their_real_time(
    tmp_path: Path, persona: Persona
) -> None:
    """补回来的消息要用**消息本身的时间**，不是现在的时间。

    存成"刚刚"的话，她会按"刚收到"去算回复时机——
    于是三小时前说的话被当成刚说的，她秒回过去。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)

    real_time = EVENING + timedelta(minutes=20)
    clock.set(EVENING + timedelta(hours=4))  # 四小时之后才重启
    wire_inbound(app, FakeHistoryChannel([fake_incoming(101, "停机期间", real_time)]))

    await app.catch_up()

    unread = await memory.unread_messages(CONVERSATION_ID)
    recovered = [m for m in unread if m.discord_message_id == 101][0]
    drift = abs((recovered.created_at - real_time).total_seconds())
    assert drift < 60, f"时间偏了 {drift / 60:.0f} 分钟，她会按错误的时机回"


async def test_a_stale_message_does_not_get_hot_chat_speed(
    tmp_path: Path, persona: Persona
) -> None:
    """停了几小时之后补回来的话，不该用"手机就在手上"的速度回。

    heat 问的是"这批消息到来时对话有多热"，那是对的；但她几小时后才看到，
    再用热聊的速度回就正好跟进程重启对上——那是很显眼的机器痕迹。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    await send(app, "聊着呢", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    stale = EVENING + timedelta(minutes=1)
    clock.set(EVENING + timedelta(hours=5))
    wire_inbound(app, FakeHistoryChannel([fake_incoming(101, "停机期间说的", stale)]))

    await app.catch_up()

    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert "hot" not in job.reason, f"隔了五小时还判成热聊：{job.reason}"


async def test_a_fresh_install_does_not_drag_in_the_whole_history(
    tmp_path: Path, persona: Persona
) -> None:
    """库是空的时候什么都不补。

    不然第一次上线会把整段历史拉进来——而那恰恰是"她不该知道的事"。
    """
    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    dm = FakeHistoryChannel([fake_incoming(1, "很久以前", EVENING)])
    wire_inbound(app, dm)

    assert await app.catch_up() == 0
    assert dm.history_calls == [], "空库连 history 都不该调"


async def test_catching_up_twice_does_not_duplicate(tmp_path: Path, persona: Persona) -> None:
    """每次重连都会跑一遍补抓，重复跑不能把消息记两遍。"""
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "之前", at=EVENING, msg_id=100)
    wire_inbound(
        app, FakeHistoryChannel([fake_incoming(101, "漏掉的", EVENING + timedelta(minutes=10))])
    )

    assert await app.catch_up() == 1
    assert await app.catch_up() == 0
    unread = await memory.unread_messages(CONVERSATION_ID)
    assert len([m for m in unread if m.discord_message_id == 101]) == 1


async def test_owner_commands_are_not_replayed_on_catch_up(
    tmp_path: Path, persona: Persona
) -> None:
    """`!np` 是给程序看的，补抓时更不该被当成消息记下来。

    真执行一遍就更糟：重启时把停机期间的 `!np pause` 又跑一次。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "之前", at=EVENING, msg_id=100)
    wire_inbound(
        app,
        FakeHistoryChannel(
            [
                fake_incoming(101, "!np status", EVENING + timedelta(minutes=5)),
                fake_incoming(102, "真的消息", EVENING + timedelta(minutes=6)),
            ]
        ),
    )

    assert await app.catch_up() == 1
    unread = await memory.unread_messages(CONVERSATION_ID)
    assert not [m for m in unread if "!np" in m.content]


async def test_a_broken_history_call_does_not_stop_her_starting(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓失败（限流、权限变了）不能让她起不来。

    补抓是锦上添花，网关连上才是命根子。
    """
    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    await send(app, "之前", at=EVENING, msg_id=100)

    class Exploding(FakeHistoryChannel):
        def history(self, **_kw):
            raise RuntimeError("429")

    wire_inbound(app, Exploding())
    assert await app.catch_up() == 0


async def test_the_reply_goes_to_where_he_spoke_last_not_where_we_looked_last(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓完排的那条回复，要回到他**最后说话**的地方。

    两个频道是挨个翻的，而"最后遍历到的那条"和"他最后说的那条"不是一回事：
    他先在公开频道说了一句、半小时后回私聊里说了最后一句，
    翻的顺序却是私聊在前、公开频道在后。按遍历顺序取的话，
    她会把私聊里那句话回到公开频道去——那是当着别人的面。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    public = FakeHistoryChannel(
        [fake_incoming(101, "公开频道里的", EVENING + timedelta(minutes=5), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(102, "私聊里最后说的", EVENING + timedelta(minutes=30), channel_id=999)],
        channel_id=999,
    )
    wire_inbound(app, dm, public)

    assert await app.catch_up() == 2
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.payload.get("channel_id") == dm.id, (
        f"回到了 {job.payload.get('channel_id')}，但他最后说话在私聊 {dm.id}"
    )


async def test_one_channel_failing_does_not_skip_the_other_forever(
    tmp_path: Path, persona: Persona
) -> None:
    """一个频道翻失败，不能把另一个频道的消息永久跳过。

    两个频道原来共用一个游标——库里最大的那个 discord_message_id。
    私聊补抓成功、公开频道正好限流的话，游标被私聊那条推了过去；
    下次重连时公开频道从那个位置往后翻，中间他说的话**再也不会被翻到**。
    这是最坏的一种 bug：消息进不了库，没有报错，没有任何症状，
    你只会觉得她那天没理你。

    所以每个频道各记各的游标，而且只在整条翻完之后才推进。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    class Flaky(FakeHistoryChannel):
        boom = True

        def history(self, **kw):
            if Flaky.boom:
                raise RuntimeError("429 限流")
            return super().history(**kw)

    public = Flaky(
        [fake_incoming(101, "公开频道里漏掉的", EVENING + timedelta(minutes=5), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(102, "私聊里漏掉的", EVENING + timedelta(minutes=10), channel_id=999)],
        channel_id=999,
    )
    wire_inbound(app, dm, public)

    assert await app.catch_up() == 1, "私聊那条这次就该补回来"
    Flaky.boom = False  # 限流过去了，下次重连再补
    assert await app.catch_up() == 1, "公开频道那条要在下一次补回来"

    ids = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert {101, 102} <= ids, f"少了：{ {101, 102} - ids}"


async def test_a_question_sent_while_a_reply_is_pending_is_seen_as_a_question(
    tmp_path: Path, persona: Persona
) -> None:
    """并进已排的回复时，特征要按**整批未读**重新算。

    原来合并那条路只更新目的地，`is_question` 和 `mode` 留着第一条消息算出来的。
    "刚到家"后面接一句"你明天有空吗？"，任务里还记着 is_question=False，
    于是那个问题按闲聊处理，该换的口吻也没换——
    而他连发的时候，往往后一条才是正事。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "刚到家", at=EVENING, msg_id=100)
    first = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert first.payload.get("is_question") is False, "前提不成立：第一条本来就被当成问句了"

    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=101,
            author_id=42,
            author_name="Leo",
            content="你明天有空吗？帮我看下那个合同行不行？",
            created_at=EVENING + timedelta(seconds=30),
        )
    )
    await app._schedule_reply(EVENING + timedelta(seconds=30), channel_id=555)

    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.id == first.id, "前提不成立：没有走合并那条路"
    assert job.payload.get("is_question") is True, "他问了问题，但任务里还记着 is_question=False"


async def test_one_bad_attachment_does_not_swallow_the_whole_catch_up(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓时某一条处理不了，剩下的还要补回来，而且**必须排上回复**。

    逐条处理那段（下附件、入库）原来在 try 外面。`_download_images` 要下载、
    要写盘，磁盘满了或者 aiohttp 抛一下就把整个补抓炸掉——

    而炸掉的后果不是"少补几条"：前面几条**已经进库了**，
    但函数是在排回复之前退出的，所以没人给它们排。下次重连再补抓时，
    那几条全是重复（add_user_message 返回 0），recovered 还是 0，
    还是没人排——那些话就永远躺在库里，她永远不会回。
    这正是"消息不能凭空消失"那条不变量要挡的事。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    calls = {"n": 0}
    original = app._download_images

    async def flaky(message):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("[Errno 28] No space left on device")
        return await original(message)

    app._download_images = flaky
    dm = FakeHistoryChannel(
        [
            fake_incoming(101 + i, f"第 {i} 句", EVENING + timedelta(minutes=i), channel_id=999)
            for i in range(3)
        ],
        channel_id=999,
    )
    wire_inbound(app, dm)

    await app.catch_up()  # 不能往外抛

    ids = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert {101, 103} <= ids, f"坏的那条不该连累别的：{sorted(ids)}"
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "补回来了却没人排回复，她永远不会回"


async def test_messages_left_unanswered_by_a_crashed_catch_up_get_a_reply_later(
    tmp_path: Path, persona: Persona
) -> None:
    """上一轮补抓中途没走完，下一轮要把没排上的回复补排。

    重复的消息 add_user_message 返回 0，所以下一轮的 recovered 是 0——
    光看 recovered 的话，永远没人给它们排回复。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    # 模拟上一轮的残局：消息进库了，但没有排着的回复
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=101,
            author_id=42,
            author_name="Leo",
            content="上一轮补进来但没排上的",
            created_at=EVENING + timedelta(minutes=5),
        )
    )
    assert not await memory.pending_jobs("reply", CONVERSATION_ID)

    wire_inbound(app, FakeHistoryChannel([], channel_id=999))
    assert await app.catch_up() == 0, "这一轮确实什么都没补到"
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "躺在库里的那条没人管了"


async def test_a_resent_message_with_no_pending_reply_gets_one(
    tmp_path: Path, persona: Persona
) -> None:
    """消息进了库但没排上回复时，网关重发是唯一的补救，不能被挡在门外。

    `add_user_message` 提交之后还要再往 jobs 表写一次；备份正在跑的时候
    SQLite 可能 `database is locked`，而 `on_message` 把异常吞成一行日志。
    于是这条消息进了库、没人回；又因为它还未读，她连主动消息都发不出来
    （未读挡着）——整个人哑掉，一直到下次重连补抓才救得回来。

    原来这里无条件 `return`（"网关重发，已经处理过了"），
    把这道天然的补救也挡掉了。重发只在**已经排着回复**时才该短路。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    app._should_handle = lambda _m: True
    clock.set(EVENING)
    incoming = fake_incoming(100, "在吗 那个作业", EVENING)

    boom = {"on": True}
    original = app._schedule_reply

    async def flaky(*args, **kwargs):
        if boom["on"]:
            boom["on"] = False
            raise RuntimeError("database is locked")
        return await original(*args, **kwargs)

    app._schedule_reply = flaky
    with contextlib.suppress(RuntimeError):
        await app.on_user_message(incoming)

    assert await memory.unread_messages(CONVERSATION_ID), "前提不成立：消息没进库"
    assert not await memory.pending_jobs("reply", CONVERSATION_ID), "前提不成立：回复排上了"

    await app.on_user_message(incoming)  # 网关重发同一条
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "重发也救不回来，她就这么哑了"


async def test_a_resent_message_does_not_push_the_reply_back_again(
    tmp_path: Path, persona: Persona
) -> None:
    """正常的网关重发还是要短路，不能每重发一次就把回复往后推一点。"""
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    app._should_handle = lambda _m: True
    clock.set(EVENING)
    incoming = fake_incoming(100, "第一句", EVENING)

    await app.on_user_message(incoming)
    before = (await memory.pending_jobs("reply", CONVERSATION_ID))[0].run_at

    for _ in range(3):
        await app.on_user_message(incoming)

    jobs = await memory.pending_jobs("reply", CONVERSATION_ID)
    assert len(jobs) == 1
    assert jobs[0].run_at == before, "重发把回复往后推了"


async def test_a_backlog_bigger_than_the_page_budget_keeps_making_progress(
    tmp_path: Path, persona: Persona
) -> None:
    """积压超过一次能翻的量时，下次重连要**接着翻**，不能从头重来。

    页数用满不是出错——那些页是好好处理完的。把它当成出错、不推进游标的话，
    这个频道在每一次重连都从同一个位置重翻同样的内容，全是重复，
    于是永远翻不过去，而且每次重连还白烧 max_pages 次 history 调用。

    卡死的门槛比想象中低：被过滤掉的消息照样吃预算（游标在过滤之前推进，
    一页数的是整页），所以公开频道里别人的闲聊、她自己发的话全都算数。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    dm = FakeHistoryChannel(
        [
            fake_incoming(101 + i, f"第 {i} 句", EVENING + timedelta(minutes=i), channel_id=999)
            for i in range(10)
        ],
        channel_id=999,
    )
    wire_inbound(app, dm)

    # 一次只够翻 4 条，10 条要翻三轮
    seen: set[int] = set()
    for _ in range(3):
        await app.catch_up(page=2, max_pages=2)
        seen = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert len(seen) >= 11, f"翻不动了，三轮只补回 {len(seen) - 1} 条：{sorted(seen)}"


async def test_a_channel_that_only_has_other_peoples_chatter_still_advances(
    tmp_path: Path, persona: Persona
) -> None:
    """整页都是该跳过的消息时，游标也必须往前走。

    不走的话，一个有人气的公开频道能把补抓永远钉在原地：
    每次重连翻回同样那几页闲聊，他真正说的那句永远排在预算之外。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    chatter = [
        fake_incoming(101 + i, f"路人 {i}", EVENING + timedelta(minutes=i), channel_id=999)
        for i in range(8)
    ]
    his = fake_incoming(200, "他真正说的那句", EVENING + timedelta(minutes=30), channel_id=999)
    dm = FakeHistoryChannel([*chatter, his], channel_id=999)
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda _cid: None,
    )
    # 只认他那条，别的全是路人
    app._should_handle = lambda m: m.id == 200

    for _ in range(5):
        await app.catch_up(page=2, max_pages=2)
    ids = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert 200 in ids, f"闲聊把补抓卡死了，他那句永远补不到：{sorted(ids)}"


async def test_catching_up_two_channels_keeps_the_unread_in_time_order(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓翻两个频道之后，未读还得是他说话的先后顺序。

    补抓是一个频道一个频道整段写库的，而未读原来按 id（插入顺序）排——
    他 20:05 在公开频道说的那句会排在 20:30 私聊那句**后面**。
    下游全指着"最后一个就是他最后说的"：

    - 提示词按这个顺序讲给她听，她看到的对话是倒着的；
    - `staleness` 拿 `unread[-1]` 算他等了多久，于是两分钟前的消息
      被算成隔了快一小时，热度被压成 cold，
      提示里还会写着"他这条消息是 63 分钟前发的"；
    - 表情反应和引用贴在 `unread[-1]` 上，而回复去的是**最新那条**的频道——
      两者对不上，表情就贴到另一个频道的消息上去，404，静默消失。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done")

    public = FakeHistoryChannel(
        [fake_incoming(101, "公开频道 20:05", EVENING + timedelta(minutes=5), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(102, "私聊 20:30", EVENING + timedelta(minutes=30), channel_id=999)],
        channel_id=999,
    )
    wire_inbound(app, dm, public)
    await app.catch_up()

    unread = await memory.unread_messages(CONVERSATION_ID)
    stamps = [m.created_at for m in unread]
    assert stamps == sorted(stamps), f"未读不是时间序：{[(m.discord_message_id, m.created_at) for m in unread]}"
    assert unread[-1].discord_message_id == 102, "最后一条不是他最后说的那条"

    # 回复的目的地和 unread[-1] 必须指同一条消息，表情才不会贴错频道
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.payload.get("channel_id") == dm.id


async def test_every_np_command_answers_without_blowing_up(
    tmp_path: Path, persona: Persona
) -> None:
    """把 `!np` 的每一条都跑一遍：不许抛异常，不许返回空串。

    这些命令是他在她不对劲的时候唯一的抓手——`!np status` 坏掉的那一刻，
    正是他最需要它的那一刻，而它坏了不会有任何别的症状。

    遍历的是 `owner.HANDLERS` 本身，所以**新加的命令自动被这条守着**。
    """
    from newperson import owner as owner_cmds

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "先说点什么", at=EVENING, msg_id=100)
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    # 给每条命令一组说得通的参数；没列到的就按不带参数跑
    args = {"away": "出差 5", "chatty": "0.5", "ledger": "study"}

    for name in owner_cmds.HANDLERS:
        text = await owner_cmds.handle(f"!np {name} {args.get(name, '')}".strip(), ctx)
        assert isinstance(text, str) and text.strip(), f"`!np {name}` 回了个空"
        assert "Traceback" not in text, f"`!np {name}` 把异常回出去了：{text}"

    # 跑完之后别把她留在暂停或者请假状态里
    await owner_cmds.handle("!np resume", ctx)
    await owner_cmds.handle("!np back", ctx)
    assert not await memory.kv_get("paused")
    assert not await memory.kv_get("away_note")


async def test_an_unknown_np_command_says_so_instead_of_doing_nothing(
    tmp_path: Path, persona: Persona
) -> None:
    """打错的命令要有回音。没回音的话他分不清是打错了还是她坏了。"""
    from newperson import owner as owner_cmds

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    text = await owner_cmds.handle("!np staus", ctx)
    assert "不认识" in text


async def test_the_fallback_reply_goes_to_the_dm_not_the_public_channel(
    tmp_path: Path, persona: Persona
) -> None:
    """补救路径也要回到他说话的地方。

    上一轮补抓在"消息已入库"和"排回复"之间炸了，这一轮全是重复、
    `recovered` 是 0，走的是补排那条分支——而它原来不传频道，
    `resolve_channel(None)` 返回的是**主动消息的去处**，
    配了 PROACTIVE_CHANNEL_ID 就是那个公开频道。
    于是他在私聊里说的话会被当着别人的面回出去。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    dm = FakeHistoryChannel([], channel_id=999)
    public = FakeHistoryChannel([], channel_id=777)
    wire_inbound(app, dm, public)

    # 上一轮的残局：私聊那条进了库、没人排回复，而"他在哪说的"已经记下了
    await send(app, "停机前", at=EVENING, msg_id=100)
    await app._remember_inbound(dm.id)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done", at=EVENING)
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=101,
            author_id=42,
            author_name="Leo",
            content="上一轮进来但没排上的",
            created_at=EVENING + timedelta(minutes=5),
        )
    )

    assert await app.catch_up() == 0
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert job.payload.get("channel_id") == dm.id, (
        f"补排的回复要去私聊 {dm.id}，实际去了 {job.payload.get('channel_id')}"
    )


async def test_a_channel_missing_for_one_round_does_not_lose_its_messages(
    tmp_path: Path, persona: Persona
) -> None:
    """某一轮**连频道都拿不到**时，那个频道的消息也不能丢。

    "第一次翻到这个频道就把基线钉住"只在它进得了补抓循环时才生效。
    拿不到（限流、权限刚变）的那一轮它连 kv 都不会被建，
    而同一轮私聊补抓成功会把库里最大的编号推上去——
    下一轮它恢复了，起点就落在被推过去的位置，中间的消息永久跳过。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done", at=EVENING)

    public = FakeHistoryChannel(
        [fake_incoming(150, "公开频道里的", EVENING + timedelta(minutes=5), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(200, "私聊里的", EVENING + timedelta(minutes=10), channel_id=999)],
        channel_id=999,
    )
    app.settings.proactive_channel_id = public.id
    app._should_handle = lambda _m: True

    reachable = {"public": False}
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda _cid: public if reachable["public"] else None,
        fetch_channel=None,
    )

    assert await app.catch_up() == 1, "私聊那条这一轮就该补回来"
    reachable["public"] = True  # 下一轮它恢复了
    assert await app.catch_up() == 1, "公开频道那条要在这一轮补回来"

    ids = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert 150 in ids, f"那一轮拿不到频道，它的消息就永久没了：{sorted(ids)}"


async def test_catching_up_out_of_id_order_does_not_fake_a_cold_conversation(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓打乱 id 序之后，"这批消息之前对话有多热"不能算错。

    `last_exchange_before` 要的是这批里**最小**的 id。未读现在按时间排，
    而补抓是一个频道一整段写库的：私聊先翻（id 大、时间晚）、
    公开频道后翻（id 小、时间早）时，`unread[0].id` 是个大的，
    于是同一批里的另一条被当成"上一次交流"捞了进来——
    那条比 unread[0] 还晚，`heat_of` 把它整个滤掉，结果一律判成 cold。
    他两分钟前还在说话，她按"冷了"处理，等十几分钟才回。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    # 先来一次真正的交流，它才是"上一次交流"
    await send(app, "在图书馆", at=EVENING, msg_id=100)
    await memory.add_bot_message(CONVERSATION_ID, "嗯", EVENING + timedelta(minutes=1))
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done", at=EVENING)
    await memory.mark_read(
        [m.id for m in await memory.unread_messages(CONVERSATION_ID)],
        EVENING + timedelta(minutes=1),
    )

    # 补抓：公开频道那条时间早、id 大（私聊先入库）
    public = FakeHistoryChannel(
        [fake_incoming(300, "公开频道 20:02", EVENING + timedelta(minutes=2), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(301, "私聊 20:04", EVENING + timedelta(minutes=4), channel_id=999)],
        channel_id=999,
    )
    wire_inbound(app, dm, public)
    await app.catch_up()

    unread = await memory.unread_messages(CONVERSATION_ID)
    assert [m.discord_message_id for m in unread] == [300, 301], "前提不成立：不是时间序"
    assert unread[0].id > unread[1].id, "前提不成立：id 序和时间序没错开"

    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert "cold" not in job.reason, (
        f"他两分钟前还在说话，却被判成冷了：{job.reason}"
    )


async def test_a_hanging_image_download_does_not_stall_the_catch_up(
    tmp_path: Path, persona: Persona
) -> None:
    """一张下不动的图不能把整个补抓卡死。

    aiohttp 默认等 300 秒，而这段代码在补抓的循环里：
    停机期间他发的几百条里有几张图卡住，补抓就停在那儿，
    她一条都不会回，而日志里什么都看不出来。
    下不下来就当没这张图——她照样回，只是看不见图。
    """
    import newperson.discord_bot as db

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done", at=EVENING)

    class Hanging:
        url = "https://cdn.example/x.png"
        filename = "x.png"
        content_type = "image/png"
        size = 1024

        async def save(self, _target):
            await asyncio.sleep(3600)  # 永远下不完

    incoming = fake_incoming(101, "看这个", EVENING + timedelta(minutes=5), channel_id=999)
    incoming.attachments = [Hanging()]
    wire_inbound(app, FakeHistoryChannel([incoming], channel_id=999))

    original = db.IMAGE_DOWNLOAD_TIMEOUT
    db.IMAGE_DOWNLOAD_TIMEOUT = 0.05
    try:
        recovered = await asyncio.wait_for(app.catch_up(), timeout=5)
    finally:
        db.IMAGE_DOWNLOAD_TIMEOUT = original

    assert recovered == 1, "那条消息本身要补回来，只是没有图"
    stored = await memory.unread_messages(CONVERSATION_ID)
    assert any(m.discord_message_id == 101 for m in stored)
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "补回来了却没人排回复"


async def test_np_status_says_it_out_loud_when_she_is_actually_stuck(
    tmp_path: Path, persona: Persona
) -> None:
    """有未读、又没有排着的回复——这是"她坏了"唯一说得清的样子，要写出来。

    `!np status` 原来把"未读 N 条"和"没有排着的任务"分两行列着，
    得他自己把两行对起来看。而她隔二十分钟才回本来就是设计好的，
    从外面看"正常"和"卡住了"一模一样，唯一分得开的就是这个组合。
    """
    from newperson import owner as owner_cmds

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    ctx = owner_cmds.OwnerContext(
        memory=memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=EVENING,
    )
    await send(app, "在吗", at=EVENING, msg_id=100)
    assert "卡住" not in await owner_cmds.handle("!np status", ctx), "排着回复的时候不该说卡住"

    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "cancelled", "模拟丢了", at=EVENING)
    assert "卡住" in await owner_cmds.handle("!np status", ctx), "真卡住了却一声不吭"

    # 暂停期间未读堆着是他自己要求的，不算卡住
    await memory.kv_set("paused", "1")
    assert "卡住" not in await owner_cmds.handle("!np status", ctx)


async def test_pausing_her_does_not_eat_the_retry_budget(
    tmp_path: Path, persona: Persona
) -> None:
    """暂停期间那条回复被反复认领，但那不是"试了一次没成"。

    `claim_job` 每认领一次 `attempts` 就 +1（那是为了让"进程跑到一半崩了"
    也算一次尝试），而暂停分支只是把任务往后推十分钟，不走 `_handle_failure`，
    所以这个数从来不清零。暂停满半小时就足以把三次重试的预算吃光：
    resume 之后模型抖第一下——这个项目里最常见、设计上"当作这会儿没看手机"
    的那种失败——那条任务直接判死，他那句话永远没人回。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    await send(app, "在吗", at=EVENING, msg_id=100)
    job_id = (await memory.pending_jobs("reply", CONVERSATION_ID))[0].id or 0

    await memory.kv_set("paused", "1")
    for i in range(12):  # 暂停两小时，每十分钟被认领一次
        clock.set(EVENING + timedelta(minutes=10 * (i + 1)))
        await app.scheduler.run_due_once()

    job = await memory.get_job(job_id)
    assert job is not None and job.status == "pending", "暂停期间这条任务不该被判死"
    assert job.attempts == 0, f"暂停吃掉了 {job.attempts} 次重试预算"


async def test_the_dm_baseline_is_pinned_from_a_realtime_message(
    tmp_path: Path, persona: Persona
) -> None:
    """私聊的频道 id 只能从实时消息里知道，而基线必须在它拿不到之前就钉好。

    公开频道的 id 在配置里就有，私聊的没有。`on_ready` 又正是最容易撞限流的
    时刻——`get_user` 缓存冷 + `fetch_user` 429 就够了。那一轮私聊连游标基线
    都不会建，而同一轮公开频道补抓成功会把全库最大编号推上去；
    下一轮私聊恢复，起点就落在被推过去的位置，中间的话永久跳过。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    app._should_handle = lambda _m: True
    app.settings.proactive_channel_id = 777

    # 他先在私聊里说过话——这是我们唯一能知道私聊 id 的地方
    await send(app, "停机前", at=EVENING, msg_id=100)
    await app._remember_inbound(999)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await memory.set_job_status(job.id or 0, "done", at=EVENING)

    public = FakeHistoryChannel(
        [fake_incoming(200, "公开频道里的", EVENING + timedelta(minutes=10), channel_id=777)],
        channel_id=777,
    )
    dm = FakeHistoryChannel(
        [fake_incoming(150, "私聊里漏掉的", EVENING + timedelta(minutes=5), channel_id=999)],
        channel_id=999,
    )
    dm_up = {"ok": False}

    def get_user(_id):
        if not dm_up["ok"]:
            raise RuntimeError("429 限流")
        return SimpleNamespace(dm_channel=dm)

    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999), get_user=get_user, get_channel=lambda _c: public
    )

    assert await app.catch_up() == 1, "公开频道那条这一轮就该补回来"
    dm_up["ok"] = True
    assert await app.catch_up() == 1, "私聊那条要在这一轮补回来"

    ids = {m.discord_message_id for m in await memory.unread_messages(CONVERSATION_ID)}
    assert 150 in ids, f"私聊那一轮拿不到，它的消息就永久没了：{sorted(ids)}"


async def test_a_message_sent_while_a_delivery_is_retrying_still_gets_answered(
    tmp_path: Path, persona: Persona
) -> None:
    """他在"投递失败、等着重试"那个窗口里说的话，不能没人回。

    那句话会被 `merge_pending` 并进这条**已经带着旧 plan** 的任务。
    任务恢复后续发的是老 plan，而发到最后一条时 `interrupted()` 根本不问
    （最后一条之后没有"剩下的"了），任务就此判 done——
    那句新话没人排回复，她会一直沉默到下次重连补抓。
    """
    first = ReplyPlan(parts=[ReplyPart(text="A1"), ReplyPart(text="A2")])
    second = ReplyPlan(parts=[ReplyPart(text="B1")])
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [first, second])

    boom = {"on": True}
    real_send = channel.send

    async def flaky(*args, **kwargs):
        # A2 一直发不出去，直到我们把 boom 关掉——模拟"网络断着，任务等重试"
        if boom["on"] and len(channel.texts) >= 1:
            raise RuntimeError("网络断了")
        return await real_send(*args, **kwargs)

    channel.send = flaky
    await send(app, "第一句", at=EVENING, msg_id=100)
    await drain(app, clock, hops=3)
    assert channel.texts == ["A1"], "前提不成立：没在中间断掉"

    # 等重试的窗口里他又说了一句
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=101,
            author_id=42,
            author_name="Leo",
            content="第二句",
            created_at=clock.now(),
        )
    )
    await app._schedule_reply(clock.now(), channel_id=None)
    boom["on"] = False  # 网络回来了
    await drain(app, clock, hops=10)

    assert "A2" in channel.texts, "前提不成立：没续发完"
    leftover = [m.content for m in await memory.unread_messages(CONVERSATION_ID)]
    assert "B1" in channel.texts, f"「第二句」没人回，还躺在未读里：{leftover}"


async def test_a_backlog_bigger_than_one_pass_is_not_silently_written_off(
    tmp_path: Path, persona: Persona
) -> None:
    """积压超过一趟能吃下的量时，最老的那批**不能**被当成整理过。

    整理原来用 `recent_messages(200, after_id=...)`——那取的是**最新的** 200 条，
    而游标却推到全库最大 id，等于宣称中间那些也整理过了。它们同时早就掉出
    "最近 40 条"的窗口，于是那一段从她的记忆里彻底消失，落后计数归零，
    以后再也没有哪一次整理会回头看它们。

    而这不会有任何症状：facts 有、摘要有、`!np status` 干净、体检印 OK。
    够得着的场景也很常规——她几天没能回话，重连时补抓一次最多灌一千条。
    """
    from newperson.models import MemoryUpdate

    seen: list[list[str]] = []

    class RecordingBrain:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def update_memory(self, request, _day):
            seen.append([m.content for m in request.messages])
            return MemoryUpdate(summary="摘要", owner_facts=[], self_facts=[])

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    app.brain = RecordingBrain(app.brain)

    for i in range(260):
        await memory.add_user_message(
            IncomingMessage(
                conversation_id=CONVERSATION_ID,
                discord_message_id=9000 + i,
                author_id=42,
                author_name="Leo",
                content=f"第{i:03d}句",
                created_at=EVENING + timedelta(minutes=i),
            )
        )

    await app._maybe_summarize()
    await drain(app, clock, hops=12)

    assert seen, "一趟都没跑起来"
    first_pass = seen[0]
    assert first_pass[0] == "第000句", f"第一趟从 {first_pass[0]} 开始，不是最老的那条"

    everything = {m for batch in seen for m in batch}
    missing = [f"第{i:03d}句" for i in range(260) if f"第{i:03d}句" not in everything]
    assert not missing, f"{len(missing)} 条从没进过模型，却被当成整理过了：{missing[:3]}…"


async def test_a_failing_memory_update_does_not_burn_the_daily_budget(
    tmp_path: Path, persona: Persona
) -> None:
    """记忆整理一直失败时，不能每回一条消息就再排一个、再烧三次模型调用。

    那道闸只看 `pending`，而重试用尽之后任务变 `failed`——闸看不见它；
    同时游标因为失败从没推进，落后条数永远压着阈值。于是每一次回复
    都新建一个任务，既没退避也没上限。实测 45 轮来回烧掉 93 次调用（该是 48），
    撞上日限额之后她在当天中途毫无征兆地不说话了。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])

    class AlwaysFails:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def update_memory(self, _request, _day):
            return None

        async def over_budget(self, _day):
            return False

    app.brain = AlwaysFails(app.brain)

    for i in range(70):
        await memory.add_user_message(
            IncomingMessage(
                conversation_id=CONVERSATION_ID,
                discord_message_id=7000 + i,
                author_id=42,
                author_name="Leo",
                content=f"第{i}句",
                created_at=EVENING + timedelta(minutes=i),
            )
        )

    # 模拟接下来的二十次回复：每次都会走到 _after_reply 末尾那道闸
    for _ in range(20):
        await app._maybe_summarize()
        await drain(app, clock, hops=6)
        clock.advance(60)

    jobs = await memory.failed_jobs(CONVERSATION_ID)
    assert len(jobs) <= 2, f"排了 {len(jobs)} 个注定失败的整理任务，每个烧三次调用"


async def test_a_reminder_that_fires_after_he_has_spoken_knows_it_may_be_stale(
    tmp_path: Path, persona: Persona
) -> None:
    """排好的"回头问他"，在他又说过话之后才响，要让她知道这件事可能已经过时了。

    走完整条路：她排了一个 follow_up → 他又发了消息、她也回了 → 那个 follow_up 到点。
    到点那一刻交给模型的请求里要带着"他之后说过话"。不带的话，
    她只看得见那条旧提醒，会照着去问一件他刚刚才说完的事。
    """
    from newperson.brain import ProactivePlan

    seen = []

    class Recording:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def generate_proactive(self, request, _day):
            seen.append(request)
            return ProactivePlan(send=False, parts=[])

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    app.brain = Recording(app.brain)
    clock.set(EVENING)

    await app.life.schedule_follow_up(CONVERSATION_ID, 90, "问问他作业 A 弄完没有")
    job = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]

    # 四十分钟后他来说了一句，她那边也记下了这一条
    clock.set(EVENING + timedelta(minutes=40))
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID,
            discord_message_id=4242,
            author_id=42,
            author_name="Leo",
            content="作业 A 弄完了",
            created_at=clock.now(),
        )
    )
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], clock.now())

    clock.set(job.run_at + timedelta(seconds=1))
    await app.handle_proactive_job(job)

    assert seen, "前提不成立：这条提醒根本没走到模型（她在睡觉？正热聊？）"
    assert seen[-1].he_spoke_since_noted, "他明明说过话了，提醒却不知道"


# -- 他说了时间的事，那之前不问 ------------------------------------------------

# 他那边 09-29 01:30（悉尼）= 她那边 09-28 11:30。她那天九点半就醒了。
HIS_1_30 = datetime(2026, 9, 28, 11, 30, tzinfo=TZ)
SYDNEY = ZoneInfo("Australia/Sydney")


async def _deliver_one_reply(app: App, clock: FakeClock, memory: Memory) -> None:
    """只把回复那一个任务跑掉，别的任务留在队列里。"""
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()


async def test_his_morning_plan_is_not_asked_about_at_his_dawn(
    tmp_path: Path, persona: Persona
) -> None:
    """复现线上那一幕，走完整条路。

    他凌晨一点半说"明天早上9点起来把回测跑完"。模型不听话：记了台账，
    又顺手排了一个三小时后的 follow_up——按原来的算法，那就是他那边四点半。
    现在：follow_up 被推到他说的时间加宽限之后；四点半排着的回访到点作废，
    连模型都不调；过了时间（也过了这一类的周期）才问得出去。
    """
    from newperson.models import FollowUp, LedgerEntry

    reply = ReplyPlan(
        parts=[ReplyPart(text="行")],
        ledger_entries=[
            LedgerEntry(
                kind="trading", claim="明天早上9点起来把回测跑完", when_there="09-29 09:00"
            )
        ],
        follow_up=FollowUp(delay_minutes=180, note="问问他回测跑完没"),
    )
    ask = ProactivePlan(send=True, parts=[ReplyPart(text="回测跑完了没")])
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [reply, ask], now=HIS_1_30
    )
    assert not app.rhythm.is_sleeping(HIS_1_30), "前提不成立：她这会儿在睡"

    await send(app, "我打算明天早上9点起来把回测跑完", at=HIS_1_30)
    await _deliver_one_reply(app, clock, memory)
    assert channel.texts == ["行"]

    nine_there = datetime(2026, 9, 29, 9, 0, tzinfo=SYDNEY)
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    assert follow.run_at >= nine_there + timedelta(hours=3), (
        f"follow_up 排在他那边 {follow.run_at.astimezone(SYDNEY):%m-%d %H:%M}"
    )

    # 另一条路：当天排好的回访正好落在他那边四点半
    dawn = datetime(2026, 9, 29, 4, 30, tzinfo=SYDNEY).astimezone(TZ)
    job_id = await app.scheduler.schedule(
        "proactive", dawn, conversation_id=CONVERSATION_ID,
        payload={"kind": "ledger_check", "note": "问一句他之前说要做的事"},
        reason="ledger_check",
    )
    calls = len(llm.calls)
    clock.set(dawn + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["行"], "他那边四点半就来问了"
    assert len(llm.calls) == calls, "还没到时间，连模型都不该调"
    assert (await memory.get_job(job_id)).status == "cancelled", "什么都没问，别记成做完"

    # 过了他说的时间加宽限、也过了交易那一类两天的周期，才问
    later = HIS_1_30 + timedelta(days=2, hours=2)
    assert not app.rhythm.is_sleeping(later), "前提不成立：挑的时刻她在睡"
    await memory.update_conversation(CONVERSATION_ID, deliverable=True)
    await app.scheduler.schedule(
        "proactive", later, conversation_id=CONVERSATION_ID,
        payload={"kind": "ledger_check", "note": "问一句他之前说要做的事"},
        reason="ledger_check",
    )
    for pending in await memory.pending_jobs("follow_up", CONVERSATION_ID):
        await app.scheduler.cancel(pending.id or 0)
    clock.set(later + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["行", "回测跑完了没"]
    note = llm.calls[-1]["messages"][0]["content"]
    assert "他那边 09-29 09:00（已经过了）" in note


async def test_a_follow_up_waits_for_a_plan_noted_after_it(
    tmp_path: Path, persona: Persona
) -> None:
    """follow_up 先排上，带时间的计划下一条回复才记下——到点那一刻也要挡住。

    排的时候看不见那条计划，只有发之前再查一次才拦得住。
    """
    from newperson.models import LedgerEntry

    app, channel, llm, clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    await app.life.schedule_follow_up(CONVERSATION_ID, 180, "问问他回测跑完没")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]

    clock.set(HIS_1_30 + timedelta(minutes=10))
    timing = app.life.resolve_when_there("09-29 09:00", clock.now())
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="明早九点跑回测", when_there="09-29 09:00")],
        clock.now(),
        [timing],
    )

    clock.set(follow.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == []
    assert llm.calls == []
    moved = await memory.get_job(follow.id or 0)
    assert moved.status == "pending"
    assert moved.run_at >= timing.ask_after
    assert moved.attempts == 0, "推迟不是失败，别吃掉重试次数"


async def test_the_prompt_says_not_yet_for_a_pending_timed_plan(
    tmp_path: Path, persona: Persona
) -> None:
    """他九点之前自己来说话，回复的上下文里要明明白白写着"还没到，别问"。

    回访那条路代码拦得住；回他消息这条路拦不住，只能靠这一段。
    过了时间，这一段就不在了。
    """
    from newperson.models import LedgerEntry

    app, _channel, llm, clock, memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="早")]), ReplyPlan(parts=[ReplyPart(text="嗯")])],
        now=HIS_1_30,
    )
    timing = app.life.resolve_when_there("09-29 09:00", HIS_1_30)
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="明早九点跑回测", when_there="09-29 09:00")],
        HIS_1_30,
        [timing],
    )

    seven_there = datetime(2026, 9, 29, 7, 0, tzinfo=SYDNEY).astimezone(TZ)
    clock.set(seven_there)
    await send(app, "起了", at=seven_there, msg_id=11)
    await _deliver_one_reply(app, clock, memory)
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "他说了时间、还没到的" in prompt
    assert "明早九点跑回测（他那边 09-29 09:00）" in prompt

    after = timing.ask_after.astimezone(TZ) + timedelta(hours=1)
    if app.rhythm.is_sleeping(after):
        after = app.rhythm.next_wake_after(after) + timedelta(hours=1)
    clock.set(after)
    await send(app, "跑完了", at=after, msg_id=12)
    await _deliver_one_reply(app, clock, memory)
    assert "他说了时间、还没到的" not in llm.calls[-1]["messages"][0]["content"]


async def test_unread_lines_carry_his_clock(tmp_path: Path, persona: Persona) -> None:
    """未读每一行都标着他那边几点。"明天"是相对他那一天说的。"""
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])], now=HIS_1_30
    )
    await send(app, "明早九点跑回测", at=HIS_1_30)
    await _deliver_one_reply(app, clock, memory)
    assert "[09-28 11:30｜他那边 09-29 01:30] 明早九点跑回测" in llm.calls[-1]["messages"][0]["content"]


async def test_the_ledger_command_shows_the_time_he_named(
    tmp_path: Path, persona: Persona
) -> None:
    """她记下的时间对不对，他一眼就能核对。写错了往早的方向，就是凌晨被问。"""
    from newperson.models import LedgerEntry
    from newperson.owner import OwnerContext, handle

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    timing = app.life.resolve_when_there("09-29 09:00", HIS_1_30)
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="明早九点跑回测", when_there="09-29 09:00")],
        HIS_1_30,
        [timing],
    )
    ctx = OwnerContext(
        memory=memory, rhythm=app.rhythm, scheduler=app.scheduler, life=app.life,
        conversation_id=CONVERSATION_ID, now=HIS_1_30,
    )
    out = await handle("!np ledger trading", ctx)
    assert "你说的时间：09-29 09:00" in out


async def test_abroad_the_prompt_tells_her_the_local_time(
    tmp_path: Path, persona: Persona
) -> None:
    """她飞去别的时区时，"现在是几点"和聊天记录的时间戳都按当地写。

    同一段里的起床时刻本来就是按当地算的；"现在"还按家里写的话，
    她会读到"现在 06:14"，紧接着"你今天 11:07 起"。
    """
    from datetime import date

    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    day = date(2026, 10, 1)
    while str(app.rhythm.tz_for(day)) == str(persona.tz) and day < date(2027, 9, 1):
        day += timedelta(days=1)
    assert day < date(2027, 9, 1), "前提不成立：一年里没有跨时区的出行"
    there = app.rhythm.tz_for(day + timedelta(days=1))
    noon = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=there)
    noon += timedelta(hours=13)
    home_now = noon.astimezone(TZ)
    clock.set(home_now)

    situation = await app._build_situation(home_now)
    assert "13:00，你这边的时间" in situation, situation.splitlines()[0]

    await send(app, "在干嘛", at=home_now, msg_id=77)
    await _deliver_one_reply(app, clock, memory)
    assert f"[{noon.strftime('%m-%d')} 13:00" in llm.calls[-1]["messages"][0]["content"]


async def test_after_a_restore_she_does_not_answer_what_she_already_answered(
    tmp_path: Path, persona: Persona
) -> None:
    """从备份恢复之后，备份之后那段时间她其实都回过了，回复还留在他手机上。

    原来补抓只捞他那一半（她自己的话被 _should_handle 当成机器人跳过），
    于是他那些话全变成未读，她把几小时前、早就回过的话再回一遍，
    上下文里还没有自己当时说了什么。**这条走真的 _should_handle**——
    别的补抓测试都把它换成了 lambda，这个 bug 就是那么漏过去的。
    """
    import discord

    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999

    def said(msg_id: int, text: str, at: datetime, *, hers: bool = False):
        return SimpleNamespace(
            id=msg_id,
            content=text,
            created_at=at,
            author=SimpleNamespace(
                id=999 if hers else 42, display_name="她" if hers else "Leo", bot=hers
            ),
            channel=dm_kind,
            attachments=[],
        )

    # 备份停在这里：库里最后一条是他 100 号那句，她还没回
    await send(app, "备份之前说的", at=EVENING, msg_id=100)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)

    later = EVENING + timedelta(hours=1)
    dm = FakeHistoryChannel(
        [
            said(101, "她当时回的第一句", EVENING + timedelta(minutes=20), hers=True),
            said(102, "我明天要去面试", later),
            said(103, "有点紧张", later + timedelta(minutes=1)),
            said(104, "你可以的", later + timedelta(minutes=9), hers=True),
            said(105, "晚上去吃拉面", later + timedelta(hours=2)),
        ]
    )
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda _cid: None,
    )
    clock.set(later + timedelta(hours=3))
    marker = _just_restored(app)
    await app.catch_up()
    assert not marker.exists(), "补完了，记号该清掉"

    unread = await memory.unread_messages(CONVERSATION_ID)
    assert [m.content for m in unread] == ["晚上去吃拉面"], "她回过的话又变成了未读"
    history = await memory.recent_messages(CONVERSATION_ID, 20)
    hers = [m.content for m in history if m.author_kind == "bot"]
    assert hers == ["她当时回的第一句", "你可以的"], "她当时说过的话没补进来"

    await drain(app, clock)
    prompt = llm.calls[-1]["messages"][0]["content"]
    mine = prompt.split("## 他刚发的")[1]
    assert "我明天要去面试" not in mine and "晚上去吃拉面" in mine
    assert "你可以的" in prompt, "上下文里没有她自己当时的回复"


async def test_after_a_restore_with_nothing_new_she_stays_quiet(
    tmp_path: Path, persona: Persona
) -> None:
    """备份之后的每一句她都回过了：补回来的全是已读，不排回复。"""
    import discord

    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    his = SimpleNamespace(
        id=201, content="在吗", created_at=EVENING + timedelta(minutes=1),
        author=SimpleNamespace(id=42, display_name="Leo", bot=False),
        channel=dm_kind, attachments=[],
    )
    hers = SimpleNamespace(
        id=202, content="在", created_at=EVENING + timedelta(minutes=4),
        author=SimpleNamespace(id=999, display_name="她", bot=True),
        channel=dm_kind, attachments=[],
    )
    # 备份里最后一条是更早的一句，已经回过
    await send(app, "更早的", at=EVENING - timedelta(hours=1), msg_id=200)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)

    dm = FakeHistoryChannel([his, hers])
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda _cid: None,
    )
    clock.set(EVENING + timedelta(hours=2))
    _just_restored(app)
    await app.catch_up()
    assert await memory.unread_messages(CONVERSATION_ID) == []
    assert await memory.pending_jobs("reply", CONVERSATION_ID) == []
    history = await memory.recent_messages(CONVERSATION_ID, 5)
    asked = next(m for m in history if m.content == "在吗")
    answer = next(m for m in history if m.author_kind == "bot")
    assert answer.content == "在"
    assert asked.read_at == answer.created_at, "他那句该算作被她这句回掉的那一批"


async def test_a_faded_fact_can_be_learned_again(tmp_path: Path, persona: Persona) -> None:
    """记忆整理时，"别重复"的名单只列她现在还记得的，最清楚的在前。

    原来列的是全量、从老到新截四十条：淡忘了的还挂在"别重复"里，
    他再提一次模型也被要求别记——每件事满九十天必忘，之后也学不回来。
    最新记下的反而被截掉，换个说法又记一遍。
    """
    from newperson.models import MemoryUpdate

    seen = []

    class RecordingBrain:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def update_memory(self, request, _day):
            seen.append(request)
            return MemoryUpdate(summary="摘要", owner_facts=[], self_facts=[])

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    app.brain = RecordingBrain(app.brain)

    await memory.add_facts("owner", ["很久以前说过的事"], EVENING - timedelta(days=120))
    for i in range(45):
        await memory.add_facts("owner", [f"事实{i:02d}"], EVENING - timedelta(days=45 - i))
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID, discord_message_id=7001, author_id=42,
            author_name="Leo", content="又提起那件很久以前的事", created_at=EVENING,
        )
    )
    job_id = await app.scheduler.schedule(
        "memory_update", EVENING, conversation_id=CONVERSATION_ID, payload={}
    )
    assert job_id
    await app.scheduler.run_due_once()

    assert seen, "记忆整理没跑"
    listed = seen[-1].existing_owner_facts
    assert "很久以前说过的事" not in listed, "已经淡忘的还挂在'别重复'里，他再提也记不回来"
    assert "事实44" in listed, "最新记下的反而不在名单上"


async def test_his_message_does_not_turn_her_green(tmp_path: Path, persona: Persona) -> None:
    """在线状态跟着她自己拿手机走，不跟着他发消息走。

    原来他一发她就亮绿灯：30 秒内变绿、几分钟后变黄、几十分钟后在黄灯下回他，
    上课时也亮。那是一条百分之百的规律，比什么都像程序。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    app._started = True
    client = NewPersonClient(app)
    await client.on_ready()
    for task in [t for t in app._tasks if t.get_name() == "presence"]:
        task.cancel()
    assert client.presence is not None
    assert app.on_phone is not None, "on_ready 没把'她拿起手机'接到在线状态上"
    # 这条测的是在线状态，不是找频道：发消息还走假频道
    app.client = SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _cid: channel)

    app._should_handle = lambda _m: True
    clock.set(EVENING)
    await client.on_message(fake_incoming(100, "在吗", EVENING))
    assert client.presence._online_until is None, "他一发消息她就亮了"

    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["嗯"]
    assert client.presence._online_until is not None, "她回消息的时候也没亮"
    assert client.presence._online_until > job.run_at
    # 而且是**马上**亮的，不是等下一轮一分钟的循环：不然气泡总比绿灯先到
    assert client.presence._current is not None and client.presence._current[0] == "online"


async def test_her_promise_is_not_dropped_when_they_are_mid_chat(
    tmp_path: Path, persona: Persona
) -> None:
    """她说"等我查一下"，十分钟后那个 follow_up 到点时他们正聊着——不能就这么没了。

    原来撞上热聊或未读就 return，任务记成做完：她自己许的承诺凭空消失，
    而且恰好在最容易许诺的时候（正聊着天）必然丢。
    """
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    clock.set(EVENING)
    await app.life.schedule_follow_up(CONVERSATION_ID, 10, "告诉他查到的那个参数")
    job = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]

    # 到点那一刻他们正热聊：他刚说完，她刚回完
    clock.set(job.run_at + timedelta(seconds=1))
    await memory.update_conversation(
        CONVERSATION_ID,
        last_user_message_at=clock.now() - timedelta(seconds=40),
        last_bot_message_at=clock.now() - timedelta(seconds=20),
    )
    await app.scheduler.run_due_once()
    again = await memory.get_job(job.id or 0)
    assert again.status == "pending", "正聊着，她答应的事就这么没了"
    assert again.run_at > clock.now()
    assert again.attempts == 0, "往后挪不是失败"


async def test_her_promise_rides_along_with_the_reply_she_is_about_to_send(
    tmp_path: Path, persona: Persona
) -> None:
    """follow_up 到点时她正好要回他：把答应的事并进这次回复里说。"""
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="查到了 是0.3")])]
    )
    clock.set(EVENING)
    await app.life.schedule_follow_up(CONVERSATION_ID, 30, "告诉他查到的那个参数是0.3")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]

    at = follow.run_at - timedelta(seconds=5)
    clock.set(at)
    await send(app, "你那边怎么样", at=at, msg_id=321)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, follow.run_at + timedelta(minutes=5))

    clock.set(follow.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    # 并进去了，但回复还没说出口之前不作废
    assert (await memory.get_job(follow.id or 0)).status == "pending"
    reply_job = await memory.get_job(reply.id or 0)
    clock.set(reply_job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "你之前答应过他的事，这次顺便说：告诉他查到的那个参数是0.3" in prompt
    assert (await memory.get_job(follow.id or 0)).status == "cancelled", "说出口了还留着，会再说一遍"


async def test_a_merged_promise_survives_a_reply_that_says_nothing(
    tmp_path: Path, persona: Persona
) -> None:
    """并进回复之后，回复却什么都没发（他那句是"好"，她不打算接）：承诺还在，到点照说。"""
    app, _channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[])]
    )
    clock.set(EVENING)
    await app.life.schedule_follow_up(CONVERSATION_ID, 30, "告诉他查到的参数")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    at = follow.run_at - timedelta(seconds=5)
    clock.set(at)
    await send(app, "好", at=at, msg_id=654)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, follow.run_at + timedelta(minutes=5))
    clock.set(follow.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    reply_job = await memory.get_job(reply.id or 0)
    clock.set(reply_job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert (await memory.get_job(follow.id or 0)).status == "pending", "回复没说出口，承诺却没了"


async def test_a_deferred_promise_survives_a_restart(tmp_path: Path, persona: Persona) -> None:
    """她答应的事睡着时被挪到早上，中间重启一次：不能按最初那个时刻判成"停机太久"作废。"""
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    night = EVENING
    while not app.rhythm.is_sleeping(night):
        night += timedelta(minutes=30)
    clock.set(night - timedelta(minutes=40))
    await app.life.schedule_follow_up(CONVERSATION_ID, 1, "告诉他查到的参数")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    await memory.reschedule_job(follow.id or 0, night + timedelta(minutes=5))
    clock.set(night + timedelta(minutes=6))
    await app.handle_proactive_job(await memory.get_job(follow.id or 0))
    moved = await memory.get_job(follow.id or 0)
    assert moved.status == "pending" and moved.run_at > clock.now()
    clock.set(moved.run_at - timedelta(minutes=1))
    await app.scheduler.recover()
    assert (await memory.get_job(follow.id or 0)).status == "pending", "一重启就当成停机太久作废了"


async def test_a_proactive_that_says_nothing_is_not_counted_as_done(
    tmp_path: Path, persona: Persona
) -> None:
    """什么都没发的主动任务记成作废。体检按做完的任务数她主动的花样，
    空照片库的 window_photo 记成做完，就会凭空多出一条"她主动说过"。"""
    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    clock.set(EVENING)
    job_id = await app.scheduler.schedule(
        "proactive", EVENING, conversation_id=CONVERSATION_ID,
        payload={"kind": "window_photo", "note": "拍窗外", "requires_photo": True},
        reason="window_photo",
    )
    await app.scheduler.run_due_once()
    assert channel.sent == [] and llm.calls == []
    assert (await memory.get_job(job_id)).status == "cancelled"


async def test_leave_notes_are_not_put_in_her_mouth(tmp_path: Path, persona: Persona) -> None:
    """`!np away 出差 5` 说的是他出门。原来却变成她的"此刻"：你最近出差。

    一个在读研究生被告知自己在出差，会在回复里说出来。写成"他在出差"也不行：
    那是他没跟她说过的事。备注只给他自己看，她只是这几天话少。
    """
    from newperson.owner import OwnerContext, handle

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    ctx = OwnerContext(
        memory=memory, rhythm=app.rhythm, scheduler=app.scheduler, life=app.life,
        conversation_id=CONVERSATION_ID, now=EVENING,
    )
    await handle("!np away 出差 5", ctx)
    situation = await app._build_situation(EVENING)
    assert "出差" not in situation
    assert "没什么心思聊天" in situation


async def test_a_resumed_reply_quotes_the_right_message(tmp_path: Path, persona: Persona) -> None:
    """发到一半断了、重试接着发时，引用的还得是这一批里的那一条。

    原来续发时"这一批"被重建成最近四十行里 id 不超过 covers 的全部，
    早就回过的旧话也在里面；reply_to_index 按原来那一批算的下标一错位，
    她就引用九个小时前的一句去回。
    """
    script = [ReplyPlan(parts=[ReplyPart(text=f"回{i}")]) for i in range(3)]
    script.append(ReplyPlan(parts=[ReplyPart(text="这个")], reply_to_index=0))
    app, channel, _llm, clock, _memory = await build(tmp_path, persona, script)
    at = EVENING
    mid = 100
    for i in range(3):
        await send(app, f"老消息{i}", at=at, msg_id=mid)
        mid += 1
        await drain(app, clock, hops=12)
        at = clock.now() + timedelta(hours=3)
    await send(app, "新消息A", at=at, msg_id=mid)
    await send(app, "新消息B", at=at + timedelta(seconds=30), msg_id=mid + 1)

    quoted: list[int | None] = []
    original = channel.send
    state = {"fail": True}

    async def flaky(content=None, *, file=None, reference=None):
        if state["fail"]:
            state["fail"] = False
            raise ConnectionResetError("网络断了一下")
        quoted.append(getattr(reference, "id", None))
        return await original(content, file=file, reference=reference)

    channel.send = flaky
    await drain(app, clock, hops=20)
    assert "这个" in channel.texts
    assert quoted[-1] == mid, f"引用的是 {quoted[-1]}，该是 {mid}"


def _just_restored(app: App) -> Path:
    """restore.sh --install 留的记号。有它，补抓才补她自己说过的话。"""
    marker = Path(app.settings.db_path).parent / ".just_restored"
    marker.write_text("2026-09-28T16:00:00Z\n", encoding="utf-8")
    return marker


def _dm_said(dm_kind, msg_id: int, text: str, at: datetime, *, hers: bool = False):
    return SimpleNamespace(
        id=msg_id, content=text, created_at=at,
        author=SimpleNamespace(id=999 if hers else 42, display_name="她" if hers else "Leo", bot=hers),
        channel=dm_kind, attachments=[],
    )


def _wire_dm(app: App, history: list) -> None:
    dm = FakeHistoryChannel(history)
    app.client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_user=lambda _id: SimpleNamespace(dm_channel=dm),
        get_channel=lambda _cid: None,
    )


async def test_catch_up_never_takes_a_command_reply_for_her_words(
    tmp_path: Path, persona: Persona
) -> None:
    """!np 的回执是程序在私聊里发的，不入库。补抓时不能把它当成她说的话：

    那会让"今天调了几次模型、没有异地备份"进到她的上下文里（不变量 5），
    还会把回执之前他的未读全标成已读——那两句她就永远不回了（不变量 2）。
    新的回执按编号认，老版本发的按"紧跟在 !np 后面几秒内"认。
    """
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "更早的", at=EVENING - timedelta(hours=1), msg_id=100)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)

    await app._remember_command_reply(105)
    _wire_dm(app, [
        _dm_said(dm_kind, 101, "在吗", EVENING),
        _dm_said(dm_kind, 102, "晚上吃了吗", EVENING + timedelta(minutes=35)),
        _dm_said(dm_kind, 103, "!np status", EVENING + timedelta(minutes=40)),
        _dm_said(dm_kind, 104, "**在晚课** 活跃度 0.04", EVENING + timedelta(minutes=40, seconds=1), hers=True),
        _dm_said(dm_kind, 105, "上次备份 3 小时前", EVENING + timedelta(minutes=50), hers=True),
    ])
    clock.set(EVENING + timedelta(minutes=55))
    _just_restored(app)  # 最要紧的是恢复之后那一轮：那时才会补她的话
    await app.catch_up()

    history = await memory.recent_messages(CONVERSATION_ID, 20)
    assert not [m for m in history if m.author_kind == "bot"], "回执进库了"
    assert [m.content for m in await memory.unread_messages(CONVERSATION_ID)] == ["在吗", "晚上吃了吗"]


async def test_a_reconnect_does_not_swallow_what_he_said_while_she_typed(
    tmp_path: Path, persona: Persona
) -> None:
    """平时重连也会从上次的游标往后重翻，她这段时间说的话早就在库里。

    那些已经在库里的，不能拿来把他的未读标成已读：他在她打字那几秒里
    插的一句，时间早于她那条气泡，一标就没人回了。
    """
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "在吗", at=EVENING, msg_id=200)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)
    # 游标钉在 200，之后两边的话都照常进了库
    _wire_dm(app, [_dm_said(dm_kind, 200, "在吗", EVENING)])
    await app.catch_up()
    await send(app, "我跟你说个事", at=EVENING + timedelta(seconds=5), msg_id=201)
    await memory.add_bot_message(
        CONVERSATION_ID, "在", EVENING + timedelta(seconds=9), discord_message_id=202,
        reply_batch=EVENING,
    )

    _wire_dm(app, [
        _dm_said(dm_kind, 200, "在吗", EVENING),
        _dm_said(dm_kind, 201, "我跟你说个事", EVENING + timedelta(seconds=5)),
        _dm_said(dm_kind, 202, "在", EVENING + timedelta(seconds=9), hers=True),
    ])
    clock.set(EVENING + timedelta(seconds=19))
    await app.catch_up()
    assert [m.content for m in await memory.unread_messages(CONVERSATION_ID)] == ["我跟你说个事"]
    # 恢复之后那一轮也一样：已经在库里的那句不能拿来标已读
    _just_restored(app)
    await app.catch_up()
    assert [m.content for m in await memory.unread_messages(CONVERSATION_ID)] == ["我跟你说个事"]


async def test_after_a_restore_she_does_not_repeat_a_proactive_she_already_sent(
    tmp_path: Path, persona: Persona
) -> None:
    """恢复出来的库里还排着备份那会儿的主动消息。她在 Discord 上其实已经说过了，
    补抓补回了那句——那个任务就该作废，不然同一句话说两遍。"""
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "明天面试", at=EVENING, msg_id=300)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)
    asked_at = EVENING + timedelta(hours=15)
    job_id = await app.scheduler.schedule(
        "follow_up", asked_at, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "问问面试"}, reason="follow_up",
    )
    later_id = await app.scheduler.schedule(
        "proactive", asked_at + timedelta(hours=6), conversation_id=CONVERSATION_ID,
        payload={"kind": "own_life", "note": "说说自己"}, reason="own_life",
    )
    _wire_dm(app, [
        _dm_said(dm_kind, 300, "明天面试", EVENING),
        _dm_said(dm_kind, 301, "面试怎么样了", asked_at, hers=True),
    ])
    clock.set(asked_at + timedelta(hours=2))
    _just_restored(app)
    await app.catch_up()
    assert (await memory.get_job(job_id)).status == "cancelled", "发过的那句又要发一遍"
    assert (await memory.get_job(later_id)).status == "pending", "还没到点的不该动"


async def test_she_knows_a_trip_is_coming_before_she_leaves(
    tmp_path: Path, persona: Persona
) -> None:
    """日历里排好的出行，出发前几天就该进她的上下文。

    原来出发前一个字不提，到了那天忽然"人在冰岛"，时差和回复节奏一起变了。
    """
    from datetime import date

    app, _channel, _llm, clock, _memory = await build(tmp_path, persona, [])
    day = date(2026, 10, 1)
    while app.calendar.trip_for(day) is None and day < date(2027, 9, 1):
        day += timedelta(days=1)
    trip = app.calendar.trip_for(day)
    assert trip is not None, "前提不成立：一年里没有出行"
    before = trip.start - timedelta(days=3)
    moment = datetime.combine(before, datetime.min.time(), tzinfo=TZ) + timedelta(hours=15)
    clock.set(moment)
    situation = await app._build_situation(moment)
    assert trip.place in situation and "3 天后" in situation
    far = datetime.combine(trip.start - timedelta(days=20), datetime.min.time(), tzinfo=TZ)
    assert trip.place not in await app._build_situation(far + timedelta(hours=15))


async def test_a_follow_up_is_held_even_when_it_would_ride_along_with_a_reply(
    tmp_path: Path, persona: Persona
) -> None:
    """follow_up 到点时正好有回复要发：先过时间闸，再谈并进回复。

    原来"有未读就并进回复"排在闸前面，那句"跑完没"跟着回复在他凌晨发了出去。
    """
    from newperson.models import LedgerEntry

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    await app.life.schedule_follow_up(CONVERSATION_ID, 60, "问问他回测跑完没")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    await memory.reschedule_job(follow.id or 0, HIS_1_30 + timedelta(minutes=60))

    clock.set(HIS_1_30 + timedelta(minutes=10))
    timing = app.life.resolve_when_there("09-29 09:00", clock.now())
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="明早九点跑回测", when_there="09-29 09:00")],
        clock.now(), [timing],
    )
    at = HIS_1_30 + timedelta(minutes=60) - timedelta(seconds=5)
    clock.set(at)
    await send(app, "对了", at=at, msg_id=77)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, HIS_1_30 + timedelta(minutes=65))

    clock.set(HIS_1_30 + timedelta(minutes=60, seconds=1))
    await app.scheduler.run_due_once()
    held = await memory.get_job(follow.id or 0)
    assert held.status == "pending", "并进回复里，在他说的时间之前问出口了"
    assert held.run_at >= timing.ask_after
    hints = (await memory.get_job(reply.id or 0)).payload.get("hints", [])
    assert not any("跑完" in h for h in hints)


async def test_saying_it_again_with_a_time_is_seen_by_the_follow_up_guard(
    tmp_path: Path, persona: Persona
) -> None:
    """同一件事三天前记过（没带时间），今天才说"明早九点"：follow_up 的闸也得认得它。

    原来闸按 created_at 圈窗口，那条还是三天前记的，圈不进来，follow_up 在他凌晨照发。
    """
    from newperson.models import LedgerEntry

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="把回测跑完")], HIS_1_30 - timedelta(days=3)
    )
    timing = app.life.resolve_when_there("09-29 09:00", HIS_1_30)
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="把回测跑完", when_there="09-29 09:00")],
        HIS_1_30, [timing],
    )
    await app.life.schedule_follow_up(CONVERSATION_ID, 180, "问问他回测跑完没")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    assert follow.run_at >= timing.ask_after


async def test_a_far_off_plan_does_not_hold_her_own_promise_for_weeks(
    tmp_path: Path, persona: Persona
) -> None:
    """他说"10 月 20 号早上考试"，她说"那个参数我查完告诉你"：她的承诺不能被压三周。

    同一时间只排一个 follow_up，被压着的那个还会把她后来答应的全挤掉。
    """
    from newperson.models import LedgerEntry

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    app.scheduler.delay_scale = 1.0
    timing = app.life.resolve_when_there("10-20 09:00", HIS_1_30)
    assert timing is not None
    await memory.add_ledger_entries(
        [LedgerEntry(kind="study", claim="期中考试", when_there="10-20 09:00")],
        HIS_1_30, [timing],
    )
    await app.life.schedule_follow_up(CONVERSATION_ID, 30, "告诉他查到的参数")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    assert follow.run_at - HIS_1_30 < timedelta(days=2), f"被压到了 {follow.run_at}"



async def test_a_normal_reconnect_leaves_her_side_alone(tmp_path: Path, persona: Persona) -> None:
    """平时重连（没有刚恢复的记号）不补她的话。

    库里本来就不是她发过的每一条都有：纯图片那条不记。平时也补的话，
    它会被补成一条空白的"她说的话"，体检多数一次主动开口；恢复专用的清理
    还会跟着触发，把她被挪后的承诺作废。
    """
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "吃饭了吗", at=EVENING, msg_id=400)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)
    promise = await app.scheduler.schedule(
        "follow_up", EVENING + timedelta(hours=1), conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他查到的"}, reason="follow_up",
    )
    await memory.reschedule_job(promise, EVENING + timedelta(hours=1))
    await memory.db.execute(
        "UPDATE jobs SET original_run_at = ? WHERE id = ?",
        ((EVENING - timedelta(minutes=30)).isoformat(), promise),
    )
    await memory.db.commit()
    _wire_dm(app, [
        _dm_said(dm_kind, 400, "吃饭了吗", EVENING),
        _dm_said(dm_kind, 401, "", EVENING + timedelta(minutes=5), hers=True),
    ])
    clock.set(EVENING + timedelta(minutes=20))
    await app.catch_up()
    history = await memory.recent_messages(CONVERSATION_ID, 10)
    assert not [m for m in history if m.author_kind == "bot"], "平时重连也把她的话补进来了"
    assert (await memory.get_job(promise)).status == "pending", "她的承诺被当成恢复后的旧任务作废了"


async def test_after_a_restore_several_bubbles_are_one_reply(tmp_path: Path, persona: Persona) -> None:
    """恢复后补回来的连着几个气泡是同一次回复，体检不能把后面那几个算成主动开口。"""
    import discord

    from newperson import doctor

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "更早的", at=EVENING - timedelta(hours=1), msg_id=500)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)
    _wire_dm(app, [
        _dm_said(dm_kind, 501, "明天面试", EVENING + timedelta(minutes=1)),
        _dm_said(dm_kind, 502, "加油", EVENING + timedelta(minutes=11), hers=True),
        _dm_said(dm_kind, 503, "你可以的", EVENING + timedelta(minutes=11, seconds=4), hers=True),
    ])
    clock.set(EVENING + timedelta(hours=1))
    _just_restored(app)
    await app.catch_up()
    rows = await memory.db.execute(
        "SELECT reply_batch FROM messages WHERE author_kind = 'bot' ORDER BY id"
    )
    batches = [r[0] for r in await rows.fetchall()]
    assert len(batches) == 2 and batches[0] and batches[0] == batches[1], batches
    import sqlite3

    conn = sqlite3.connect(app.settings.db_path)
    conn.row_factory = sqlite3.Row
    _b, opened, _floor = doctor._batches_and_runs(conn, EVENING - timedelta(days=1))
    conn.close()
    assert opened == 0, f"一次回复被算成了 {opened} 次主动开口"



async def test_a_chain_of_plans_cannot_stretch_the_hold_past_its_cap(
    tmp_path: Path, persona: Persona
) -> None:
    """考试周连着几件事：她答应的 follow_up 被压着，每到点一次上限不能跟着往后滑。"""
    from newperson.models import LedgerEntry

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [], now=HIS_1_30)
    app.scheduler.delay_scale = 1.0
    entries, timings = [], []
    for day in (30, 1, 2, 3):
        month = 9 if day == 30 else 10
        when = f"{month:02d}-{day:02d} 09:00"
        entries.append(LedgerEntry(kind="study", claim=f"考试{day}", when_there=when))
        timings.append(app.life.resolve_when_there(when, HIS_1_30))
    await memory.add_ledger_entries(entries, HIS_1_30, timings)
    await app.life.schedule_follow_up(CONVERSATION_ID, 30, "告诉他查到的参数")
    cap = HIS_1_30 + timedelta(minutes=30) + timedelta(
        hours=persona.proactive.ledger_timed.follow_up_hold_max_hours
    )
    for _ in range(6):
        job = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
        assert job.run_at <= cap + timedelta(hours=12), f"被压到了 {job.run_at}"
        clock.set(job.run_at + timedelta(seconds=1))
        with contextlib.suppress(RuntimeError):  # 放行之后会去调模型，这里没准备脚本
            await app.handle_proactive_job(await memory.get_job(job.id or 0))
        moved = await memory.get_job(job.id or 0)
        if moved.status != "pending" or moved.run_at == job.run_at:
            break


async def test_a_merged_promise_survives_an_interrupted_reply(
    tmp_path: Path, persona: Persona
) -> None:
    """并进回复的承诺在后面那个气泡里，回复发到一半被他打断：承诺还在，不作废。"""
    plan = ReplyPlan(parts=[ReplyPart(text="到家啦"), ReplyPart(text="对了 那个参数是0.3")])
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    clock.set(EVENING)
    await app.life.schedule_follow_up(CONVERSATION_ID, 30, "告诉他参数是0.3")
    follow = (await memory.pending_jobs("follow_up", CONVERSATION_ID))[0]
    at = follow.run_at - timedelta(seconds=5)
    clock.set(at)
    await send(app, "你到家没", at=at, msg_id=870)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, follow.run_at + timedelta(minutes=5))
    clock.set(follow.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()

    original = channel.send

    async def and_he_speaks(content=None, *, file=None, reference=None):
        sent = await original(content, file=file, reference=reference)
        if len(channel.sent) == 1:
            await send(app, "哈哈", at=clock.now(), msg_id=871)
        return sent

    channel.send = and_he_speaks
    reply_job = await memory.get_job(reply.id or 0)
    clock.set(reply_job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["到家啦"]
    assert (await memory.get_job(follow.id or 0)).status == "pending", "没说出口的承诺被作废了"


async def test_restore_waits_for_a_channel_it_could_not_reach(
    tmp_path: Path, persona: Persona
) -> None:
    """恢复后第一次补抓有个频道拿不到：记号要留着，下次重连接着补她的话。"""
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "更早的", at=EVENING - timedelta(hours=1), msg_id=900)
    _wire_dm(app, [_dm_said(dm_kind, 900, "更早的", EVENING - timedelta(hours=1))])
    app.settings.proactive_channel_id = 555

    async def unreachable(_cid=None):
        raise RuntimeError("503")

    original = app.resolve_channel

    async def resolve(channel_id=None):
        if channel_id == 555:
            return await unreachable()
        return await original(channel_id)

    app.resolve_channel = resolve
    marker = _just_restored(app)
    clock.set(EVENING)
    await app.catch_up()
    assert marker.exists(), "有个频道没翻到，恢复记号却清掉了"


async def test_after_a_restore_a_later_opener_is_not_part_of_the_reply(
    tmp_path: Path, persona: Persona
) -> None:
    """她回完一句，几个小时后自己又开口：那是主动开口，不能继承那次回复的批次。"""
    import discord

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    dm_kind = object.__new__(discord.DMChannel)
    dm_kind.id = 999
    await send(app, "更早的", at=EVENING - timedelta(hours=1), msg_id=950)
    await memory.mark_read([m.id for m in await memory.unread_messages(CONVERSATION_ID)], EVENING)
    for job in await memory.pending_jobs("reply", CONVERSATION_ID):
        await app.scheduler.cancel(job.id or 0)
    _wire_dm(app, [
        _dm_said(dm_kind, 951, "明天面试", EVENING + timedelta(minutes=1)),
        _dm_said(dm_kind, 952, "加油", EVENING + timedelta(minutes=11), hers=True),
        _dm_said(dm_kind, 953, "今天实验室好冷", EVENING + timedelta(hours=3), hers=True),
    ])
    clock.set(EVENING + timedelta(hours=4))
    _just_restored(app)
    await app.catch_up()
    rows = await memory.db.execute(
        "SELECT content, reply_batch FROM messages WHERE author_kind = 'bot' ORDER BY id"
    )
    got = {r[0]: r[1] for r in await rows.fetchall()}
    assert got["加油"] and got["今天实验室好冷"] is None, got


# -- 睡前说一声再走 ------------------------------------------------------------


async def _bedtime(app: App, around: datetime) -> datetime:
    return app.rhythm.next_sleep_after(around)


async def test_she_says_she_is_going_to_sleep_when_they_were_chatting(
    tmp_path: Path, persona: Persona
) -> None:
    """聊着聊着到了她睡觉的点：她说一句再走，不是一声不吭下线、第二天才回。"""
    goodnight = ProactivePlan(send=True, parts=[ReplyPart(text="困了 我睡了")])
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="哈哈")]), goodnight]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=30)
    clock.set(at)
    await send(app, "你还没睡啊", at=at, msg_id=1200)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, memory)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"]
    assert len(sign) == 1 and sign[0].run_at < bedtime
    clock.set(sign[0].run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts[-1] == "困了 我睡了"
    assert clock.now() < bedtime


async def test_his_last_words_before_her_bedtime_get_answered_before_she_sleeps(
    tmp_path: Path, persona: Persona
) -> None:
    """他在她睡前几分钟说了一句，回复本来排到了明天：睡前回完，顺便说一声。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="我也是"), ReplyPart(text="睡了")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=12)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=1300)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, bedtime + timedelta(hours=9))
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(max(sign.run_at, at + timedelta(seconds=30)) + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    moved = await memory.get_job(reply.id or 0)
    assert moved.run_at < bedtime, "他那句还是要等到明天"
    clock.set(moved.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["我也是", "睡了"]
    assert "你准备睡了" in llm.calls[-1]["messages"][0]["content"]


async def test_she_does_not_announce_bedtime_when_the_chat_died_long_ago(
    tmp_path: Path, persona: Persona
) -> None:
    """聊天早就停了的晚上，她直接睡，不专门跑来道晚安。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=85)
    clock.set(at)
    await send(app, "在干嘛", at=at, msg_id=1400)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, memory)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    calls = len(llm.calls)
    clock.set(sign.run_at + timedelta(seconds=1))
    assert clock.now() - at > timedelta(minutes=30)
    await app.scheduler.run_due_once()
    assert channel.texts == ["嗯"] and len(llm.calls) == calls
    assert (await memory.get_job(sign.id or 0)).status == "cancelled"


async def test_there_is_at_most_one_goodnight_a_night(tmp_path: Path, persona: Persona) -> None:
    """他在她睡前连着说好几句，睡前那一句也只排一个。"""
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    bedtime = await _bedtime(app, EVENING)
    for i in range(4):
        clock.set(bedtime - timedelta(minutes=40 - i * 5))
        await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    assert len([j for j in await memory.pending_jobs() if j.kind == "sign_off"]) == 1


async def test_a_reply_pulled_before_bedtime_drops_its_stale_timing_hints(
    tmp_path: Path, persona: Persona
) -> None:
    """排到明早的回复被拉回睡前：提示按新时刻重算，不在旧的后面追加。

    原来拉回来的那条还带着"他这条是九个小时前发的，别说在睡觉""你其实早看到了"，
    再追加一句"你准备睡了"——三条互相矛盾，而他几分钟前刚说过话。
    """
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="我也是")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=12)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=1500)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    stale = {
        **reply.payload,
        "hints": [
            "他这条消息是 9.5 小时 前发的。**别解释这段时间你在干嘛**：不说在睡觉。",
            "你其实早看到了，只是当时没回。别提这件事。",
        ],
    }
    await app.scheduler.reschedule(reply.id or 0, bedtime + timedelta(hours=9), stale)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(max(sign.run_at, at + timedelta(seconds=30)) + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    moved = await memory.get_job(reply.id or 0)
    clock.set(moved.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "小时 前发的" not in prompt and "早看到了" not in prompt, prompt
    assert persona.proactive.sign_off.reply_note in prompt


async def test_a_goodnight_reply_that_slips_past_bedtime_does_not_say_goodnight_at_breakfast(
    tmp_path: Path, persona: Persona
) -> None:
    """并进睡前的那条回复又被推迟过了睡点：醒来回的时候不该还说"我要睡了"。

    原来"你准备睡了"是写死进任务里的，额度用完顺延到明天、暂停中一再推迟，
    它都跟着走。现在要不要说是生成那一刻决定的。
    """
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="早")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=12)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=1600)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, bedtime + timedelta(hours=9))
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(max(sign.run_at, at + timedelta(seconds=30)) + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert (await memory.get_job(reply.id or 0)).payload.get("sign_off_before")

    morning = app.rhythm.next_wake_after(bedtime) + timedelta(minutes=30)
    await memory.reschedule_job(reply.id or 0, morning)  # 比如额度用完、顺延到了明早
    clock.set(morning + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert persona.proactive.sign_off.reply_note not in prompt
    assert "要睡" not in prompt


async def test_a_goodnight_reply_delayed_into_her_sleep_waits_until_she_wakes(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前被拉回来的那条回复，重试退避之后过了睡点：醒来再回，不在她睡着时发。

    拉回来之后离睡点只剩几分钟，两次瞬时失败的退避就能把它推过去。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="早")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=12)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=1650)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, bedtime + timedelta(hours=9))
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(max(sign.run_at, at + timedelta(seconds=30)) + timedelta(seconds=1))
    await app.scheduler.run_due_once()

    asleep = bedtime + timedelta(minutes=3)
    assert app.rhythm.is_sleeping(asleep)
    await memory.reschedule_job(reply.id or 0, asleep)  # 比如重试退避推过了睡点
    clock.set(asleep + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [] and llm.calls == []
    moved = await memory.get_job(reply.id or 0)
    assert moved.status == "pending" and not app.rhythm.is_sleeping(moved.run_at)
    clock.set(moved.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["早"]
    assert persona.proactive.sign_off.reply_note not in llm.calls[-1]["messages"][0]["content"]


async def test_she_says_goodnight_where_he_was_talking_not_in_the_public_channel(
    tmp_path: Path, persona: Persona
) -> None:
    """私聊聊到睡前，那句"我睡了"回到私聊。

    不带参数的 resolve_channel 是主动消息的去处，配了公开频道就是那里——
    睡前这句是刚才那段对话的收尾，不是主动开口。
    """
    goodnight = ProactivePlan(send=True, parts=[ReplyPart(text="困了 我睡了")])
    app, _channel, _llm, clock, _memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="哈哈")]), goodnight]
    )
    dm, public = FakeChannel(), FakeChannel()
    app.settings.proactive_channel_id = 777

    async def resolve(channel_id=None):
        return dm if channel_id == 999 else public

    app.resolve_channel = resolve
    await app._remember_inbound(999)
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=30)
    clock.set(at)
    await send(app, "你还没睡啊", at=at, msg_id=1700)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, app.memory)
    sign = [j for j in await app.memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(sign.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert dm.texts == ["哈哈", "困了 我睡了"]
    assert public.texts == []


async def test_nothing_else_pops_up_after_she_said_she_was_going_to_sleep(
    tmp_path: Path, persona: Persona
) -> None:
    """说完"我睡了"，同一晚排着的主动消息作废，答应他的事挪到她醒来之后。"""
    goodnight = ProactivePlan(send=True, parts=[ReplyPart(text="困了 我睡了")])
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="哈哈")]), goodnight]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=30)
    clock.set(at)
    await send(app, "你还没睡啊", at=at, msg_id=1800)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, memory)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    # 道别之后隔过"正在热聊"那几分钟、又还没到睡点：原来这段里什么都拦不住
    await memory.reschedule_job(sign.id or 0, bedtime - timedelta(minutes=10))
    clock.set(bedtime - timedelta(minutes=10) + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts[-1] == "困了 我睡了"

    soon = bedtime - timedelta(minutes=4)
    promise = await app.scheduler.schedule(
        "follow_up", soon, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开", "due": soon.isoformat()},
    )
    chatter = await app.scheduler.schedule(
        "proactive", soon, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"},
    )
    calls = len(llm.calls)
    clock.set(soon + timedelta(seconds=1))
    assert clock.now() < bedtime
    while await app.scheduler.run_due_once():
        pass
    assert channel.texts[-1] == "困了 我睡了" and len(llm.calls) == calls
    assert (await memory.get_job(chatter)).status == "cancelled"
    moved = await memory.get_job(promise)
    assert moved.status == "pending" and moved.run_at > bedtime
    assert not app.rhythm.is_sleeping(moved.run_at)


async def test_a_goodnight_said_in_a_reply_is_not_said_again(
    tmp_path: Path, persona: Persona
) -> None:
    """回他的时候已经顺便说了要睡：到点那一句就不再说，也不再调一次模型。

    原来 attention 给一句软提示"可以顺口说一句就下线"，模型说了，
    到点 sign_off 看最后一次说话在半小时内，又调一次模型，再说一遍。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="哈哈"), ReplyPart(text="我睡了")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=20)
    clock.set(at)
    await send(app, "你还没睡啊", at=at, msg_id=1900)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(reply.id or 0, at + timedelta(minutes=2))
    await _deliver_one_reply(app, clock, memory)
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert persona.proactive.sign_off.reply_note in prompt
    assert "可以顺口说一句" not in prompt
    assert not [j for j in await memory.pending_jobs() if j.kind == "sign_off"]
    clock.set(bedtime - timedelta(minutes=1))
    while await app.scheduler.run_due_once():
        pass
    assert channel.texts == ["哈哈", "我睡了"] and len(llm.calls) == 1


async def test_her_own_unanswered_message_does_not_count_as_still_chatting(
    tmp_path: Path, persona: Persona
) -> None:
    """他早就不说话了，只是她自己主动说了一句没人回：睡前那句不补。

    原来"还在聊"看的是两个人谁最后说话，她自己那句也算——
    于是没人回的主动消息后面又跟一句"我睡了"，成了追发。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=85)
    clock.set(at)
    await send(app, "在干嘛", at=at, msg_id=2000)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, memory)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    her_own = sign.run_at - timedelta(minutes=15)
    await memory.add_bot_message(CONVERSATION_ID, "今天实验室好冷", her_own)
    await memory.update_conversation(CONVERSATION_ID, unanswered_initiations=1)
    calls = len(llm.calls)
    clock.set(sign.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert len(llm.calls) == calls
    assert (await memory.get_job(sign.id or 0)).status == "cancelled"


async def test_the_goodnight_knows_what_he_said_is_not_due_yet(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前那句的上下文里也有"他说了时间、还没到的"。

    他刚说完"明早九点起来做完"，她道晚安时最容易顺口问一句"那个做完没"。
    """
    from newperson.models import LedgerEntry

    goodnight = ProactivePlan(send=True, parts=[ReplyPart(text="困了 我睡了")])
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="哈哈")]), goodnight]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=30)
    clock.set(at)
    there = at.astimezone(SYDNEY) + timedelta(days=1)
    when = there.replace(hour=9, minute=0).strftime("%m-%d %H:%M")
    await memory.add_ledger_entries(
        [LedgerEntry(kind="trading", claim="明早九点跑回测", when_there=when)],
        at,
        [app.life.resolve_when_there(when, at)],
    )
    await send(app, "你还没睡啊", at=at, msg_id=2100)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    await _deliver_one_reply(app, clock, memory)
    sign = [j for j in await memory.pending_jobs() if j.kind == "sign_off"][0]
    clock.set(sign.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert "他说了时间、还没到的" in llm.calls[-1]["messages"][0]["content"]


async def test_a_shutdown_between_two_bubbles_keeps_the_first_in_her_memory(
    tmp_path: Path, persona: Persona
) -> None:
    """停机取消落在两条气泡之间：已经到他手机上的那条要进库，重启后从第二条接着发。

    CancelledError 不是 Exception，原来记"已发出的部分"那条 except 接不住它。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="第一句"), ReplyPart(text="第二句")])]
    )
    await send(app, "在吗", at=EVENING, msg_id=2200)
    real_send = channel.send

    async def send_then_shutdown(content=None, **kw):
        if channel.texts:
            raise asyncio.CancelledError
        return await real_send(content, **kw)

    channel.send = send_then_shutdown
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    with pytest.raises(asyncio.CancelledError):
        await app.scheduler.run_due_once()
    rows = await memory.db.execute("SELECT content FROM messages WHERE author_kind = 'bot'")
    assert [r[0] for r in await rows.fetchall()] == ["第一句"]

    channel.send = real_send
    await drain(app, clock)
    assert channel.texts == ["第一句", "第二句"]


async def test_the_first_message_after_a_night_apart_is_not_treated_as_a_long_chat(
    tmp_path: Path, persona: Persona
) -> None:
    """昨晚热聊过，今天他发的第一句：不按"聊久了"放慢，也不提示她收尾。

    原来库里的热聊起点清成了 None，这一次交给 plan_reply 的却还是昨晚那个时刻：
    疲劳倍率顶满 ×8，user 消息里写着"你们已经聊了一阵了，可以自然收尾"——
    一段对话刚开头她就被要求收尾。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await memory.update_conversation(
        CONVERSATION_ID, hot_session_started_at=EVENING - timedelta(days=1)
    )
    await send(app, "早", at=EVENING, msg_id=2300)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert "聊久了" not in (job.reason or "")
    assert not [h for h in job.payload.get("hints", []) if "收尾" in h]


async def test_a_window_photo_needs_a_window_photo(tmp_path: Path, persona: Persona) -> None:
    """"凌晨拍了一张窗外"只从对得上标签的照片里挑；库里只有午饭和猫就不排、不发。

    原来 photo_tags 写进了任务却没人读：候选只按时段和冷却筛，
    模型拿到"拍了一张窗外，不配字直接发"，手边却是牛肉面和猫。
    """
    from newperson.models import Photo

    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    app.media.library.photos = [
        Photo(id="lunch-001", file="a.jpg", tags=["food", "lunch"]),
        Photo(id="cat-001", file="b.jpg", tags=["cat", "home"]),
    ]
    window = next(k for k in persona.proactive.kinds if k.photo_tags and k.requires_photo)
    assert await app.life.has_photos([]) and not await app.life.has_photos(window.photo_tags)

    at = EVENING + timedelta(hours=1)
    job_id = await app.scheduler.schedule(
        "proactive", at, conversation_id=CONVERSATION_ID,
        payload={"kind": window.name, "note": window.note if hasattr(window, "note") else "",
                 "photo_tags": window.photo_tags, "requires_photo": True},
    )
    clock.set(at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert llm.calls == [] and channel.sent == []
    assert (await memory.get_job(job_id)).status == "cancelled"

    app.media.library.photos.append(Photo(id="win-001", file="c.jpg", tags=["window", "night"]))
    assert [p.id for p in await app._photo_shortlist(at, window.photo_tags)] == ["win-001"]
    assert await app.life.has_photos(window.photo_tags)
    # 唯一那张窗外照片刚发过、还在冷却里：不进当天候选，免得白占名额
    await memory.mark_photo_used("win-001", CONVERSATION_ID, clock.now(), False)
    assert not await app.life.has_photos(window.photo_tags)


async def test_her_custom_status_shows_up_on_some_days_and_not_at_midnight_sharp(
    tmp_path: Path, persona: Persona
) -> None:
    """自定义状态：三成左右的日子有，等当天日程出来才定，不在 00:00 整准点变。

    原来按日历日、第一次调用就锁：那时日程还没生成，当天锁成"没有状态"，
    实测六十天里一次都没出现过；偶尔有一条，也在午夜准点被清掉。
    """
    from newperson.discord_bot import PresenceManager
    from newperson.models import DayPlan

    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])

    async def change_presence(**_kw) -> None:
        return None

    assert persona.style.status_text_probability == 0, "默认该关着：挂的是心情原文"
    on = persona.model_copy(
        update={"style": persona.style.model_copy(update={"status_text_probability": 0.3})}
    )
    presence = PresenceManager(
        SimpleNamespace(change_presence=change_presence),
        on, app.rhythm, memory, clock, random.Random(1),
    )
    shown = 0
    for offset in range(40):
        day = EVENING.date() + timedelta(days=offset)
        wake = app.rhythm.for_day(day).wake
        clock.set(wake + timedelta(minutes=1))
        assert await presence._status_text(clock.now()) is None  # 日程还没出来
        await memory.save_day_plan(day, DayPlan(date=day.isoformat(), mood="有点困但还行"))
        clock.set(wake + timedelta(minutes=40))
        text = await presence._status_text(clock.now())
        shown += text is not None
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
        before, after = midnight - timedelta(minutes=1), midnight + timedelta(minutes=1)
        if not app.rhythm.is_sleeping(before) and not app.rhythm.is_sleeping(after):
            clock.set(before)
            at_2359 = await presence._status_text(before)
            clock.set(after)
            assert await presence._status_text(after) == at_2359, f"{day} 午夜准点变了"
    assert 4 <= shown <= 24, f"40 天里有状态的只有 {shown} 天"


async def test_his_message_still_gets_answered_after_a_long_api_outage(
    tmp_path: Path, persona: Persona
) -> None:
    """接口连着挂了好一阵：那批话照样有人回，不是三次失败就永远躺在未读里。"""
    outage = [RuntimeError("连不上接口")] * 8
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [*outage, ReplyPlan(parts=[ReplyPart(text="在")])]
    )
    await send(app, "在吗", at=EVENING, msg_id=2400)
    await drain(app, clock, hops=40)
    assert channel.texts == ["在"]
    assert not await memory.unread_messages(CONVERSATION_ID)


async def test_a_reply_that_slips_into_her_sleep_waits_until_she_wakes(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前一两分钟排的回复，被重试或重启挪进了睡眠：醒来再回。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="早")])]
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=3)
    clock.set(at)
    await send(app, "睡了没", at=at, msg_id=2500)
    reply = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    asleep = bedtime + timedelta(minutes=4)
    await memory.reschedule_job(reply.id or 0, asleep)  # 重试退避、开机打散都会这样
    clock.set(asleep + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [] and llm.calls == []
    moved = await memory.get_job(reply.id or 0)
    assert moved.status == "pending" and not app.rhythm.is_sleeping(moved.run_at)


async def test_a_goodnight_reply_stuck_until_morning_is_rethought_not_sent(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前想好了"好 我先睡了"，Discord 发不出去，拖到了她睡着：早上重新想，不照发那句。"""
    app, channel, llm, clock, memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="好 我先睡了")]), ReplyPlan(parts=[ReplyPart(text="早")])],
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=10)
    clock.set(at)
    await send(app, "你睡了没", at=at, msg_id=2650)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    real_send = channel.send

    async def discord_down(*_a, **_kw):
        raise RuntimeError("503 Service Unavailable")

    channel.send = discord_down
    await memory.reschedule_job(job.id or 0, bedtime - timedelta(minutes=5))
    clock.set(bedtime - timedelta(minutes=5) + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert (await memory.get_job(job.id or 0)).progress.get("goodnight"), "前提：想好的是睡前那句"

    channel.send = real_send
    await memory.reschedule_job(job.id or 0, bedtime + timedelta(minutes=3))
    clock.set(bedtime + timedelta(minutes=3, seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [], "她睡着了还在发"
    await drain(app, clock, hops=10)
    assert channel.texts == ["早"]
    assert persona.proactive.sign_off.reply_note not in llm.calls[-1]["messages"][0]["content"]


async def test_a_reply_planned_long_ago_is_not_sent_as_is(
    tmp_path: Path, persona: Persona
) -> None:
    """想好的回复一条都没发出去（Discord 断了），过了很久才恢复：放回未读、按人的节奏重排。

    原来照原样发：几个小时前的回复，而且 !np retry 放回来的几条会在一两秒内连着冒出来。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path,
        persona,
        [ReplyPlan(parts=[ReplyPart(text="旧的")]), ReplyPlan(parts=[ReplyPart(text="新的")])],
    )
    await send(app, "在吗", at=EVENING, msg_id=2600)
    real_send = channel.send

    async def discord_down(*_a, **_kw):
        raise RuntimeError("503 Service Unavailable")

    channel.send = discord_down
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert not await memory.unread_messages(CONVERSATION_ID), "前提：这批已经标成已读"

    channel.send = real_send
    back = clock.now() + timedelta(minutes=50)
    await memory.reschedule_job(job.id or 0, back)
    clock.set(back + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [], "放了五十分钟的旧回复被照原样发了出去"
    assert await memory.unread_messages(CONVERSATION_ID)
    await drain(app, clock, hops=10)
    assert channel.texts == ["新的"] and len(llm.calls) == 2


async def test_a_dropped_reply_is_picked_up_again_by_her_next_proactive_moment(
    tmp_path: Path, persona: Persona
) -> None:
    """回复重试到放弃了，他的话还挂在未读：她下一次想主动开口时，先把回复补排上。"""
    app, _channel, _llm, clock, memory = await build(tmp_path, persona, [])
    await send(app, "在吗", at=EVENING, msg_id=2700)
    dead = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.set_job_status(dead.id or 0, "failed", "试了三次", at=EVENING)
    later_on = EVENING + timedelta(hours=1)
    await app.scheduler.schedule(
        "proactive", later_on, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    clock.set(later_on + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert await memory.pending_jobs("reply", CONVERSATION_ID), "没人再去回他那句"


async def test_how_long_he_waited_is_counted_when_she_actually_replies(
    tmp_path: Path, persona: Persona
) -> None:
    """"他这条是多久前发的"按生成那一刻算，不按排期时写下的那个数。"""
    app, _channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING - timedelta(hours=2), msg_id=2800)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    stale = {**job.payload, "hints": ["他这条消息是 20 分钟 前发的。**别解释这段时间你在干嘛**"]}
    await app.scheduler.reschedule(job.id or 0, EVENING + timedelta(hours=1), stale)
    clock.set(EVENING + timedelta(hours=1, seconds=1))
    await app.scheduler.run_due_once()
    prompt = llm.calls[-1]["messages"][0]["content"]
    assert "20 分钟 前发的" not in prompt
    assert "3.0 小时 前发的" in prompt


async def test_a_failed_start_is_retried_without_waiting_for_a_reconnect(
    tmp_path: Path, persona: Persona
) -> None:
    """启动半途失败（比如盘满）：过一会儿自己再试，不等下一次重新 IDENTIFY。

    原来异常被 discord.py 吞成一行日志：进程活着、网关连着，调度循环却没起来，
    要等 Discord 让会话失效、重新 IDENTIFY 才会再试——可能是好几天以后。
    """
    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    real_open = memory.open
    failures = {"left": 1}

    async def open_once_broken() -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("database or disk is full")
        await real_open()

    memory.open = open_once_broken
    client = NewPersonClient(app)
    await client.on_ready()
    assert not app._started
    retry = client._start_retry
    assert retry is not None
    await asyncio.wait_for(retry, timeout=5)
    assert app._started, "失败之后没有自己再试"
    assert [t for t in app._tasks if t.get_name() == "scheduler"]
    for task in list(app._tasks):
        task.cancel()


async def test_she_keeps_trying_to_log_in_while_discord_is_unreachable() -> None:
    """开机时连不上 Discord：进程里退避重试，不是一秒就退出、让 systemd 熔断。"""
    import aiohttp

    from newperson.discord_bot import login_with_retry

    waits: list[float] = []
    tries = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    class Unreachable:
        async def login(self, _token: str) -> None:
            tries["n"] += 1
            if tries["n"] <= 6:
                raise aiohttp.ClientConnectionError("Cannot connect to host discord.com:443")

    await login_with_retry(Unreachable(), "x", sleep=fake_sleep)
    assert tries["n"] == 7
    assert waits[0] == 30 and waits == sorted(waits) and max(waits) <= 600


async def test_a_wrong_token_is_not_retried_forever() -> None:
    """token 错了等多久都不会好，照旧直接退出。"""
    import discord

    from newperson.discord_bot import login_with_retry

    class WrongToken:
        async def login(self, _token: str) -> None:
            raise discord.LoginFailure("Improper token has been passed.")

    async def no_sleep(_s: float) -> None:
        raise AssertionError("不该重试")

    with pytest.raises(discord.LoginFailure):
        await login_with_retry(WrongToken(), "x", sleep=no_sleep)


async def test_catching_up_does_not_download_his_pictures_again(
    tmp_path: Path, persona: Persona
) -> None:
    """补抓重翻到库里已经有的消息：不再把它的图下载一遍。

    游标只在补抓时前进，每次重连都会重翻上次重连以来的整段；
    原来每张图都重下一次（最多 5MB、等 20 秒），新文件还逃过了两周清理。
    """
    from newperson.discord_bot import CATCHUP_CURSOR, CATCHUP_SEEN

    app, _channel, _llm, _clock, memory = await build(tmp_path, persona, [])
    await send(app, "停机前", at=EVENING, msg_id=100)
    await memory.add_user_message(
        IncomingMessage(
            conversation_id=CONVERSATION_ID, discord_message_id=101, author_id=42,
            author_name="Leo", content="看这个", created_at=EVENING + timedelta(minutes=1),
        )
    )
    with_picture = fake_incoming(101, "看这个", EVENING + timedelta(minutes=1))
    with_picture.attachments = [SimpleNamespace(
        url="https://cdn/x.png", filename="x.png", content_type="image/png", size=10,
    )]
    wire_inbound(app, FakeHistoryChannel([with_picture]))
    await memory.kv_set(CATCHUP_SEEN, "999")
    await memory.kv_set(f"{CATCHUP_CURSOR}999", "100")
    downloads: list[int] = []

    async def counting(message):
        downloads.append(message.id)
        return []

    app._download_images = counting
    await app.catch_up()
    assert downloads == [], "库里已经有的那条，图又下了一遍"


def _owner_ctx(app: App):
    from newperson import owner

    return owner.OwnerContext(
        memory=app.memory,
        rhythm=app.rhythm,
        scheduler=app.scheduler,
        life=app.life,
        conversation_id=CONVERSATION_ID,
        now=app.clock.now(),
    )


async def _two_bubbles_second_fails(tmp_path: Path, persona: Persona, at: datetime, script: list):
    """回复两条气泡，第一条发出去、第二条 Discord 报错。返回 (app, channel, llm, clock, memory, job, restore)。"""
    app, channel, llm, clock, memory = await build(tmp_path, persona, script)
    clock.set(at)
    await send(app, "在吗", at=at, msg_id=2900)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    real_send = channel.send

    async def second_fails(content=None, **kw):
        if channel.texts:
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = second_fails
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()

    def restore() -> None:
        channel.send = real_send

    return app, channel, llm, clock, memory, job, restore


async def test_the_second_half_of_an_old_reply_waits_until_she_wakes(
    tmp_path: Path, persona: Persona
) -> None:
    """发出第一句之后 Discord 挂了，重试一路拖进她的睡眠：后半句等她醒来再发，不在凌晨冒出来。"""
    app, channel, _llm, clock, memory, job, restore = await _two_bubbles_second_fails(
        tmp_path, persona, EVENING, [ReplyPlan(parts=[ReplyPart(text="第一句"), ReplyPart(text="第二句")])]
    )
    assert channel.texts == ["第一句"]
    bedtime = app.rhythm.next_sleep_after(EVENING)
    restore()
    asleep = bedtime + timedelta(hours=1)
    await memory.reschedule_job(job.id or 0, asleep)
    clock.set(asleep + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["第一句"], "她睡着了，后半句还是发了出去"
    moved = await memory.get_job(job.id or 0)
    assert moved.status == "pending" and not app.rhythm.is_sleeping(moved.run_at)


async def test_np_now_on_a_stale_reply_answers_right_away(tmp_path: Path, persona: Persona) -> None:
    """放久了的旧回复，你 !np now 催了：当场重想、马上发，不是说"马上发"却排到十几分钟后。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona,
        [ReplyPlan(parts=[ReplyPart(text="旧的")]), ReplyPlan(parts=[ReplyPart(text="新的")])],
    )
    await send(app, "在吗", at=EVENING, msg_id=3000)
    real_send = channel.send

    async def discord_down(*_a, **_kw):
        raise RuntimeError("503 Service Unavailable")

    channel.send = discord_down
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    channel.send = real_send

    clock.set(clock.now() + timedelta(minutes=50))
    await owner.handle("!np now", _owner_ctx(app))
    await app.scheduler.run_due_once()
    assert channel.texts == ["新的"]


async def test_an_old_np_now_does_not_carry_her_retries_into_sleep(
    tmp_path: Path, persona: Persona
) -> None:
    """!np now 的记号只管敲命令那一下。几个小时后的重试落在她睡着的时候，照样推到醒来。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="嗯")])]
    )
    await send(app, "在吗", at=EVENING, msg_id=3100)
    await owner.handle("!np now", _owner_ctx(app))
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    asleep = app.rhythm.next_sleep_after(EVENING) + timedelta(hours=1)
    await memory.reschedule_job(job.id or 0, asleep)  # 比如接口挂着、一路重试到半夜
    clock.set(asleep + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [] and llm.calls == []


async def test_her_opening_after_a_quiet_spell_starts_a_fresh_chat(
    tmp_path: Path, persona: Persona
) -> None:
    """冷了之后她先开口、他一分钟就回：这一段从头算，不拿昨晚那段热聊的起点算"聊了多久"。"""
    app, _channel, _llm, clock, memory = await build(
        tmp_path, persona, [ProactivePlan(send=True, parts=[ReplyPart(text="刚下课 好饿")])]
    )
    yesterday = EVENING - timedelta(days=1)
    await memory.update_conversation(
        CONVERSATION_ID,
        hot_session_started_at=yesterday,
        last_user_message_at=yesterday + timedelta(hours=1),
        last_bot_message_at=yesterday + timedelta(hours=1),
    )
    job_id = await app.scheduler.schedule(
        "proactive", EVENING, conversation_id=CONVERSATION_ID, payload={"kind": "own_life"}
    )
    clock.set(EVENING + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert (await memory.get_job(job_id)).status == "done"
    assert (await memory.get_conversation(CONVERSATION_ID)).hot_session_started_at is None

    reply_at = clock.now() + timedelta(minutes=1)
    clock.set(reply_at)
    await send(app, "哈哈 吃啥", at=reply_at, msg_id=3200)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    assert "聊久了" not in (job.reason or "")
    assert not [h for h in job.payload.get("hints", []) if "收尾" in h]


async def test_stopping_the_service_mid_reply_keeps_what_she_already_said(
    tmp_path: Path, persona: Persona
) -> None:
    """重启服务时正在发第二条：第一条要进她的库，任务回到 pending，再关库。

    真实的收工是所有任务一起被取消、同时 close() 在跑。原来 close() 第一时间关库，
    投递那边护着写库的那一步全部失败——她自己的历史少了已经到他手机上的那一句。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona, [ReplyPlan(parts=[ReplyPart(text="第一句"), ReplyPart(text="第二句")])]
    )
    await send(app, "在吗", at=EVENING, msg_id=3300)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    real_send = channel.send
    blocked = asyncio.Event()

    async def hang_on_second(content=None, **kw):
        if channel.texts:
            blocked.set()
            await asyncio.Event().wait()
        return await real_send(content, **kw)

    channel.send = hang_on_second
    clock.set(job.run_at + timedelta(seconds=1))
    delivering = app.spawn(app.scheduler.run_due_once(), "scheduler")
    await asyncio.wait_for(blocked.wait(), timeout=5)

    client = NewPersonClient(app)
    delivering.cancel()  # Runner 收工时一起取消
    await client.close()

    again = Memory(tmp_path / "e2e.db")
    await again.open()
    _OPEN.append(again)
    rows = await again.db.execute("SELECT content FROM messages WHERE author_kind = 'bot'")
    assert [r[0] for r in await rows.fetchall()] == ["第一句"]
    assert (await again.get_job(job.id or 0)).status == "pending"


async def test_a_goodnight_fully_sent_before_a_restart_still_counts(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前那条回复最后一句已经发出去了，还没收尾就重启：续发时没有要发的，也算说过了。

    改错字要等 4–20 秒，停机落在这段里就是这样。原来续发时 sent_texts 是空的，
    "今晚说过了"不记，到点 sign_off 又说一遍要睡了。
    """
    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=18)
    clock.set(at)
    await send(app, "你还不睡", at=at, msg_id=3400)
    await app.life.maybe_schedule_sign_off(CONVERSATION_ID)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    unread = await memory.unread_messages(CONVERSATION_ID)
    await memory.mark_read([m.id for m in unread], at)
    plan = ReplyPlan(parts=[ReplyPart(text="哈哈我也困了 先睡了")])
    await memory.save_job_progress(
        job.id or 0,
        {"plan": plan.model_dump(mode="json"), "sent_parts": 1,
         "planned_at": at.isoformat(), "goodnight": bedtime.isoformat()},
        max(m.id for m in unread),
    )
    await memory.reschedule_job(job.id or 0, at + timedelta(minutes=3))
    clock.set(at + timedelta(minutes=3, seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == [] and llm.calls == []
    assert not [j for j in await memory.pending_jobs() if j.kind == "sign_off"], "到点还会再道一次晚安"


async def test_a_promise_half_delivered_is_not_repeated_a_dozen_times(
    tmp_path: Path, persona: Persona
) -> None:
    """答应他的事发到一半断了：记住断点，重试只接着发剩下的，不从头重新生成、重发第一句。

    follow_up 要重试十几个小时；第二条一直发不出去的话，
    同一件事原来会换着措辞说十几遍。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona,
        [ProactivePlan(send=True, parts=[ReplyPart(text="我查了"), ReplyPart(text="那家周末不开")])] * 3,
    )
    real_send = channel.send

    async def second_fails(content=None, **kw):
        if channel.texts:
            raise RuntimeError("413 Payload Too Large")
        return await real_send(content, **kw)

    channel.send = second_fails
    at = EVENING + timedelta(hours=1)
    job_id = await app.scheduler.schedule(
        "follow_up", at, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开", "due": at.isoformat()},
    )
    clock.set(at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    await drain(app, clock, hops=10)
    assert channel.texts == ["我查了"] and len(llm.calls) == 1
    job = await memory.get_job(job_id)
    assert job.progress.get("sent_parts") == 1, "没记住发到哪了"
    rows = await memory.db.execute("SELECT content FROM messages WHERE author_kind = 'bot'")
    assert [r[0] for r in await rows.fetchall()] == ["我查了"]


async def test_a_promise_interrupted_by_a_blip_is_finished_not_abandoned(
    tmp_path: Path, persona: Persona
) -> None:
    """答应他的事第二条碰上一次 503、之后恢复：接着把后半句发完，模型只调一次。

    上一版把"发出一个字"就当做完：后半句——往往就是答应他的那件事——永远不发。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona,
        [ProactivePlan(send=True, parts=[ReplyPart(text="诶我查了"), ReplyPart(text="那家周末不开")])],
    )
    real_send = channel.send
    blips = {"left": 1}

    async def one_blip(content=None, **kw):
        if channel.texts and blips["left"]:
            blips["left"] -= 1
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = one_blip
    at = EVENING + timedelta(hours=1)
    job_id = await app.scheduler.schedule(
        "follow_up", at, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开", "due": at.isoformat()},
    )
    clock.set(at + timedelta(seconds=1))
    await drain(app, clock, hops=10)
    assert channel.texts == ["诶我查了", "那家周末不开"] and len(llm.calls) == 1
    assert (await memory.get_job(job_id)).status == "done"


async def test_a_promise_is_kept_even_after_an_unanswered_hello(
    tmp_path: Path, persona: Persona
) -> None:
    """她今天主动说了句闲话、他没回：之后到点的"答应他的事"照样说，不被"今天别再追"作废。"""
    app, channel, llm, clock, memory = await build(
        tmp_path, persona, [ProactivePlan(send=True, parts=[ReplyPart(text="查了 那家周末开")])]
    )
    day = app.rhythm.local_date(EVENING)
    await memory.update_conversation(
        CONVERSATION_ID, unanswered_initiations=1, last_initiation_date=day,
        last_user_message_at=EVENING - timedelta(hours=4),
    )
    at = EVENING + timedelta(hours=1)
    job_id = await app.scheduler.schedule(
        "follow_up", at, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开", "due": at.isoformat()},
    )
    clock.set(at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["查了 那家周末开"]
    assert (await memory.get_job(job_id)).status == "done"


async def test_a_promise_is_not_dropped_when_the_rewrite_call_hiccups(
    tmp_path: Path, persona: Persona
) -> None:
    """答应他的事第一稿带禁语、重写那次接口抖了一下：过一阵再试，不当成"她不想说"作废。"""
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "http://x")
    hiccup = anthropic.InternalServerError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    app, channel, llm, clock, memory = await build(
        tmp_path, persona,
        [ProactivePlan(send=True, parts=[ReplyPart(text="查了 那家周末不开 你早点休息")]), hiccup],
    )
    at = EVENING + timedelta(hours=1)
    job_id = await app.scheduler.schedule(
        "follow_up", at, conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开", "due": at.isoformat()},
    )
    clock.set(at + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == []
    assert (await memory.get_job(job_id)).status == "pending", "网络抖一下就把答应他的事作废了"


async def test_a_picture_only_reply_that_cannot_be_sent_is_rethought(
    tmp_path: Path, persona: Persona
) -> None:
    """他说"拍张看看"，她只回一张图，图又发不出去：放回未读重想一次，不已读不回。"""
    from newperson.models import Photo, PhotoRequest

    picture_only = ReplyPlan(parts=[ReplyPart(text="{photo}")], photo_request=PhotoRequest(photo_id="big"))
    words = ReplyPlan(parts=[ReplyPart(text="拍不了 在外面")])
    app, channel, llm, clock, memory = await build(tmp_path, persona, [picture_only, words])
    app.media.library.photos = [Photo(id="big", file="big.jpg", tags=["sky"])]

    class TooLarge(Exception):
        status = 413

    real_send = channel.send

    async def too_large(content=None, *, file=None, reference=None):
        if file:
            raise TooLarge("413 Payload Too Large")
        return await real_send(content, reference=reference)

    channel.send = too_large
    await send(app, "拍张看看？", at=EVENING, msg_id=4100)
    await drain(app, clock, hops=10)
    assert channel.texts == ["拍不了 在外面"]
    assert not await memory.unread_messages(CONVERSATION_ID)


async def test_a_rethought_reply_is_stamped_with_when_it_was_thought(
    tmp_path: Path, persona: Persona
) -> None:
    """!np now 当场重想的回复，进度里记的是这次想好的时刻，不是旧 plan 的。

    原来发出第一条后写回的是几个小时前那份的时刻：第二条一抖，
    剩下的半句就被当成"很久以前的"推到第二天。
    """
    app, channel, llm, clock, memory = await build(
        tmp_path, persona,
        [ReplyPlan(parts=[ReplyPart(text="旧的")]),
         ReplyPlan(parts=[ReplyPart(text="新的"), ReplyPart(text="第二句")])],
    )
    await send(app, "在吗", at=EVENING, msg_id=3500)
    real_send = channel.send

    async def discord_down(*_a, **_kw):
        raise RuntimeError("503 Service Unavailable")

    channel.send = discord_down
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    clock.set(job.run_at + timedelta(seconds=1))
    await app.scheduler.run_due_once()

    async def second_fails(content=None, **kw):
        if channel.texts:
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = second_fails
    clock.set(clock.now() + timedelta(minutes=50))
    await owner.handle("!np now", _owner_ctx(app))
    await app.scheduler.run_due_once()
    assert channel.texts == ["新的"]
    progress = (await memory.get_job(job.id or 0)).progress
    assert progress["sent_parts"] == 1
    assert clock.now() - datetime.fromisoformat(progress["planned_at"]) < timedelta(minutes=5)


async def test_the_unsent_half_of_a_goodnight_is_dropped_the_next_morning(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前那条回复发了一半卡住，拖到第二天醒来：剩下的"困了 先睡了"不补发。

    醒来第一句是"我先睡了"最像程序。前面说出口的那句算数。
    """
    app, channel, _llm, clock, memory = await build(
        tmp_path, persona,
        [ReplyPlan(parts=[ReplyPart(text="哈哈对"), ReplyPart(text="困了 先睡了")])],
    )
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=15)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=3600)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(job.id or 0, at + timedelta(minutes=1))
    real_send = channel.send

    async def second_fails(content=None, **kw):
        if channel.texts:
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = second_fails
    clock.set(at + timedelta(minutes=1, seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["哈哈对"]
    assert (await memory.get_job(job.id or 0)).progress.get("goodnight")

    channel.send = real_send
    morning = app.rhythm.next_wake_after(bedtime) + timedelta(hours=1)
    await memory.reschedule_job(job.id or 0, morning)
    clock.set(morning + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == ["哈哈对"], "醒来第一句是'困了 先睡了'"
    assert (await memory.get_job(job.id or 0)).status == "done"


async def test_photo_tags_are_matched_regardless_of_case(tmp_path: Path, persona: Persona) -> None:
    """index.yaml 是手写的，"Window""Boston"顺手就大写了：照样对得上。"""
    from newperson.models import Photo

    app, _channel, _llm, _clock, _memory = await build(tmp_path, persona, [])
    app.media.library.photos = [Photo(id="win-001", file="c.jpg", tags=["Window", "Boston"])]
    window = next(k for k in persona.proactive.kinds if k.photo_tags and k.requires_photo)
    assert await app.life.has_photos(window.photo_tags)


async def test_resuming_a_reply_keeps_the_picture_it_started_with(
    tmp_path: Path, persona: Persona
) -> None:
    """图发出去了、后面一句抖了一下：续发用第一次挑中的那张，后面的话一句不少。

    原来续发时重新挑图：刚发过的那张进了冷却、挑不到，只放图的那格被丢掉，
    下标整体前移——"你看"永远没发，他看到一张图后直接是"好看吧"。
    """
    from newperson.models import Photo, PhotoRequest

    plan = ReplyPlan(
        parts=[ReplyPart(text="{photo}"), ReplyPart(text="你看"), ReplyPart(text="好看吧")],
        photo_request=PhotoRequest(photo_id="p1"),
    )
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    app.media.library.photos = [Photo(id="p1", file="p.jpg", tags=["food"])]
    await send(app, "晚饭吃啥了", at=EVENING, msg_id=3700)
    real_send = channel.send
    fails = {"left": 1}

    async def flaky(content=None, **kw):
        if content == "你看" and fails["left"]:
            fails["left"] -= 1
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = flaky
    await drain(app, clock, hops=10)
    assert channel.sent == [(None, True), ("你看", False), ("好看吧", False)]


async def test_a_promise_folded_into_a_dropped_goodnight_half_is_still_kept(
    tmp_path: Path, persona: Persona
) -> None:
    """并进睡前那条回复的承诺，在醒来被裁掉的后半句里：不算说过，它到点自己说。"""
    app, channel, llm, clock, memory = await build(tmp_path, persona, [])
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=15)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=3800)
    promise = await app.scheduler.schedule(
        "follow_up", bedtime + timedelta(hours=12), conversation_id=CONVERSATION_ID,
        payload={"kind": "follow_up", "note": "告诉他那家店周末开不开"},
    )
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    unread = await memory.unread_messages(CONVERSATION_ID)
    await memory.mark_read([m.id for m in unread], at)
    plan = ReplyPlan(parts=[ReplyPart(text="哈哈对"), ReplyPart(text="那家周末不开 困了 先睡了")])
    await memory.save_job_progress(
        job.id or 0,
        {"plan": plan.model_dump(mode="json"), "sent_parts": 1, "planned_at": at.isoformat(),
         "goodnight": bedtime.isoformat()},
        max(m.id for m in unread),
    )
    await app.scheduler.reschedule(
        job.id or 0, at, {**job.payload, "riding_follow_up": promise}
    )
    morning = app.rhythm.next_wake_after(bedtime) + timedelta(hours=1)
    await memory.reschedule_job(job.id or 0, morning)
    clock.set(morning + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.texts == []
    assert (await memory.get_job(promise)).status == "pending", "答应他的事没说出口就作废了"


async def test_the_picture_of_a_dropped_goodnight_half_is_not_sent_the_next_morning(
    tmp_path: Path, persona: Persona
) -> None:
    """睡前那条回复带图、后半截醒来被裁掉：那张图也不补，醒来第一眼不是单独一张图。"""
    from newperson.models import Photo, PhotoRequest

    plan = ReplyPlan(
        parts=[ReplyPart(text="哈哈对"), ReplyPart(text="困了 先睡了")],
        photo_request=PhotoRequest(photo_id="p1"),
    )
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    app.media.library.photos = [Photo(id="p1", file="p.jpg", tags=["sky"])]
    bedtime = await _bedtime(app, EVENING)
    at = bedtime - timedelta(minutes=15)
    clock.set(at)
    await send(app, "今天好累", at=at, msg_id=3900)
    job = (await memory.pending_jobs("reply", CONVERSATION_ID))[0]
    await memory.reschedule_job(job.id or 0, at + timedelta(minutes=1))
    real_send = channel.send

    async def second_fails(content=None, **kw):
        if channel.sent:
            raise RuntimeError("503 Service Unavailable")
        return await real_send(content, **kw)

    channel.send = second_fails
    clock.set(at + timedelta(minutes=1, seconds=1))
    await app.scheduler.run_due_once()
    assert channel.sent == [("哈哈对", False)]

    channel.send = real_send
    morning = app.rhythm.next_wake_after(bedtime) + timedelta(hours=1)
    await memory.reschedule_job(job.id or 0, morning)
    clock.set(morning + timedelta(seconds=1))
    await app.scheduler.run_due_once()
    assert channel.sent == [("哈哈对", False)], "醒来第一眼单独冒出来一张图"


async def test_a_picture_that_cannot_be_sent_goes_into_cooldown(
    tmp_path: Path, persona: Persona
) -> None:
    """发不出去的那张图也进冷却：不然下次又被挑中，每次都是"你看"后面没图。"""
    from newperson.models import Photo, PhotoRequest

    plan = ReplyPlan(parts=[ReplyPart(text="你看{photo}")], photo_request=PhotoRequest(photo_id="big"))
    app, channel, _llm, clock, memory = await build(tmp_path, persona, [plan])
    app.media.library.photos = [Photo(id="big", file="big.jpg", tags=["sky"])]

    class TooLarge(Exception):
        status = 413

    real_send = channel.send

    async def too_large(content=None, *, file=None, reference=None):
        if file:
            raise TooLarge("413 Payload Too Large")
        return await real_send(content, reference=reference)

    channel.send = too_large
    await send(app, "今天天气怎么样", at=EVENING, msg_id=4000)
    await drain(app, clock, hops=5)
    assert channel.texts == ["你看"]
    assert "big" in await memory.recently_used_photo_ids(clock.now())


def test_every_job_kind_has_a_handler_when_she_starts() -> None:
    """每一种任务，App.start 里都得有人接。

    sign_off 就是这么死的：类型定义了、调度器认它、体检数它，
    可是没人注册处理它——排上了也只会以"没有 handler"作废，一年都没说过一句。
    """
    import re
    import typing

    from newperson.models import JobKind

    source = (Path(__file__).resolve().parent.parent / "newperson" / "discord_bot.py").read_text(
        encoding="utf-8"
    )
    registered = set(re.findall(r'register\("(\w+)"', source))
    missing = set(typing.get_args(JobKind)) - registered
    assert not missing, f"没人接的任务：{missing}"
