"""大脑：所有 Claude 调用都在这里。

- 使用 ``anthropic.AsyncAnthropic``；模型/effort/max_tokens 来自 Settings。
- 结构化输出用 ``client.messages.parse(output_format=<pydantic>)``。
- 不传 ``thinking``（claude-opus-5 默认自适应思考）；传 ``output_config={"effort": ...}``。
- system 用列表形式并对稳定部分加 ``cache_control={"type": "ephemeral"}``。
- 错误处理链（DESIGN.md 2.8）：失败返回 None，由调用方决定重试；绝不向用户暴露错误。
- ``stop_reason == "refusal"`` → None。
- 所有调用记录 usage（input/cache_read/cache_creation/output tokens）到日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .config import Settings
from .models import (
    DayPlan,
    MemoryUpdate,
    PlanEvent,
    ProactivePlan,
    ReplyPlan,
    RhythmSnapshot,
    StoredMessage,
)
from .persona import Persona

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """为了测试可替换，只依赖这一个方法的形状（与 AsyncAnthropic.messages.parse 一致）。"""

    async def parse(self, **kwargs: Any) -> Any: ...


@dataclass
class ReplyContext:
    conversation_id: str
    now: datetime
    snapshot: RhythmSnapshot
    day_plan: DayPlan | None
    current_event: PlanEvent | None
    diary_notes: list[str]
    summary: str
    user_facts: list[str]
    self_facts: list[str]
    recent: list[StoredMessage]
    unread: list[StoredMessage]
    waited_minutes: float
    quick_before_sleep: bool
    available_photo_tags: list[str]
    images: list[tuple[str, bytes]] = field(default_factory=list)
    """对方发来的图片 [(media_type, bytes)]，作为 image block 传入。"""


@dataclass
class ProactiveContext:
    conversation_id: str
    now: datetime
    snapshot: RhythmSnapshot
    day_plan: DayPlan | None
    trigger_kind: str
    trigger_note: str
    diary_notes: list[str]
    summary: str
    user_facts: list[str]
    self_facts: list[str]
    recent: list[StoredMessage]
    hours_since_last_exchange: float | None
    available_photo_tags: list[str]


@dataclass
class DayPlanContext:
    day: datetime
    weekday_schedule_text: str
    yesterday_plan: DayPlan | None
    summary: str
    user_facts: list[str]
    self_facts: list[str]


@dataclass
class MemoryUpdateContext:
    previous_summary: str
    messages: list[StoredMessage]
    existing_user_facts: list[str]
    existing_self_facts: list[str]


class Brain:
    def __init__(self, client: Any, settings: Settings, persona: Persona) -> None:
        """client 是 ``anthropic.AsyncAnthropic()`` 实例（测试里传假对象，只需要 ``.messages.parse``）。"""
        self.client = client
        self.settings = settings
        self.persona = persona
        self._system_cache: str | None = None

    def system_blocks(self) -> list[dict[str, Any]]:
        """稳定 system 块列表（带 cache_control）。"""
        raise NotImplementedError

    async def _call(self, output_format: type[T], user_content: str | list[dict[str, Any]], *, purpose: str) -> T | None:
        """统一调用入口：组装参数、调用 parse、处理 refusal 与异常、记录 usage。"""
        raise NotImplementedError

    async def generate_reply(self, ctx: ReplyContext) -> ReplyPlan | None:
        raise NotImplementedError

    async def generate_proactive(self, ctx: ProactiveContext) -> ProactivePlan | None:
        raise NotImplementedError

    async def generate_day_plan(self, ctx: DayPlanContext) -> DayPlan | None:
        raise NotImplementedError

    async def update_memory(self, ctx: MemoryUpdateContext) -> MemoryUpdate | None:
        raise NotImplementedError
