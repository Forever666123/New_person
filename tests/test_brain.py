"""大脑的测试。用假 client，不联网。

重点：失败绝不能被对方看见、稳定层要能命中缓存、风格不过关会重写。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import httpx2 as httpx
import pytest

from newperson.brain import Brain, DayPlanRequest, ProactiveRequest, ReplyRequest
from newperson.config import Settings
from newperson.memory import Memory
from newperson.models import ProactivePlan, ReplyPart, ReplyPlan, StoredMessage
from newperson.persona import Persona
from newperson.prompts import build_system

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 40
"""一小段真的 PNG 文件头。图片类型现在按字节认，假字节过不了。"""

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


def test_the_system_prompt_is_the_same_bytes_in_a_fresh_process() -> None:
    """**字节级固定，跨进程也要一样。**

    同一个进程里调两次相同，不能证明什么：真正会咬人的是那种
    "重启之后变了一点点"的不确定性——遍历 set 的顺序、字典的插入顺序、
    任何跟哈希种子有关的东西。稳定层只要差一个字节，prompt cache
    就永远不命中，成本翻好几倍，而**账单之外没有任何症状**。

    所以这里开两个子进程，给不同的 PYTHONHASHSEED，比字节。
    """
    import hashlib
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    code = (
        "from pathlib import Path;"
        "from newperson.persona import load_persona;"
        "from newperson.prompts import build_system;"
        "import sys;"
        f"sys.stdout.write(build_system(load_persona(Path({str(root / 'persona' / 'persona.yaml')!r}))))"
    )
    digests = []
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, check=True, cwd=root,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        digests.append(hashlib.sha256(out).hexdigest())
    assert len(set(digests)) == 1, f"换个哈希种子稳定层就变了：{digests}"


def test_the_system_prompt_carries_no_clock(persona: Persona) -> None:
    """稳定层里不能有今年、明年、或者任何钟点。

    年份从当前时间算，不写死：写死的话这条测试会在某一年悄悄失效，
    而失效的那天正是有人往稳定层里塞了 `datetime.now().year` 的那天。
    """
    from datetime import datetime

    text = build_system(persona)
    this_year = datetime.now().year
    tokens = [str(this_year), str(this_year + 1), "现在是", ":00"]
    for token in tokens:
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
    await brain.generate_reply(reply_request(images=[("image/png", PNG_BYTES)]), TODAY)
    content = client.messages.calls[0]["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"


async def test_a_wrongly_labelled_image_is_sent_with_its_real_type(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """**Discord 报的类型是会错的，按字节认。**

    线上真出过：他发了一张 PNG，Discord 报成 `image/webp`，我们原样转给接口，
    接口对了一遍字节就 400——
    `The image was specified using the image/webp media type, but the image
    appears to be a image/png image`。

    后果不是"这张图没看到"，是**她永久哑掉**：那条消息一直未读，
    每次排新的回复任务都重新带上同一张图、再 400 一次，
    后面所有的话都堵在它后面。实测八条消息堵了一个多小时，
    而 `!np status` 上只能看到"未读 8 条"，看不出为什么。
    """
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="哪拍的")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(images=[("image/webp", PNG_BYTES)]), TODAY)
    content = client.messages.calls[0]["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/png", "照着 Discord 说的发了出去"


async def test_an_image_we_cannot_identify_is_dropped_but_she_still_replies(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """认不出格式的图（iPhone 的 heic、bmp、截断的文件）就别发，但她照样要回。

    接口只收 jpeg / png / gif / webp。发一个它不认的过去就是 400，
    而 400 会把整批未读永久堵死——宁可她看不见这张图。
    """
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    plan = await brain.generate_reply(
        reply_request(images=[("image/heic", b"\x00\x00\x00\x18ftypheic" + b"x" * 40)]), TODAY
    )
    assert plan is not None, "认不出图就不回了？她该照样回，只是看不见图"
    assert isinstance(client.messages.calls[0]["messages"][0]["content"], str)


async def test_every_shape_the_api_accepts_is_recognised() -> None:
    """四种接口收的格式都要认得出来，别把好图也扔了。"""
    from newperson.brain import sniff_media_type

    assert sniff_media_type(PNG_BYTES) == "image/png"
    assert sniff_media_type(b"\xff\xd8\xff\xe0" + b"x" * 40) == "image/jpeg"
    assert sniff_media_type(b"GIF89a" + b"x" * 40) == "image/gif"
    assert sniff_media_type(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 40) == "image/webp"
    assert sniff_media_type(b"\x00\x00\x00\x18ftypheic") is None
    assert sniff_media_type(b"") is None


async def test_an_oversized_image_is_skipped(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(
        reply_request(images=[("image/png", PNG_BYTES + b"x" * (6 * 1024 * 1024))]), TODAY
    )
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


async def test_a_refused_chore_is_retried_once_on_the_main_model(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """Haiku 5.5 带安全分类器，整理记忆时一句亲昵话就可能被拒。拒了换主模型再试一次。

    只在"拒"的时候换：断网、限流换了模型也一样，白花一次调用。
    """
    from newperson.brain import MemoryUpdateRequest
    from newperson.models import MemoryUpdate

    class HaikuRefuses(FakeMessages):
        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["model"] == "claude-haiku-5-5":
                return SimpleNamespace(parsed_output=None, usage=usage(), stop_reason="refusal")
            return SimpleNamespace(
                parsed_output=MemoryUpdate(summary="整理好了"), usage=usage(), stop_reason="end_turn"
            )

    request = MemoryUpdateRequest(
        previous_summary="", messages=[], existing_owner_facts=[], existing_self_facts=[]
    )
    chosen = settings(tmp_path, model="claude-sonnet-5-5", utility_model_override="claude-haiku-5-5")
    client = SimpleNamespace(messages=HaikuRefuses([]))
    brain = Brain(client, chosen, persona, memory)
    got = await brain.update_memory(request, TODAY)
    assert got is not None and got.summary == "整理好了"
    assert [c["model"] for c in client.messages.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    # 主模型接住之后"模型拒绝回答"被清掉了，另记一笔，体检才看得见打杂模型在拒
    assert len([x for x in (await memory.kv_get("utility_refused") or "").split(",") if x]) == 1
    assert await memory.kv_get("day_plan_refused") is None

    # 两个都拒：日程排不出来，她那天就不主动开口，不抛错也不留失败的任务。记一笔。
    # 记忆整理那边自己会记，这里不记，不然一次对半切的七八步全数成"被拒了八次"
    class BothRefuse(FakeMessages):
        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed_output=None, usage=usage(), stop_reason="refusal")

    brain = Brain(SimpleNamespace(messages=BothRefuse([])), chosen, persona, memory)
    assert await brain.update_memory(request, TODAY) is None
    assert await memory.kv_get("day_plan_refused") is None
    plan_request = DayPlanRequest(
        now=datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
        state_line="",
        mood_notes=[],
        wake_at=datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
        sleep_at=datetime(2026, 10, 9, 1, 0, tzinfo=UTC),
        classes=[],
        yesterday=None,
        summary="",
    )
    assert await brain.generate_day_plan(plan_request, TODAY) is None
    assert len([x for x in (await memory.kv_get("day_plan_refused") or "").split(",") if x]) == 1

    error = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    flaky = fake_client(error)
    brain = Brain(flaky, chosen, persona, memory)
    assert await brain.update_memory(request, TODAY) is None
    assert len(flaky.messages.calls) == 1 and not brain.last_refused


async def test_a_refusal_halfway_through_is_still_a_refusal(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """分类器可能在模型写到一半时才拒：内容是半截 JSON，SDK 解析时先抛了校验错误，

    我们看不到 stop_reason。原来这被当成"格式不对"，不换模型、也不缩小那一批，
    记忆照样卡在原地。半截的按被拒处理；完整但字段不对的才是真的格式错，不换。
    """
    import pydantic

    from newperson.brain import MemoryUpdateRequest
    from newperson.models import MemoryUpdate

    def invalid(text: str) -> pydantic.ValidationError:
        try:
            pydantic.TypeAdapter(MemoryUpdate).validate_json(text)
        except pydantic.ValidationError as exc:
            return exc
        raise AssertionError(text)

    class HaikuStops(FakeMessages):
        def __init__(self, haiku_says: str) -> None:
            super().__init__([])
            self.haiku_says = haiku_says

        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["model"] == "claude-haiku-5-5":
                raise invalid(self.haiku_says)
            return SimpleNamespace(
                parsed_output=MemoryUpdate(summary="整理好了"), usage=usage(), stop_reason="end_turn"
            )

    request = MemoryUpdateRequest(
        previous_summary="", messages=[], existing_owner_facts=[], existing_self_facts=[]
    )
    chosen = settings(tmp_path, model="claude-sonnet-5-5", utility_model_override="claude-haiku-5-5")

    halfway = HaikuStops('{"summary": "他们昨晚')
    brain = Brain(SimpleNamespace(messages=halfway), chosen, persona, memory)
    assert await brain.update_memory(request, TODAY) is not None
    assert [c["model"] for c in halfway.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]

    wrong_shape = HaikuStops('{"notes": 1}')
    brain = Brain(SimpleNamespace(messages=wrong_shape), chosen, persona, memory)
    assert await brain.update_memory(request, TODAY) is None
    assert [c["model"] for c in wrong_shape.calls] == ["claude-haiku-5-5"]
    assert not brain.last_refused


async def test_the_last_error_is_recorded_for_the_owner(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """`!np status` 要能看到最近一次出了什么问题。"""
    error = anthropic.APIStatusError(
        "boom", response=httpx.Response(500, request=httpx.Request("POST", "http://x")), body=None
    )
    brain = Brain(fake_client(error), settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    recorded = await memory.kv_get("last_api_error")
    assert recorded and "500" in recorded
    # 带时间戳：不带的话你分不出这是三周前的一次抖动还是刚刚密钥失效
    stamp, _, _detail = recorded.partition("\t")
    assert datetime.fromisoformat(stamp)


async def test_every_common_failure_is_visible_to_the_owner(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """限流、连不上、被拒——三种最常见的失败都要留痕。

    她的正常状态就包含长时间不说话，所以"接口挂了"和"她不想聊"在外面看
    完全一样。原来只有 APIStatusError 和兜底那两条写了 last_api_error，
    而这三种恰恰是最常见的：出事时 `!np status` 干干净净，
    你只会觉得她今天特别安静。
    """
    request = httpx.Request("POST", "http://x")
    failures = [
        anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        ),
        anthropic.APIConnectionError(request=request),
    ]
    for failure in failures:
        await memory.kv_delete("last_api_error")
        brain = Brain(fake_client(failure), settings(tmp_path), persona, memory)
        await brain.generate_reply(reply_request(), TODAY)
        assert await memory.kv_get("last_api_error"), f"{type(failure).__name__} 没留下痕迹"

    # 被拒
    class Refusing(FakeMessages):
        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(parsed_output=None, usage=usage(), stop_reason="refusal")

    await memory.kv_delete("last_api_error")
    brain = Brain(SimpleNamespace(messages=Refusing([])), settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert await memory.kv_get("last_api_error"), "被拒也要留痕"


async def test_a_success_clears_the_old_error(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """接口恢复了就把旧报错清掉。

    不清的话三周前的一次网络抖动会一直挂在 status 上，
    跟"此刻密钥失效了"长得一模一样。
    """
    await memory.kv_set("last_api_error", "2026-01-01T00:00:00+00:00\t很久以前的事")
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(), TODAY)
    assert await memory.kv_get("last_api_error") is None


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
    """重写只给一次机会，卡在这里既费钱又会让她很久不回。

    重写完还是禁语的话，那一条**不发**。原来是原句照发——
    "还在睡 没看到"就是这么漏出去的。一条不剩就当这会儿没看手机，
    交给调度器过一阵再试，这一次不再多调模型。
    """
    bad = ReplyPlan(parts=[ReplyPart(text="加油")])
    client = fake_client(bad, bad, bad)
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got is None or "加油" not in [p.text for p in got.parts]
    assert len(client.messages.calls) == 2


async def test_a_banned_bubble_is_dropped_but_the_rest_still_goes(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """重写那次调用失败了：带禁语的那条不发，别的照发。"""
    first = ReplyPlan(parts=[ReplyPart(text="还在睡 没看到"), ReplyPart(text="soxl后来怎么样了")])
    request = httpx.Request("POST", "http://x")
    outage = anthropic.APIConnectionError(request=request)
    client = fake_client(first, outage)
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got is not None
    assert [p.text for p in got.parts] == ["soxl后来怎么样了"]


async def test_a_proactive_rewrite_that_is_still_banned_is_not_sent(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """主动消息重写回来还是寒暄：这次就不说。"""
    first = ProactivePlan(send=True, parts=[ReplyPart(text="在吗")])
    again = ProactivePlan(send=True, parts=[ReplyPart(text="在吗 好久不见")])
    brain = Brain(fake_client(first, again), settings(tmp_path), persona, memory)
    got = await brain.generate_proactive(proactive_request(), TODAY)
    assert got is not None and (not got.send or not got.parts)



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


def proactive_request(**kw) -> ProactiveRequest:
    base = {
        "situation": "",
        "trigger_note": "随口说一句自己的事",
        "summary": "",
        "owner_facts": [],
        "self_facts": [],
        "recent": [],
        "hours_since_last_exchange": 30.0,
        "unanswered_initiations": 0,
        "photos": [],
    }
    base.update(kw)
    return ProactiveRequest(**base)


async def test_a_greeting_in_a_proactive_message_gets_rewritten(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """主动消息也要过重写那一关，不是只做机械修剪。

    这条最要紧的场景是第一次上线那句开场：她说的第一句话，
    上下文是空的（没有摘要、没有事实、没有最近的对话），
    模型手里没有别的东西可抓，最容易滑到寒暄上去。
    而寒暄是他们认识一年之后最不该出现的东西。

    回复一直有两道关（机械修剪 + 重写一次），主动消息原来只有一道，
    要重写的那部分被直接丢掉了。
    """
    client = fake_client(
        ProactivePlan(send=True, parts=[ReplyPart(text="在吗，好久没聊了")]),
        ProactivePlan(send=True, parts=[ReplyPart(text="今天雪大到地铁都停了")]),
    )
    brain = Brain(client, settings(tmp_path), persona, memory)

    got = await brain.generate_proactive(proactive_request(), TODAY)

    assert len(client.messages.calls) == 2, "带寒暄的那版应该被打回去重写"
    assert got.parts[0].text == "今天雪大到地铁都停了"


async def test_a_clean_proactive_message_does_not_cost_a_second_call(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """没问题就不要多花一次钱。重写只在真的违规时发生。"""
    client = fake_client(
        ProactivePlan(send=True, parts=[ReplyPart(text="图书馆一个位置都没有")])
    )
    brain = Brain(client, settings(tmp_path), persona, memory)

    got = await brain.generate_proactive(proactive_request(), TODAY)

    assert len(client.messages.calls) == 1
    assert got.parts[0].text == "图书馆一个位置都没有"


async def test_a_failed_rewrite_never_lets_a_greeting_through(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """重写调不动模型的时候，宁可这次不说话，也不能把寒暄原样发出去。

    禁语**不是机械可修的**：style_guard 把它标成 fixable=False，
    apply_fixes 一个字都不动，所以"修剪过的那版"就是原句。
    早先这里是"重写没出来就用修剪版发出去"，等于给寒暄开了一道后门——
    模型抽风或者网络抖一下，她的第一句话就变成"在吗"。
    她本来就不是每次想说都会说，少说一句没有代价。
    """
    client = fake_client(
        ProactivePlan(send=True, parts=[ReplyPart(text="在吗")]),
        None,
    )
    brain = Brain(client, settings(tmp_path), persona, memory)

    got = await brain.generate_proactive(proactive_request(), TODAY)

    # 重写没调通时返回 None（交给上层重试，答应他的事不因为网络抖一下就作废），
    # 或者重写回来还是禁语时 send=False——两种都不会把寒暄发出去
    assert got is None or (got.send is False and not got.parts), "寒暄不能因为重写失败就漏出去"


async def test_a_failed_rewrite_still_sends_when_the_problem_is_cosmetic(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """表情太多、句子太长这类是机械可修的，修完照发。

    不能因为上面那条就把所有重写失败都变成沉默——那她会平白少说很多话。
    """
    noisy = "今天雪大到地铁都停了🥹🥹🥹🥹🥹"
    client = fake_client(ProactivePlan(send=True, parts=[ReplyPart(text=noisy)]), None)
    brain = Brain(client, settings(tmp_path), persona, memory)

    got = await brain.generate_proactive(proactive_request(), TODAY)

    assert got is not None and got.send is True
    assert got.parts, "机械可修的问题不该导致她闭嘴"


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


async def test_a_real_question_is_never_left_unanswered(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """她话少是给得少，不是不理人。

    小模型在低 effort 下很容易走"这条不回"这条省事的路，
    但对方问了具体的事还沉默，那不是人设，那是坏了。
    """
    empty = ReplyPlan(parts=[])
    good = ReplyPlan(parts=[ReplyPart(text="止损设了没")])
    client = fake_client(empty, good)
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(must_reply=True), TODAY)
    assert [p.text for p in got.parts] == ["止损设了没"]
    assert len(client.messages.calls) == 2


async def test_small_talk_can_still_go_unanswered(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """"晚安"这种不需要接的话，不回是对的，不该硬逼她说点什么。"""
    client = fake_client(ReplyPlan(parts=[]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(must_reply=False), TODAY)
    assert got.parts == []
    assert len(client.messages.calls) == 1


async def test_a_reaction_counts_as_answering(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """只点个表情也是回应，不用再逼一次。"""
    client = fake_client(ReplyPlan(parts=[], reaction="👀"))
    brain = Brain(client, settings(tmp_path), persona, memory)
    await brain.generate_reply(reply_request(must_reply=True), TODAY)
    assert len(client.messages.calls) == 1


def test_the_output_rules_say_when_silence_is_ok(persona: Persona) -> None:
    """规范要写清楚什么时候可以不回，不能只说"可以是空的"。"""
    text = build_system(persona)
    assert "必须回" in text
    assert "只影响你说话的语气" in text, "状态不该被模型当成不回的理由"


def test_log_timestamps_follow_her_timezone_not_the_servers() -> None:
    """启动时那句"日志里的时间都是她那边的时间"必须是真的。

    原来它是假的：格式化器用服务器本地时间（VPS 上通常是 UTC），
    而日志正文里的任务时刻是她那边的时间。于是一行里两个时区：

        16:07:57 INFO [job] 排上 proactive#2 09-10 13:48 opener

    看上去像是把任务排到了三个小时前。排查"她怎么不说话"的时候，
    这种时间戳会把人直接带到沟里去。
    """
    import logging
    import re
    from datetime import UTC, datetime
    from io import StringIO
    from zoneinfo import ZoneInfo

    from newperson.__main__ import setup_logging

    boston = ZoneInfo("America/New_York")
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.handlers.clear()
        setup_logging("INFO", boston)
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(root.handlers[0].formatter)
        root.handlers = [handler]
        logging.getLogger("t").info("测试")
    finally:
        root.handlers = saved

    stamp = re.match(r"(\d\d-\d\d \d\d:\d\d)", stream.getvalue())
    assert stamp, f"时间戳格式不对：{stream.getvalue()!r}"
    assert stamp.group(1) == datetime.now(boston).strftime("%m-%d %H:%M")
    # 波士顿跟 UTC 从来不是同一个偏移，所以这条能真的分辨出用的是哪个时区
    assert stamp.group(1) != datetime.now(UTC).strftime("%m-%d %H:%M")


async def test_the_api_error_timestamp_comes_from_the_clock(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """接口报错的时间戳要走 Clock，不能裸调 datetime.now()。

    体检靠这个戳分"半年前那次抖动"和"此刻密钥失效了"——
    一个是看看就行，一个是退出码 1。裸调的话测试没法把时钟拨过去验它。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from newperson.clock import FakeClock

    frozen = datetime(2026, 3, 14, 9, 26, tzinfo=ZoneInfo("America/New_York"))
    brain = Brain(
        fake_client(RuntimeError("连不上")), settings(tmp_path), persona, memory, FakeClock(frozen)
    )
    assert await brain.generate_reply(reply_request(), TODAY) is None

    stamp, _, _ = (await memory.kv_get("last_api_error")).partition("\t")
    assert datetime.fromisoformat(stamp) == frozen, f"戳的是 {stamp}，不是时钟上的时间"


def test_his_small_hours_are_labelled_as_small_hours() -> None:
    """给她看的时间要带"凌晨/下午"，不能只给一个 24 小时制的数。

    线上真出过：他那边凌晨两点，她回了句"两点是该起了"，
    他得纠正两次。隔着十四个小时的时差，"几点"本来就容易算错，
    光给 ``02:05`` 模型会把它当成一个普通的钟点。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from newperson.prompts import format_time

    sydney = ZoneInfo("Australia/Sydney")
    assert "凌晨 02:05" in format_time(datetime(2026, 9, 26, 2, 5, tzinfo=sydney))
    assert "下午 14:30" in format_time(datetime(2026, 9, 26, 14, 30, tzinfo=sydney))
    assert "晚上 20:34" in format_time(datetime(2026, 9, 26, 20, 34, tzinfo=sydney))


def test_a_stale_reminder_is_told_he_has_spoken_since(persona: Persona) -> None:
    """记下"要问他的事"之后他又说过话，提示词要把这一点讲出来。

    线上真出过：他已经说了"做完了"，她自己也回了"不错"，
    一个多小时之后那条旧的提醒照样响了，问他"弄完了吧"，
    他回"我不是和你说了吗"。模型看得见最近的对话，
    但那条提醒是"你为什么想说话"——它会照着提醒去问，除非被明确告诉：
    这件事是早先记下的，他之后说过话，先看看他是不是已经答了。
    """
    from newperson.prompts import build_proactive_user

    common = dict(
        persona=persona,
        situation="此刻",
        trigger_note="问问他作业 A 弄完没有",
        summary="",
        owner_facts=[],
        self_facts=[],
        recent=[],
        hours_since_last_exchange=2.0,
        unanswered_initiations=0,
        photos=[],
    )
    stale = build_proactive_user(**common, he_spoke_since_noted=True)
    fresh = build_proactive_user(**common, he_spoke_since_noted=False)
    assert "那之后他又说过话" in stale
    assert "别再问" in stale
    assert "那之后他又说过话" not in fresh


@pytest.mark.parametrize(
    ("status", "message", "expect"),
    [
        (400, "Your credit balance is too low to access the Anthropic API.", "余额"),
        (400, "You have reached your specified API usage limits.", "花费上限"),
        (401, "invalid x-api-key", "API key"),
    ],
)
async def test_money_trouble_is_named_in_the_status(
    persona: Persona, tmp_path: Path, memory: Memory, status: int, message: str, expect: str
) -> None:
    """余额用完、撞到花费上限、密钥不认——这几种要他本人去处理。

    原来在 `!np status` 里都只是"接口返回 400"，跟任何别的 400 分不开。
    他问过"快到期了提醒我一下，我得去充钱"：这就是那个提醒。
    """
    from newperson.doctor import _safe_detail

    request = httpx.Request("POST", "http://x")
    cls = anthropic.AuthenticationError if status == 401 else anthropic.BadRequestError
    error = cls(
        message,
        response=httpx.Response(status, request=request),
        body={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )
    brain = Brain(fake_client(error), settings(tmp_path), persona, memory)
    assert await brain.generate_reply(reply_request(), TODAY) is None
    noted = (await memory.kv_get("last_api_error") or "").partition("\t")[2]
    assert expect in noted, noted
    assert _safe_detail(noted) == noted, "体检会把这条藏起来"


def test_an_out_of_range_number_does_not_void_the_whole_reply() -> None:
    """模型给出十几天的 follow_up、五分钟的停顿：截到边上，别整条作废。

    范围只写在 schema 的说明里，接口不强制。原来 SDK 解析时拒收，
    这次回复作废、重试三次还是一样——他那句话一直没人回，那几次调用照样计费。
    """
    plan = ReplyPlan.model_validate_json(
        '{"parts":[{"text":"行","pause_before_seconds":999}],'
        '"follow_up":{"delay_minutes":20000,"note":"下下周末看完告诉他"}}'
    )
    assert plan.parts[0].pause_before_seconds == 180
    assert plan.follow_up is not None and plan.follow_up.delay_minutes == 60 * 24 * 7


async def test_an_unparseable_answer_still_counts_against_the_daily_cap(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """接口回了、计了费、SDK 却解析不了：这一次也要记进用量，日限额才看得见。"""
    import pydantic

    try:
        ReplyPlan.model_validate_json('{"parts": "不是列表"}')
    except pydantic.ValidationError as exc:
        error = exc
    brain = Brain(fake_client(error), settings(tmp_path), persona, memory)
    assert await brain.generate_reply(reply_request(), TODAY) is None
    usage_today = await memory.usage_for(TODAY)
    assert usage_today and usage_today["calls"] >= 1


def test_a_dated_model_id_is_priced_like_its_alias() -> None:
    """.env 里写成带日期的正式 ID 也按同一个价算；认不出的型号按高价算，宁可多估。"""
    from newperson.brain import PRICING_PER_MTOK, price_of

    assert price_of("claude-haiku-4-5-20251001") == PRICING_PER_MTOK["claude-haiku-4-5"]
    assert price_of("claude-sonnet-5") == PRICING_PER_MTOK["claude-sonnet-5"]
    assert price_of("claude-sonnet-5-preview") == (5.0, 25.0)


def test_the_current_models_are_priced_and_get_their_effort() -> None:
    """换成新型号时，漏在表外不会报错：effort 悄悄没传、花费按高价估。"""
    from newperson.brain import EFFORT_SUPPORTED, price_of

    assert price_of("claude-sonnet-5-5") == (2.0, 10.0)
    assert price_of("claude-opus-5-5") == (4.0, 20.0)
    assert price_of("claude-haiku-5-5") == (0.10, 0.50)
    assert {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"} <= EFFORT_SUPPORTED
    assert "claude-haiku-4-5" not in EFFORT_SUPPORTED


def test_the_cost_follows_each_models_cache_and_length_pricing() -> None:
    """5.5 这代缓存命中只收半成；Haiku 5.5 提示超过十万 token 整个请求贵五倍。

    一律按一成算的话，Sonnet 5.5 的花费在 `!np status` 里多估一倍。
    """
    from newperson.brain import cost_of

    million = 1_000_000
    assert cost_of("claude-sonnet-5-5", 0, million, 0, 0) == pytest.approx(0.10)
    assert cost_of("claude-sonnet-5", 0, million, 0, 0) == pytest.approx(0.20)
    assert cost_of("claude-haiku-5-5", 0, 0, 0, million) == pytest.approx(0.50)
    assert cost_of("claude-haiku-5-5", 100_001, 0, 0, million) == pytest.approx(2.50 + 0.05)
    # 门槛是"超过"十万，正好十万还按低档；缓存读写也算进提示长度
    assert cost_of("claude-haiku-5-5", 100_000, 0, 0, 0) == pytest.approx(0.01)
    assert cost_of("claude-haiku-5-5", 50_000, 60_000, 0, 0) == pytest.approx(
        (50_000 * 0.50 + 60_000 * 0.05) / million
    )
    assert cost_of("claude-haiku-4-5", 200_000, 0, million, 0) == pytest.approx(0.20 + 1.25)


async def test_a_rewrite_that_is_still_all_banned_does_not_wedge_the_message(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """重写回来还是只有禁语：这一批不接话，而不是返回 None 让调度器重试。

    重试只会让模型把同一个词写三遍、烧六次调用，然后任务判死，
    他那句一直挂在未读里，有未读期间主动消息也全被压住。
    """
    bad = ReplyPlan(parts=[ReplyPart(text="还在睡 刚看到")])
    client = fake_client(bad, ReplyPlan(parts=[ReplyPart(text="刚醒 才看到")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got is not None and got.parts == []
    assert len(client.messages.calls) == 2



async def test_she_only_slips_when_the_code_says_so(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """打错字多久一次由代码定：没让她手滑，模型自己写的 typo_text 也不用；让了，只留一条。"""
    typed = ReplyPlan(
        parts=[ReplyPart(text="我在图书馆", typo_text="我再图书馆"), ReplyPart(text="你呢", typo_text="你尼")]
    )
    brain = Brain(fake_client(typed.model_copy(deep=True)), settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(), TODAY)
    assert got is not None and all(not p.typo_text for p in got.parts)

    brain = Brain(fake_client(typed.model_copy(deep=True)), settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(typo=True), TODAY)
    assert got is not None and [p.typo_text for p in got.parts] == ["我再图书馆", ""]


def test_the_slip_is_asked_for_only_in_the_request_that_slips(persona: Persona) -> None:
    """"这次打字手滑了"只进那一次的 user 消息；稳定层只有一句固定的"平时留空"。"""
    from newperson.prompts import build_reply_user

    base = dict(
        persona=persona, situation="此刻", summary="", owner_facts=[], self_facts=[],
        ledger=[], mode_instruction="", recent=[], unread=[], hints=[], photos=[],
    )
    assert "这次打字手滑了" in build_reply_user(**base, typo=True)
    assert "这次打字手滑了" not in build_reply_user(**base)
    assert "typo_text" in build_system(persona)


async def test_proactive_messages_never_slip(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """手滑只在回复里由代码掷骰子。主动开口的那句，模型自己填了 typo_text 也不用。"""
    first = ProactivePlan(send=True, parts=[ReplyPart(text="我在图书馆", typo_text="我再图书馆")])
    brain = Brain(fake_client(first), settings(tmp_path), persona, memory)
    got = await brain.generate_proactive(proactive_request(), TODAY)
    assert got is not None and got.parts and not got.parts[0].typo_text


def _png(width: int, height: int) -> bytes:
    import struct

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0" * 8


def _image_blocks(call: dict) -> list:
    content = call["messages"][0]["content"]
    return [] if isinstance(content, str) else [b for b in content if b["type"] == "image"]


async def test_a_long_screenshot_is_not_sent_to_the_model(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """聊天记录的长截图一万多像素高，接口每次都拒：不带它，她照样回。

    带着它的话每次都 400，那批话一直未读、他后面说的也全堵住——
    直到两周后那张图被清理掉。
    """
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="哈哈")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(
        reply_request(images=[("image/png", _png(1080, 9000)), ("image/png", _png(1080, 1920))]),
        TODAY,
    )
    assert got is not None and got.parts
    assert len(_image_blocks(client.messages.calls[0])) == 1


async def test_too_many_pictures_keeps_the_newest_twenty(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """一次最多带二十张：超过了接口对每张的尺寸要求更严，干脆只带最新的。"""
    client = fake_client(ReplyPlan(parts=[ReplyPart(text="好多")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    images = [("image/png", _png(100 + i, 100)) for i in range(25)]
    await brain.generate_reply(reply_request(images=images), TODAY)
    blocks = _image_blocks(client.messages.calls[0])
    assert len(blocks) == 20


async def test_a_picture_the_api_rejects_is_dropped_and_she_still_answers(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """预检没认出来的毛病、带图被接口 400 了：不带图马上再问一次，不交给调度器重试十几个小时。"""
    request = httpx.Request("POST", "http://x")
    rejected = anthropic.BadRequestError(
        "image exceeds limits", response=httpx.Response(400, request=request), body=None
    )
    client = fake_client(rejected, ReplyPlan(parts=[ReplyPart(text="看不清")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(images=[("image/png", PNG_BYTES)]), TODAY)
    assert got is not None and [p.text for p in got.parts] == ["看不清"]
    assert _image_blocks(client.messages.calls[0]) and not _image_blocks(client.messages.calls[1])


async def test_a_server_hiccup_with_a_picture_is_not_a_reason_to_drop_it(
    persona: Persona, tmp_path: Path, memory: Memory
) -> None:
    """接口 5xx 是暂时的：不丢图重问，交给调度器过一会儿带着图再试。"""
    request = httpx.Request("POST", "http://x")
    hiccup = anthropic.InternalServerError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    client = fake_client(hiccup, ReplyPlan(parts=[ReplyPart(text="嗯")]))
    brain = Brain(client, settings(tmp_path), persona, memory)
    got = await brain.generate_reply(reply_request(images=[("image/png", PNG_BYTES)]), TODAY)
    assert got is None and len(client.messages.calls) == 1
