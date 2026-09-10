"""投递：把 ReplyPlan / ProactivePlan 变成 Discord 上的一串消息。见 DESIGN.md 2.3。

这一层决定的是"发出来像不像人"，而不是"说什么"。三件事最要紧：

- **打字要显示得住**。用 ``async with channel.typing():``，discord.py 会每 ~5 秒自动续期；
  单次 ``trigger_typing()`` 只顶约 10 秒，一条 30 秒的话打到一半"正在输入"就没了，
  接着突然冒出一长段，这是最典型的机器人破绽。
- **打字不是匀速的**。长句打到一半会停下来想一下（退出 typing 再重新进入），
  对方看到的是"输入…"闪一下停一下，跟真人一样。
- **发到一半可以停**。对方在她发第二条之前又说了话，剩下的气泡就不该硬发完，
  上层会拿着新消息重新想一遍。

不直接依赖 discord.py 的具体类，只依赖下面的 Protocol，方便测试。
异常也一样：``discord.Forbidden`` 按类名和 HTTP 状态识别（见 :func:`_is_forbidden`），
所以测试里用一个同名的假异常就够了，不用真的建网关连接。
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .attention import AttentionPolicy
from .clock import Clock
from .models import ProactivePlan, ReplyPart, ReplyPlan, ResolvedPhoto

log = logging.getLogger(__name__)

DISCORD_MAX_LEN = 2000

PHOTO_PLACEHOLDER = "{photo}"

THINK_PAUSE_AFTER_SECONDS = 8.0
"""打字超过这么久才可能中途停下来想。短句一口气打完，停反而假。"""

THINK_PAUSE_PROBABILITY = 0.4

THINK_PAUSE_RANGE = (2.0, 6.0)
"""停下来想多久。太短看不出来，太长像掉线。"""

THINK_PAUSE_SPLIT = (0.3, 0.7)
"""在整段打字时长的哪个位置停。"""

BLOCKED_HINT = "Owner 需要和机器人共享一个服务器并允许来自服务器成员的私信"

_SEGMENT = re.compile(r"[^。！？!?\n]*[。！？!?\n]+|[^。！？!?\n]+")
"""按句号/换行切段，分隔符跟着前一段走，拆开之后读起来还是完整的句子。"""


class SentMessage(Protocol):
    id: int


class Channel(Protocol):
    def typing(self) -> Any:
        """async context manager，显示"正在输入…"。"""
        ...

    async def send(
        self, content: str | None = None, *, file: Any = None, reference: Any = None
    ) -> SentMessage: ...


class Reactable(Protocol):
    async def add_reaction(self, emoji: str) -> None: ...


@dataclass
class DeliveryResult:
    sent_texts: list[str] = field(default_factory=list)
    sent_message_ids: list[int] = field(default_factory=list)
    photo_sent: ResolvedPhoto | None = None
    reacted: bool = False
    interrupted: bool = False
    """发到一半发现对方又发了新消息，剩余气泡没发。"""
    next_index: int = 0
    """下一条该从这里发。中途失败或被打断时，重启/重试靠它续上，不会重发。"""


class DeliveryBlocked(RuntimeError):
    """这个会话根本发不出去（Discord 403）。

    和网络错误的区别是**重试没有意义**：上层据此把会话标记为 ``deliverable=0``、
    任务记 ``done(undeliverable)``、不再安排主动消息，并把 :data:`BLOCKED_HINT` 打进日志。
    """

    def __init__(self, result: DeliveryResult, hint: str = BLOCKED_HINT) -> None:
        super().__init__(f"投递被 Discord 拒绝：{hint}")
        self.result = result
        """已经发出去的部分。拒绝往往发生在第 n 条上，前面几条是真的发出去了。"""
        self.hint = hint


def _is_forbidden(exc: BaseException) -> bool:
    """认出 ``discord.Forbidden``，但不 import discord。

    投递层要能在没有网关的测试里跑，所以按类名和 HTTP 状态认：
    50007（不能私聊）和 50001（无权限）都会以 403 的形式到这里。
    """
    if any(cls.__name__ == "Forbidden" for cls in type(exc).__mro__):
        return True
    return getattr(exc, "status", None) == 403


def _attach_partial(exc: BaseException, result: DeliveryResult) -> None:
    """把"已经发出去了多少"挂到异常上。

    交给调度器重试的那条路上，异常是唯一的返回值；不带上进度的话，
    重试会把前面几条再发一遍，对方就会看到重复的消息。
    """
    try:
        exc.delivery_result = result  # type: ignore[attr-defined]
    except AttributeError:  # 带 __slots__ 的异常挂不上，挂不上就算了，不能因此吞掉原异常
        log.debug("异常 %s 挂不上投递进度", type(exc).__name__)


def _split_long(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    """超长气泡按句号/换行拆成多条。

    Discord 硬性限制 2000 字。宁可在句子边界上拆，也不要拦腰截断：
    真人一次说太多也是分几条发的，断在句号上看不出破绽。
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for segment in _SEGMENT.findall(text):
        # 单句就超长（一句话不带标点写了两千多字），只能硬切
        while len(segment) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(segment[:limit])
            segment = segment[limit:]
        if len(buf) + len(segment) > limit:
            chunks.append(buf)
            buf = segment
        else:
            buf += segment
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


class Deliverer:
    def __init__(
        self,
        clock: Clock,
        attention: AttentionPolicy,
        rng: random.Random,
        make_file: Callable[[Path], Any],
    ) -> None:
        """make_file: 把本地路径变成 discord.File（测试里传 lambda p: p）。"""
        self.clock = clock
        self.attention = attention
        self.rng = rng
        self.make_file = make_file

    # -- 组稿 ---------------------------------------------------------------

    def prepare_parts(
        self, parts: list[ReplyPart], photo: ResolvedPhoto | None
    ) -> list[tuple[ReplyPart, bool]]:
        """处理 {photo} 占位：有图 → 该条带图发送（去掉占位符）；无图 → 去掉占位符，若去掉后为空则丢弃该条。
        没有任何一条含占位但有图 → 图跟在最后一条后面单独发。超过 2000 字的气泡拆分。
        返回 [(part, attach_photo)]。

        为什么占位符只认第一条：模型偶尔会在两条里都写 ``{photo}``，但一次只挑得到一张图，
        第二条再带一次就成了重复发图。
        """
        prepared: list[tuple[ReplyPart, bool]] = []
        photo_used = False

        for part in parts:
            wants_photo = PHOTO_PLACEHOLDER in part.text
            text = part.text
            if wants_photo:
                # 占位符抠掉之后剩下的空格要收干净，否则会发出"晚饭  好了"这种缝
                text = re.sub(r"[ \t]{2,}", " ", text.replace(PHOTO_PLACEHOLDER, " ")).strip()
            attach = wants_photo and photo is not None and not photo_used

            if not text and not attach:
                # 占位符没挑到图，这条只剩空壳，丢掉
                continue

            chunks = _split_long(text) or [""]
            for i, chunk in enumerate(chunks):
                prepared.append(
                    (
                        ReplyPart(
                            text=chunk,
                            # 拆出来的后半段是同一口气说完的，中间不再停
                            pause_before_seconds=part.pause_before_seconds if i == 0 else 0.0,
                        ),
                        # 拆开之后图跟着最后一段走：先把话说完再出图，和"没占位符时图跟在最后"一致
                        attach and i == len(chunks) - 1,
                    )
                )
            photo_used = photo_used or attach

        if photo is not None and not photo_used:
            # 没人认领这张图：跟在最后单独发一条，不配字
            prepared.append((ReplyPart(text="", pause_before_seconds=0.0), True))
        return prepared

    # -- 投递 ---------------------------------------------------------------

    async def deliver(
        self,
        channel: Channel,
        parts: list[ReplyPart],
        photo: ResolvedPhoto | None,
        *,
        reaction: str | None = None,
        react_to: Reactable | None = None,
        interrupted: Callable[[], Awaitable[bool]] | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
        reply_to: Any = None,
        start_index: int = 0,
    ) -> DeliveryResult:
        """按顺序：先加表情反应（如果有），再逐条：pause_before → typing(时长由 timing.typing_duration) → send。
        每条发完后调用 interrupted()，为 True 就停止并标记 interrupted。任何一条发送异常：记录日志，
        已发的保留在结果里，然后抛出异常给上层决定。

        ``on_progress(index)`` 在每条真的发出去之后调用，上层用它把进度写进数据库；
        ``start_index`` 是重启后续发的起点，配合前者才能做到不重发。
        ``reply_to`` 只用在第一条气泡上——真人引用的是"他哪句话"，不是自己说的每一句。
        """
        result = DeliveryResult(next_index=start_index)
        prepared = self.prepare_parts(parts, photo)

        try:
            # 反应在文字之前：先"看到了"，再慢慢打字，顺序反了就像先写完才想起来点个赞
            if reaction and react_to is not None and start_index == 0:
                await self._react(react_to, reaction, result)

            for index in range(start_index, len(prepared)):
                part, attach = prepared[index]

                if part.pause_before_seconds > 0:
                    # 拿起手机之前的停顿，跟着 delay_scale 一起缩放，调试时才不用真等
                    await self.clock.sleep(part.pause_before_seconds * self.attention.delay_scale)

                if part.text:
                    await self._type(channel, part.text)

                message = await self._send(
                    channel,
                    part.text,
                    file=self.make_file(Path(photo.path)) if attach and photo else None,
                    # 引用只挂在第一条上；续发（start_index>0）时第一条早发过了
                    reference=reply_to if index == 0 else None,
                )

                if part.text:
                    result.sent_texts.append(part.text)
                if attach and photo:
                    result.photo_sent = photo
                message_id = getattr(message, "id", None)
                if isinstance(message_id, int):
                    result.sent_message_ids.append(message_id)
                result.next_index = index + 1

                if on_progress is not None:
                    await on_progress(index)

                # 最后一条发完就不用问了：这时候标 interrupted 只会让上层以为还有没发的
                if index < len(prepared) - 1 and interrupted is not None and await interrupted():
                    log.info("发到第 %d 条被打断，剩下的不发了", index + 1)
                    result.interrupted = True
                    break
        except Exception as exc:
            if _is_forbidden(exc):
                log.error("私聊被拒绝（%s）：%s", type(exc).__name__, BLOCKED_HINT)
                raise DeliveryBlocked(result) from exc
            # 网络抖动 / 5xx：交给调度器重试，但要让它知道已经发了几条
            log.warning("投递中断，已发出 %d 条：%s", result.next_index - start_index, exc)
            _attach_partial(exc, result)
            raise

        return result

    async def deliver_reply(
        self,
        channel: Channel,
        plan: ReplyPlan,
        photo: ResolvedPhoto | None,
        react_to: Reactable | None = None,
        interrupted: Callable[[], Awaitable[bool]] | None = None,
        *,
        reply_to: Any = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
        start_index: int = 0,
    ) -> DeliveryResult:
        """回复一批未读消息。``reply_to`` 是要引用的那条消息对象（有 ``.id``），不引用就传 None。"""
        return await self.deliver(
            channel,
            plan.parts,
            photo,
            reaction=plan.reaction,
            react_to=react_to,
            interrupted=interrupted,
            on_progress=on_progress,
            reply_to=reply_to,
            start_index=start_index,
        )

    async def deliver_proactive(
        self,
        channel: Channel,
        plan: ProactivePlan,
        photo: ResolvedPhoto | None,
        *,
        interrupted: Callable[[], Awaitable[bool]] | None = None,
        on_progress: Callable[[int], Awaitable[None]] | None = None,
        start_index: int = 0,
    ) -> DeliveryResult:
        """主动开口。没有反应、也不引用——她主动说事的时候不是在回谁的话。"""
        if not plan.send:
            # 模型自己觉得这会儿没必要说话，这不是错误
            return DeliveryResult()
        return await self.deliver(
            channel,
            plan.parts,
            photo,
            interrupted=interrupted,
            on_progress=on_progress,
            start_index=start_index,
        )

    # -- 细节 ---------------------------------------------------------------

    async def _react(self, react_to: Reactable, reaction: str, result: DeliveryResult) -> None:
        """加表情反应。加不上不影响后面的文字，除非是 403。

        表情是锦上添花：对方把那条消息删了、emoji 服务器不认，都不该让整条回复发不出去。
        但 403 说明这个会话本来就发不出去，得往上报。
        """
        try:
            await react_to.add_reaction(reaction)
        except Exception as exc:
            if _is_forbidden(exc):
                raise
            log.warning("表情反应 %s 加不上，跳过：%s", reaction, exc)
            return
        result.reacted = True

    async def _type(self, channel: Channel, text: str) -> None:
        """把"正在输入…"显示够整段打字时长，长句中途还会停下来想一下。"""
        total = self.attention.typing_duration(text, self.rng)

        if total > THINK_PAUSE_AFTER_SECONDS and self.rng.random() < THINK_PAUSE_PROBABILITY:
            first = total * self.rng.uniform(*THINK_PAUSE_SPLIT)
            await self._typing_for(channel, first)
            # 退出 typing 上下文，对方那边"正在输入"会消失几秒——这正是想要的效果
            await self.clock.sleep(self.rng.uniform(*THINK_PAUSE_RANGE) * self.attention.delay_scale)
            await self._typing_for(channel, total - first)
        else:
            await self._typing_for(channel, total)

    async def _typing_for(self, channel: Channel, seconds: float) -> None:
        """一段连续的打字。上下文管理器会自己续期，中途不用管。"""
        async with channel.typing():
            await self.clock.sleep(seconds)

    async def _send(
        self, channel: Channel, text: str, *, file: Any = None, reference: Any = None
    ) -> Any:
        """真正发出去。只传用得上的参数，免得假 channel 和老版本 discord.py 被多余的关键字噎住。"""
        kwargs: dict[str, Any] = {}
        if file is not None:
            kwargs["file"] = file
        if reference is not None:
            kwargs["reference"] = reference
        return await channel.send(text or None, **kwargs)
