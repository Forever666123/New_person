"""端到端：一条消息从进来到发出去，走完整条链路。

不连 Discord，不连模型。假 channel 记录发了什么，假 client 按脚本返回。
这个测试的价值在于抓组装错误：单个模块都对，串起来不对。
"""

from __future__ import annotations

import contextlib
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
    app._default_channel = channel
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
