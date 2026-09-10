"""投递：把 ReplyPlan / ProactivePlan 变成 Discord 上的一串消息。见 DESIGN.md 2.4。

不直接依赖 discord.py 的具体类，只依赖下面的 Protocol，方便测试。
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .clock import Clock
from .models import ProactivePlan, ReplyPart, ReplyPlan, ResolvedPhoto
from .timing import ReplyTimingPolicy

DISCORD_MAX_LEN = 2000


class SentMessage(Protocol):
    id: int


class Channel(Protocol):
    def typing(self) -> Any:
        """async context manager，显示"正在输入…"。"""
        ...

    async def send(self, content: str | None = None, *, file: Any = None) -> SentMessage: ...


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


class Deliverer:
    def __init__(self, clock: Clock, timing: ReplyTimingPolicy, rng: random.Random, make_file: Callable[[Path], Any]) -> None:
        """make_file: 把本地路径变成 discord.File（测试里传 lambda p: p）。"""
        self.clock = clock
        self.timing = timing
        self.rng = rng
        self.make_file = make_file

    def prepare_parts(self, parts: list[ReplyPart], photo: ResolvedPhoto | None) -> list[tuple[ReplyPart, bool]]:
        """处理 {photo} 占位：有图 → 该条带图发送（去掉占位符）；无图 → 去掉占位符，若去掉后为空则丢弃该条。
        没有任何一条含占位但有图 → 图跟在最后一条后面单独发。超过 2000 字的气泡拆分。
        返回 [(part, attach_photo)]。"""
        raise NotImplementedError

    async def deliver(
        self,
        channel: Channel,
        parts: list[ReplyPart],
        photo: ResolvedPhoto | None,
        *,
        reaction: str | None = None,
        react_to: Reactable | None = None,
        interrupted: Callable[[], Awaitable[bool]] | None = None,
    ) -> DeliveryResult:
        """按顺序：先加表情反应（如果有），再逐条：pause_before → typing(时长由 timing.typing_duration) → send。
        每条发完后调用 interrupted()，为 True 就停止并标记 interrupted。任何一条发送异常：记录日志，
        已发的保留在结果里，然后抛出异常给上层决定。"""
        raise NotImplementedError

    async def deliver_reply(self, channel: Channel, plan: ReplyPlan, photo: ResolvedPhoto | None,
                            react_to: Reactable | None, interrupted: Callable[[], Awaitable[bool]] | None) -> DeliveryResult:
        raise NotImplementedError

    async def deliver_proactive(self, channel: Channel, plan: ProactivePlan, photo: ResolvedPhoto | None) -> DeliveryResult:
        raise NotImplementedError
