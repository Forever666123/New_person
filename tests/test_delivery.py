"""投递的测试。

这里断言的都是"发出来像不像人"的底线：气泡的顺序、"正在输入"显示得住不住、
说到一半被打断会不会硬发完、发不出去的时候前面已经发的会不会丢。

假 channel 只实现 delivery 里那三个 Protocol，所以整套测试不需要真的 discord 连接。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from newperson.attention import AttentionPolicy
from newperson.clock import FakeClock
from newperson.delivery import (
    DISCORD_MAX_LEN,
    Deliverer,
    DeliveryBlocked,
)
from newperson.models import ProactivePlan, ReplyPart, ReplyPlan, ResolvedPhoto
from newperson.persona import Persona
from newperson.rhythm import Rhythm

# -- 假的 Discord ------------------------------------------------------------


class Forbidden(Exception):
    """假的 ``discord.Forbidden``。

    投递层按类名和 403 认它（见 ``delivery._is_forbidden``），所以测试里不用真的
    discord 异常对象，也就不用为了造一个异常去建 HTTP 响应。
    """

    status = 403


@dataclass
class Event:
    """一次可观察的动作，带发生时刻。有了时刻才能断言"停顿发生在两次打字之间"。"""

    kind: str
    at: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sent:
    content: str | None
    file: Any = None
    reference: Any = None


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeTyping:
    """``async with channel.typing():`` 的假实现，只记录进出。"""

    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    async def __aenter__(self) -> FakeTyping:
        self.channel.typing_open += 1
        self.channel.log("typing_enter")
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.channel.typing_open -= 1
        self.channel.log("typing_exit")
        return False


class FakeChannel:
    def __init__(self, clock: FakeClock, fail_at: int | None = None, error: Exception | None = None) -> None:
        """``fail_at`` 是第几次 send 抛 ``error``（从 0 数），用来测中途失败。"""
        self.clock = clock
        self.sends: list[Sent] = []
        self.events: list[Event] = []
        self.typing_open = 0
        self.typing_sessions = 0
        self.fail_at = fail_at
        self.error = error
        self._start = clock.now()

    def log(self, kind: str, **payload: Any) -> None:
        self.events.append(Event(kind, (self.clock.now() - self._start).total_seconds(), payload))

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    @property
    def texts(self) -> list[str | None]:
        return [s.content for s in self.sends]

    def typing(self) -> FakeTyping:
        self.typing_sessions += 1
        return FakeTyping(self)

    async def trigger_typing(self) -> None:
        raise AssertionError("不许用 trigger_typing：它只顶约 10 秒，长消息打到一半就露馅")

    async def send(self, content: str | None = None, *, file: Any = None, reference: Any = None) -> FakeMessage:
        assert self.typing_open == 0, "发出去的时候应该已经退出 typing 上下文了"
        if self.fail_at is not None and len(self.sends) == self.fail_at:
            assert self.error is not None
            raise self.error
        self.sends.append(Sent(content, file, reference))
        self.log("send", content=content)
        return FakeMessage(1000 + len(self.sends))


class FakeReactable:
    def __init__(self, clock: FakeClock, message_id: int = 42) -> None:
        self.clock = clock
        self.id = message_id
        self.reactions: list[str] = []
        self.reacted_at: datetime | None = None

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)
        self.reacted_at = self.clock.now()


class AlwaysPause(random.Random):
    """让"打一半停下来想"必然发生，且停顿长度固定。

    ``random()`` 恒为 0：概率判定必中，``uniform`` 取区间下界，``gauss`` 退化成均值，
    于是整段时长可以手算，断言不用留模糊余量。
    """

    def random(self) -> float:
        return 0.0


# -- 夹具 --------------------------------------------------------------------


@pytest.fixture
def policy(persona: Persona, rhythm: Rhythm) -> AttentionPolicy:
    return AttentionPolicy(persona, rhythm)


@pytest.fixture
def deliverer(clock: FakeClock, policy: AttentionPolicy, rng: random.Random) -> Deliverer:
    # make_file 直接返回路径，断言时好比对
    return Deliverer(clock, policy, rng, make_file=lambda p: p)


@pytest.fixture
def channel(clock: FakeClock) -> FakeChannel:
    return FakeChannel(clock)


def parts(*texts: str) -> list[ReplyPart]:
    return [ReplyPart(text=t) for t in texts]


PHOTO = ResolvedPhoto(path="/photos/window-001.jpg", photo_id="window-001")


# -- 顺序与打字 --------------------------------------------------------------


async def test_parts_are_sent_in_order(deliverer: Deliverer, channel: FakeChannel) -> None:
    """几条气泡按给的顺序发出去，一条不多一条不少。"""
    result = await deliverer.deliver(channel, parts("刚下课", "累死了", "你在干嘛"), None)

    assert channel.texts == ["刚下课", "累死了", "你在干嘛"]
    assert result.sent_texts == ["刚下课", "累死了", "你在干嘛"]
    assert result.sent_message_ids == [1001, 1002, 1003]
    assert not result.interrupted


async def test_typing_context_is_entered_and_exited(deliverer: Deliverer, channel: FakeChannel) -> None:
    """每条都真的进出 typing 上下文（不是 trigger_typing），发的时候已经退出来了。"""
    await deliverer.deliver(channel, parts("在", "刚看到"), None)

    assert channel.kinds == [
        "typing_enter",
        "typing_exit",
        "send",
        "typing_enter",
        "typing_exit",
        "send",
    ]
    assert channel.typing_open == 0


async def test_typing_takes_longer_for_longer_text(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """打一长段花的时间明显比打一个字多。"""
    await deliverer.deliver(channel, parts("嗯"), None)
    short = sum(clock.slept)

    clock.slept.clear()
    await deliverer.deliver(channel, parts("今天" * 60), None)
    long_text = sum(clock.slept)

    assert short > 0
    assert long_text > short * 3


async def test_pause_before_is_actually_waited(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """pause_before_seconds 是真的静默等着，而且等在开始打字之前。"""
    await deliverer.deliver(channel, [ReplyPart(text="想了一下", pause_before_seconds=7.0)], None)

    assert 7.0 in clock.slept
    assert channel.events[0].kind == "typing_enter"
    assert channel.events[0].at >= 7.0


async def test_long_typing_pauses_midway_outside_typing_context(
    clock: FakeClock, policy: AttentionPolicy, channel: FakeChannel
) -> None:
    """打字超过 8 秒时会停下来想：退出 typing 上下文停几秒，再重新进去接着打。"""
    deliverer = Deliverer(clock, policy, AlwaysPause(), make_file=lambda p: p)

    await deliverer.deliver(channel, parts("这条挺长的" * 20), None)

    assert channel.kinds == ["typing_enter", "typing_exit", "typing_enter", "typing_exit", "send"]
    # 停顿夹在两次打字之间，长度取区间下界 2 秒
    assert channel.events[2].at - channel.events[1].at == pytest.approx(2.0)
    assert channel.typing_sessions == 2


async def test_short_text_is_typed_in_one_go(deliverer: Deliverer, channel: FakeChannel) -> None:
    """短句一口气打完，不会中途停——那样反而假。"""
    await deliverer.deliver(channel, parts("好"), None)

    assert channel.typing_sessions == 1


# -- 打断 --------------------------------------------------------------------


async def test_interruption_stops_remaining_parts(deliverer: Deliverer, channel: FakeChannel) -> None:
    """发到一半对方又说话了：剩下的不发，已经发出去的留在结果里。"""

    async def interrupted() -> bool:
        return True

    result = await deliverer.deliver(
        channel, parts("等我一下", "我先去洗个澡", "回来跟你说"), None, interrupted=interrupted
    )

    assert channel.texts == ["等我一下"]
    assert result.sent_texts == ["等我一下"]
    assert result.interrupted
    assert result.next_index == 1


async def test_not_interrupted_marks_nothing(deliverer: Deliverer, channel: FakeChannel) -> None:
    """没被打断就一路发完，最后一条之后不再问（问了也只会让上层误会）。"""
    calls = 0

    async def interrupted() -> bool:
        nonlocal calls
        calls += 1
        return False

    result = await deliverer.deliver(channel, parts("一", "二", "三"), None, interrupted=interrupted)

    assert channel.texts == ["一", "二", "三"]
    assert calls == 2
    assert not result.interrupted


# -- 照片 --------------------------------------------------------------------


async def test_photo_placeholder_attaches_to_that_part(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """带 {photo} 的那条自己带图发，占位符不会漏出去。"""
    result = await deliverer.deliver(
        channel, parts("刚下雪 {photo}", "外面好安静"), PHOTO
    )

    assert channel.texts == ["刚下雪", "外面好安静"]
    assert channel.sends[0].file == Path(PHOTO.path)
    assert channel.sends[1].file is None
    assert result.photo_sent == PHOTO


async def test_photo_without_placeholder_is_sent_last(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """模型忘了写占位符，但图挑好了：跟在最后单独发一条，不配字。"""
    result = await deliverer.deliver(channel, parts("看窗外"), PHOTO)

    assert channel.texts == ["看窗外", None]
    assert channel.sends[-1].file == Path(PHOTO.path)
    assert result.sent_texts == ["看窗外"]
    assert result.photo_sent == PHOTO


async def test_placeholder_without_photo_drops_empty_part(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """没挑到图：占位符去掉，只剩占位符的那条整条丢掉，不发空消息。"""
    result = await deliverer.deliver(
        channel, parts("你看", "{photo}", "拍得不好"), None
    )

    assert channel.texts == ["你看", "拍得不好"]
    assert result.photo_sent is None


async def test_photo_attaches_only_once(deliverer: Deliverer, channel: FakeChannel) -> None:
    """模型在两条里都写了 {photo}，也只发一张图，第二条只是去掉占位符。"""
    await deliverer.deliver(channel, parts("你看 {photo}", "{photo} 好看吧"), PHOTO)

    assert [s.file for s in channel.sends] == [Path(PHOTO.path), None]
    assert channel.texts == ["你看", "好看吧"]


# -- 长文本拆分 --------------------------------------------------------------


async def test_long_text_is_split_under_discord_limit(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """超过 2000 字按句号拆开，每条都在限制内，内容一个字不丢。"""
    text = "".join(f"第{i}句话说的事情都差不多。" for i in range(300))
    assert len(text) > DISCORD_MAX_LEN * 2

    await deliverer.deliver(channel, parts(text), None)

    assert len(channel.sends) >= 3
    assert all(len(s.content or "") <= DISCORD_MAX_LEN for s in channel.sends)
    assert "".join(s.content or "" for s in channel.sends) == text


async def test_unpunctuated_wall_of_text_is_hard_cut(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """一整段没有标点的长文只能硬切，但仍然不许超过 2000 字。"""
    text = "啊" * (DISCORD_MAX_LEN + 500)

    await deliverer.deliver(channel, parts(text), None)

    assert [len(s.content or "") for s in channel.sends] == [DISCORD_MAX_LEN, 500]


# -- 表情反应 ----------------------------------------------------------------


async def test_reaction_only_without_text(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """可以只点个表情不说话——这本来就是她常见的回应方式。"""
    target = FakeReactable(clock)

    result = await deliverer.deliver(channel, [], None, reaction="👍", react_to=target)

    assert target.reactions == ["👍"]
    assert channel.sends == []
    assert result.reacted
    assert result.sent_texts == []


async def test_reaction_comes_before_text(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """先点表情再打字：反过来就像写完了才想起来点个赞。"""
    target = FakeReactable(clock)

    await deliverer.deliver(channel, parts("哈哈哈"), None, reaction="😂", react_to=target)

    assert target.reacted_at is not None
    assert target.reacted_at <= clock.now()
    assert channel.events[0].kind == "typing_enter"
    assert target.reacted_at < clock.now()


# -- 续发与进度 --------------------------------------------------------------


async def test_start_index_does_not_resend_earlier_parts(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """重启续发：前面已经发过的不重发。"""
    seen: list[int] = []

    async def on_progress(index: int) -> None:
        seen.append(index)

    result = await deliverer.deliver(
        channel, parts("一", "二", "三"), None, on_progress=on_progress, start_index=1
    )

    assert channel.texts == ["二", "三"]
    assert seen == [1, 2]
    assert result.next_index == 3


async def test_on_progress_called_after_every_part(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """每发出一条就回调一次，上层靠它写进度，回调时该条确实已经发出去了。"""
    seen: list[tuple[int, int]] = []

    async def on_progress(index: int) -> None:
        seen.append((index, len(channel.sends)))

    await deliverer.deliver(channel, parts("一", "二", "三"), None, on_progress=on_progress)

    assert seen == [(0, 1), (1, 2), (2, 3)]


async def test_start_index_skips_reaction(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """续发不再点一次表情，那条消息上早就有了。"""
    target = FakeReactable(clock)

    await deliverer.deliver(
        channel, parts("一", "二"), None, reaction="👍", react_to=target, start_index=1
    )

    assert target.reactions == []
    assert channel.texts == ["二"]


# -- 引用回复 ----------------------------------------------------------------


async def test_reply_reference_only_on_first_part(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """引用只挂在第一条气泡上，后面几条是接着自己说。"""
    target = FakeReactable(clock, message_id=777)
    plan = ReplyPlan(parts=parts("这个我看了", "感觉还行"))

    await deliverer.deliver_reply(channel, plan, None, reply_to=target)

    assert channel.sends[0].reference is target
    assert channel.sends[1].reference is None


async def test_reply_plan_carries_reaction(
    deliverer: Deliverer, channel: FakeChannel, clock: FakeClock
) -> None:
    """ReplyPlan 里的 reaction 会点到对方那条消息上。"""
    target = FakeReactable(clock)
    plan = ReplyPlan(parts=parts("好"), reaction="👀")

    result = await deliverer.deliver_reply(channel, plan, None, react_to=target)

    assert target.reactions == ["👀"]
    assert result.reacted


async def test_proactive_send_false_sends_nothing(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """模型自己判断这会儿没必要开口，就一条都不发。"""
    plan = ProactivePlan(send=False, parts=parts("在吗"))

    result = await deliverer.deliver_proactive(channel, plan, None)

    assert channel.sends == []
    assert result.sent_texts == []


# -- 错误分类 ----------------------------------------------------------------


async def test_forbidden_becomes_delivery_blocked(clock: FakeClock, policy: AttentionPolicy, rng: random.Random) -> None:
    """403：换成 DeliveryBlocked 往上抛，已经发出去的那条留在异常里。"""
    channel = FakeChannel(clock, fail_at=1, error=Forbidden("cannot send messages to this user"))
    deliverer = Deliverer(clock, policy, rng, make_file=lambda p: p)

    with pytest.raises(DeliveryBlocked) as caught:
        await deliverer.deliver(channel, parts("一", "二", "三"), None)

    result = caught.value.result
    assert result.sent_texts == ["一"]
    assert result.next_index == 1
    assert "私信" in caught.value.hint
    assert isinstance(caught.value.__cause__, Forbidden)


async def test_network_error_propagates_with_progress(
    clock: FakeClock, policy: AttentionPolicy, rng: random.Random
) -> None:
    """网络错误原样抛出交给调度器重试，但要带上已发进度，免得重试时重复发。"""
    channel = FakeChannel(clock, fail_at=1, error=ConnectionError("连接断了"))
    deliverer = Deliverer(clock, policy, rng, make_file=lambda p: p)

    with pytest.raises(ConnectionError) as caught:
        await deliverer.deliver(channel, parts("一", "二"), None)

    result = caught.value.delivery_result  # type: ignore[attr-defined]
    assert result.sent_texts == ["一"]
    assert result.next_index == 1


async def test_reaction_failure_does_not_lose_the_reply(
    deliverer: Deliverer, channel: FakeChannel
) -> None:
    """表情加不上（比如那条消息被删了）不该把整条回复拖没。"""

    class BrokenReactable:
        async def add_reaction(self, emoji: str) -> None:
            raise RuntimeError("消息没了")

    result = await deliverer.deliver(
        channel, parts("嗯"), None, reaction="👍", react_to=BrokenReactable()
    )

    assert channel.texts == ["嗯"]
    assert not result.reacted


async def test_a_forbidden_reaction_does_not_mute_her(deliverer, channel) -> None:
    """没有加表情的权限，不等于说不出话。**这两个是不同的权限。**

    反应是加在所有文字**之前**的。403 往上抛的话，整条回复一个字都发不出去，
    会话被标成 deliverable=0，而那个标记只在"真的发出了文字"时才收回来——
    这条路永远走不到。她就此永久哑掉，而消息照样被 mark_read，
    体检里"他的话没人回"那一项也一声不吭。
    公开频道里机器人有 Send Messages 没有 Add Reactions 就是这个形状。
    """

    class NoReactions:
        def __init__(self) -> None:
            self.tried = 0

        async def add_reaction(self, _emoji: str) -> None:
            self.tried += 1
            raise PermissionError403()

    class PermissionError403(Exception):
        status = 403

    target = NoReactions()
    result = await deliverer.deliver(
        channel,
        [ReplyPart(text="在的"), ReplyPart(text="刚下课")],
        None,
        reaction="👍",
        react_to=target,
    )

    assert target.tried == 1, "前提不成立：没去加表情"
    assert not result.reacted
    assert result.sent_texts == ["在的", "刚下课"], "表情加不上就把话也咽回去了"
    assert [s.content for s in channel.sends] == ["在的", "刚下课"]
