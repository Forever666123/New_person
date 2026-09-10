"""跨模块共享的数据结构。

所有时间字段都是**带时区**的 ``datetime``；不带时区的时间只允许出现在 persona.yaml 的
``HH:MM`` 字符串里，由 ``rhythm`` 模块解释。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 作息 / 注意力
# ---------------------------------------------------------------------------

RhythmState = Literal["sleeping", "busy", "free", "winding_down"]
Heat = Literal["hot", "warm", "cold"]


PeriodKind = Literal["in_session", "finals", "break", "summer"]


class AcademicPeriod(BaseModel):
    """学期日历上的一段：上课、期末周、假期、暑假。"""

    name: str
    kind: PeriodKind
    start: date
    end: date
    """含当天。"""
    activity_multiplier: float = 1.0
    engage_multiplier: float = 1.0
    sleep_bonus_hours: float = 0.0
    """这段时间平均多睡多久。假期会睡得多一些。"""
    note: str = ""
    travel: Literal["none", "short", "long"] = "none"
    """这段假期有没有可能出门，出远门还是近门。"""

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class Trip(BaseModel):
    """一次出行。旅行期间她人在别的时区，作息和话题都会跟着变。"""

    start: date
    end: date
    place: str
    timezone: str
    note: str = ""
    kind: Literal["short", "long"] = "short"
    activity_multiplier: float = 1.0
    """在外面玩的时候看手机更少。住下来的长途影响小一些。"""

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class ClassInstance(BaseModel):
    """某一天真的发生了的一节课。"""

    start: datetime
    end: datetime
    title: str


class DailyRhythm(BaseModel):
    """某一天抽签抽出来的作息。同一天同一 seed 抽出来的结果永远相同。"""

    day: date
    period: str = ""
    """学期日历上的哪一段。"""
    period_kind: PeriodKind = "in_session"
    trip: Trip | None = None
    """今天在不在外面。"""
    timezone: str = ""
    """当天她所在的时区。旅行时会变。"""
    phase: str = "平常"
    phase_note: str = ""
    variant: str = ""
    variant_note: str = ""
    wake: datetime
    """这一天早上起床的时刻。"""
    sleep_start: datetime
    """这一天晚上入睡的时刻（通常落在次日凌晨）。"""
    activity_multiplier: float = 1.0
    engage_probability: float = 0.85
    classes: list[ClassInstance] = Field(default_factory=list)

    period_note: str = ""

    @property
    def mood_notes(self) -> list[str]:
        """今天注入上下文的心态提示，从大到小：学期、出行、阶段、当日。"""
        notes = [self.period_note, self.phase_note, self.variant_note]
        if self.trip and self.trip.note:
            notes.insert(1, self.trip.note)
        return [n for n in notes if n]


class RhythmSnapshot(BaseModel):
    """某一时刻的作息快照。"""

    state: RhythmState
    at: datetime
    until: datetime
    """当前状态到什么时候结束。"""
    next_wake: datetime
    next_sleep: datetime
    activity: float
    """此刻"会看手机"的活跃度，0 到 1。"""
    period: str = ""
    phase: str = ""
    variant: str = ""
    trip_place: str = ""
    """在外面的话，人在哪。"""
    mood_notes: list[str] = Field(default_factory=list)
    block_title: str | None = None
    """当前 busy 区间的标题，非 busy 时为 None。"""


class MessageFeatures(BaseModel):
    """从一批来信里提取的、影响回复时机的特征。"""

    is_question: bool = False
    is_urgent: bool = False
    length: int = 0
    has_image: bool = False
    mode: str | None = None
    """命中的话题模式名，如 trading。"""


class TimingDecision(BaseModel):
    """回复时机决定。"""

    notice_at: datetime
    """人物"看到"消息的时间。"""
    reply_at: datetime
    """人物开始回复（开始打字）的时间。"""
    reason: str
    """人类可读的解释，写进日志方便调参。"""
    defers: int = 0
    """看到了先放着的次数。真人常有的"待会儿再回"。"""
    quick_before_sleep: bool = False
    hints: list[str] = Field(default_factory=list)
    """给模型的处境提示，进上下文，不会直接发出去。"""


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    url: str
    filename: str
    content_type: str | None = None
    size: int = 0
    local_path: str | None = None


class IncomingMessage(BaseModel):
    id: int | None = None
    conversation_id: str
    discord_message_id: int
    author_id: int
    author_name: str = ""
    content: str
    attachments: list[Attachment] = Field(default_factory=list)
    created_at: datetime


class StoredMessage(BaseModel):
    id: int
    conversation_id: str
    discord_message_id: int | None = None
    """Discord 那边的消息 id。加表情反应和引用回复都要用它。"""
    author_kind: Literal["user", "bot"]
    author_id: int
    author_name: str = ""
    content: str
    attachments: list[Attachment] = Field(default_factory=list)
    created_at: datetime
    read_at: datetime | None = None


class ConversationState(BaseModel):
    id: str
    kind: Literal["dm", "channel"] = "dm"
    last_user_message_at: datetime | None = None
    last_bot_message_at: datetime | None = None
    pending_reply_job_id: int | None = None
    summary: str = ""
    summary_upto_message_id: int = 0
    unanswered_initiations: int = 0
    last_initiation_date: date | None = None
    deliverable: bool = True
    hot_session_started_at: datetime | None = None


# ---------------------------------------------------------------------------
# 模型输出（结构化）
# ---------------------------------------------------------------------------


class LedgerEntry(BaseModel):
    """对方说过的、以后可能要拿来对质的一句话。主要用于交易纪律。"""

    kind: str = Field(
        default="trading",
        description=(
            "台账类型，从这几个里挑：trading（仓位、止损、回测）、study（课业、考试、deadline）、"
            "shift（便利店排班）、project（他在写的东西）、english（英语练习）、sleep（作息）。"
        ),
    )
    claim: str = Field(description="他说了什么。用他自己的话。")
    reason: str = Field(default="", description="他给的理由，如果有。")
    committed_to: str = Field(default="", description="他答应要做的事，如果有。")


class ReplyPart(BaseModel):
    """一条聊天气泡。"""

    text: str = Field(description="这一条的文字。短。可以包含 {photo} 占位符表示这条配图。")
    pause_before_seconds: float = Field(
        default=0.0, ge=0.0, le=180.0, description="发这条之前先停几秒。通常 0 到 10。"
    )


class PhotoRequest(BaseModel):
    """想发一张照片。优先用 photo_id 从可用照片列表里选。"""

    photo_id: str | None = Field(default=None, description="从上下文给出的可用照片列表里选一个 id。")
    tags: list[str] = Field(default_factory=list, description="没有合适 id 时用标签描述想发什么。")
    description: str = ""


class FollowUp(BaseModel):
    """稍后要主动跟进的事。"""

    delay_minutes: int = Field(ge=1, le=60 * 24 * 7)
    note: str = Field(description="到时候要说什么。")


class ReplyPlan(BaseModel):
    """对一批未读消息的回复方案。"""

    parts: list[ReplyPart] = Field(
        default_factory=list, description="要发的气泡，按顺序。可以为空，表示这次不回。"
    )
    reaction: str | None = Field(default=None, description="给对方最后一条消息加的 emoji 反应。不需要就 null。")
    reply_to_index: int | None = Field(
        default=None, description="如果这次回复是针对未读消息里的某一条，给出它的序号（从 0 开始），会用引用回复。"
    )
    photo_request: PhotoRequest | None = None
    follow_up: FollowUp | None = None
    ledger_entries: list[LedgerEntry] = Field(
        default_factory=list, description="从对方这次说的话里记下来的、以后要拿来对质的陈述。"
    )
    resolved_ledger_ids: list[int] = Field(
        default_factory=list,
        description=(
            "他这次给了下文的台账条目编号（上下文里 [#12] 的那个数）。"
            "做了、没做、改主意了都算。放进来之后就不会再追问它。"
        ),
    )
    inner_note: str = Field(default="", description="一句话记下你此刻的状态，进你的日记，不发给对方。")


class ProactivePlan(BaseModel):
    """主动发起的一次消息。"""

    send: bool = Field(description="现在到底要不要发。觉得没必要就 false。")
    parts: list[ReplyPart] = Field(default_factory=list)
    photo_request: PhotoRequest | None = None
    inner_note: str = ""


class PlanEvent(BaseModel):
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM")
    title: str
    detail: str = Field(description="一两句细节，之后聊天时可以引用。")
    shareable: bool = Field(description="这件事值不值得主动跟对方提。")
    share_hint: str | None = None
    photo_tags: list[str] = Field(default_factory=list)


class DayPlan(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    mood: str = Field(description="今天的状态，一句话。")
    events: list[PlanEvent] = Field(default_factory=list)
    thoughts: list[str] = Field(default_factory=list, description="今天心里挂着的一两件事。")


class MemoryUpdate(BaseModel):
    """对话摘要与事实抽取的结果。"""

    summary: str = Field(description="到目前为止这段对话的滚动摘要，第三人称，包含约定和没聊完的话题。")
    owner_facts: list[str] = Field(default_factory=list, description="关于对方的新的稳定事实。")
    self_facts: list[str] = Field(default_factory=list, description="你自己说过的、以后要保持一致的事。")


# ---------------------------------------------------------------------------
# 任务队列
# ---------------------------------------------------------------------------

JobKind = Literal["reply", "proactive", "follow_up", "day_plan", "memory_update", "sign_off"]
JobStatus = Literal["pending", "running", "done", "failed", "cancelled"]


class Job(BaseModel):
    id: int | None = None
    kind: JobKind
    run_at: datetime
    conversation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = "pending"
    attempts: int = 0
    dedupe_key: str | None = None
    lease_until: datetime | None = None
    progress: dict[str, Any] = Field(default_factory=dict)
    covers_upto_message_id: int = 0
    original_run_at: datetime | None = None
    reason: str = ""
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# 照片
# ---------------------------------------------------------------------------

TimeOfDay = Literal["any", "morning", "day", "evening", "night"]


class Photo(BaseModel):
    id: str
    file: str
    tags: list[str] = Field(default_factory=list)
    caption: str = ""
    taken_hint: str = ""
    time_of_day: TimeOfDay = "any"
    location: str = ""
    freshness: Literal["evergreen", "dated"] = "evergreen"


class ResolvedPhoto(BaseModel):
    path: str
    photo_id: str | None = None
    caption: str = ""
    is_fresh: bool = True
    """True 表示可以说成"刚拍的"。"""


# ---------------------------------------------------------------------------
# 风格检查
# ---------------------------------------------------------------------------


class StyleViolation(BaseModel):
    kind: Literal[
        "banned_phrase", "emoji", "exclamation", "full_english", "too_long", "too_many_parts", "trailing_period"
    ]
    detail: str
    part_index: int = 0
    fixable: bool = True
    """True 表示可以机械修掉；False 表示需要让模型重写。"""


class UsageRecord(BaseModel):
    day: date
    calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: float = 0.0
