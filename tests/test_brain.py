"""大脑的测试。用假 client，不联网。

重点：失败绝不能被对方看见、稳定层要能命中缓存、风格不过关会重写。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import httpx2 as httpx
import pytest

from newperson.brain import Brain, ProactiveRequest, ReplyRequest
from newperson.config import Settings
from newperson.memory import Memory
from newperson.models import ProactivePlan, ReplyPart, ReplyPlan, StoredMessage
from newperson.persona import Persona
from newperson.prompts import build_system

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)
TODAY = NOW.date()


def usage(**kw):
    return SimpleNamespace(
        input_tokens=kw.get("input_tokens", 100),
        cache_read_input_tokens=kw.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=kw.get("cache_creation_input_tokens", 0),
        output_tokens=kw.get("output_tokens", 20),
    )


class FakeMessages:
    """记下每次请求，按脚本返回结果。"""

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
            usage=usage(),
            stop_reason="end_turn",
        )


def fake_client(*script) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(list(script)))


def settings(tmp_path: Path, **kw) -> Settings:
    return Settings(db_path=tmp_path / "b.db", **kw)


def reply_request(**kw) -> ReplyRequest:
    base = dict(
        situation="现在是 10-12 周一 20:00。",
        summary="",
        owner_facts=[],
        self_facts=[],
        ledger=[],
        mode_instruction="",
        recent=[],
        unread=[
            StoredMessage(
                id=1,
                conversation_id="owner",
                author_kind="user",
                author_id=42,
                content="在吗",
                created_at=NOW,
            )
        ],
        hints=[],
        photos=[],
    )
    base.update(kw)
    return ReplyRequest(**base)


@pytest.fixture
async def memory(tmp_path: Path):
    mem = Memory(tmp_path / "b.db")
    await mem.open()
    yield mem
    await mem.close()


# -- 稳定层与缓存 ------------------------------------------------------------


def test_the_system_prompt_never_changes(persona: Persona) -> None:
    """稳定层里有时间就永远命中不了缓存，这个断言守着这件事。"""
    assert build_system(persona) == build_system(persona)


def test_the_system_prompt_carries_no_clock(persona: Persona) -> None:
    text = build_system(persona)
    for token in ("2026", "2027", "现在是", ":00"):
        assert token not in text, f"稳定层里混进了 {token}"


def test_the_system_prompt_is_marked_for_caching(persona: Persona, tmp_path: Path) -> None:
    brain = Brain(fake_client(), settings(tmp_path), persona)
    blocks = brain.system_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_who_she_is_reaches_the_model(persona: Persona) -> None:
    text = build_system(persona)
    assert persona.name in text
    assert "Northeastern" in text
    assert "Leo" in text


# -- 正常路径 ----------------------------------------------------------------


async def test_a_reply_comes_back(persona: Persona, tmp_path: Path, memory: Memory) -> None:
    plan = ReplyPlan(parts=[ReplyPart(text="在")], inner_note="他又在赶工")
    brain = Brain(fake_client(plan), settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert [p.text for p in got.parts] == ["在"]


async def test_the_request_carries_effort_and_model(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path, model="claude-opus-5", effort="medium"), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"] == {"effort": "medium"}
    assert "thinking" not in call, "Opus 5 默认就是自适应思考，不用显式传"


async def test_an_image_he_sent_is_passed_along(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """他发图片她要真的看得到，不能只看到 [图片] 两个字。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="哪拍的")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(images=[("image/png", b"\x89PNG" * 20)]), TODAY)
    content = client.messages.calls[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image"


async def test_an_oversized_image_is_skipped(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(images=[("image/png", b"x" * (6 * 1024 * 1024))]), TODAY)
    assert isinstance(client.messages.calls[0]["messages"][0]["content"], str)


# -- 失败绝不被对方看见 ------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://x")), body=None
        ),
        anthropic.APIStatusError(
            "boom", response=httpx.Response(503, request=httpx.Request("POST", "http://x")), body=None
        ),
        anthropic.APIConnectionError(request=httpx.Request("POST", "http://x")),
        RuntimeError("完全没想到的错误"),
    ],
)
async def test_any_failure_becomes_silence(
    persona: Persona, tmp_path: Path, memory: Memory, error: Exception
) -> None:
    """调不通就当没看手机，让调度器过一阵重试。绝不发错误消息给对方。"""
    brain = Brain(fake_client(error), settings(tmp_path), persona, memory)
    assert await brain.generate_reply(reply_request(), TODAY) is None


async def test_a_refusal_is_treated_as_no_reply(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    class Refusing(FakeMessages):
        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed_output=None, usage=usage(), stop_reason="refusal")

    client = SimpleNamespace(messages=Refusing([]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    assert await brain.generate_reply(reply_request(), TODAY) is None


async def test_the_last_error_is_recorded_for_the_owner(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """`!np status` 要能看到最近一次出了什么问题。"""
    error = anthropic.APIStatusError(
        "boom", response=httpx.Response(500, request=httpx.Request("POST", "http://x")), body=None
    )
    brain = Brain(fake_client(error), settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert await memory.kv_get("last_api_error") == "500"


# -- 花钱的上限 --------------------------------------------------------------


async def test_usage_is_recorded(persona: Persona, tmp_path: Path, memory: Memory) -> None:
    brain = Brain(fake_client(ReplyPlan(parts=[])), settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    used = await memory.usage_for(TODAY)
    assert used["calls"] == 1
    assert used["estimated_usd"] > 0


async def test_the_daily_cap_stops_further_calls(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """跑一个月才发现账单太高就晚了。到上限就当今天没怎么看手机。"""
    client = fake_client(*[ReplyPlan(parts=[ReplyPart(text="嗯")])] * 5)
    brain = Brain(client, settings(tmp_path, max_calls_per_day=2), persona, memory)
    for _ in range(4):
        await brain.generate_reply(reply_request(), TODAY)
    assert len(client.messages.calls) == 2


# -- 风格把关 ----------------------------------------------------------------


async def test_a_banned_phrase_triggers_a_rewrite(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """她不说加油。模型说了就让它重写一次。"""
    bad = ReplyPlan(parts=[ReplyPart(text="加油")])
    good = ReplyPlan(parts=[ReplyPart(text="那现在怎么办")])
    client = fake_client(bad, good)
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert [p.text for p in got.parts] == ["那现在怎么办"]
    assert len(client.messages.calls) == 2


async def test_a_clean_reply_is_not_rewritten(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="知道了")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert len(client.messages.calls) == 1


async def test_it_gives_up_after_one_rewrite(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """重写只给一次机会，卡在这里既费钱又会让她很久不回。"""
    bad = ReplyPlan(parts=[ReplyPart(text="加油")])
    client = fake_client(bad, bad, bad)
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got is not None
    assert len(client.messages.calls) == 2


async def test_trailing_periods_are_stripped_without_a_rewrite(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """句号能机械修掉，不用惊动模型。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="知道了。")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got.parts[0].text == "知道了"
    assert len(client.messages.calls) == 1


# -- 主动消息 ----------------------------------------------------------------


async def test_she_can_decide_not_to_say_anything(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """觉得没什么可说的就不说，这很正常。"""
    client = fake_client(ProactivePlan(send=False))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_proactive(
        ProactiveRequest(
            situation="",
            trigger_note="想起他说过的事",
            summary="",
            owner_facts=[],
            self_facts=[],
            recent=[],
            hours_since_last_exchange=30.0,
            unanswered_initiations=0,
            photos=[],
        ),
        TODAY,
    )
    assert got.send is False


async def test_haiku_does_not_get_an_effort_parameter(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """Haiku 4.5 不接受 effort，传了直接 400，她一句话都发不出来。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path, model="claude-haiku-4-5"), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert "output_config" not in client.messages.calls[0]


async def test_an_unknown_model_plays_it_safe(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """不认识的模型宁可少传一个参数，也别让她连不上。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path, model="something-new"), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert "output_config" not in client.messages.calls[0]


async def test_the_day_plan_can_run_on_a_cheaper_model(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """日程和记忆整理不面向对话，只要格式对就行，用便宜的那档能省不少。"""
    from newperson.brain import DayPlanRequest
    from newperson.models import DayPlan

    client = fake_client(DayPlan(date="2026-10-12", mood="还行"))
    brain = Brain(
        client,
        settings(tmp_path, model="claude-sonnet-5", utility_model_override="claude-haiku-4-5"),
        persona,
        memory,
    )
    await brain.generate_day_plan(
        DayPlanRequest(
            now=NOW,
            state_line="",
            mood_notes=[],
            wake_at=NOW,
            sleep_at=NOW,
            classes=[],
            yesterday=None,
            summary="",
        ),
        TODAY,
    )
    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


async def test_replies_stay_on_the_main_model(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """回复是她像不像人的关键，不能被降级。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(
        client,
        settings(tmp_path, model="claude-sonnet-5", utility_model_override="claude-haiku-4-5"),
        persona,
        memory,
    )
    await brain.generate_reply(reply_request(), TODAY)
    assert client.messages.calls[0]["model"] == "claude-sonnet-5"
