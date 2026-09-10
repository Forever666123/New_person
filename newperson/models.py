"""跨模块共享的数据结构。

所有时间字段都是**带时区**的 ``datetime``；不带时区的时间只允许出现在 persona.yaml 的
"HH:MM" 字符串里，由 ``rhythm`` 模块解释。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 作息 / 时机
# ---------------------------------------------------------------------------

RhythmState = Literal["sleeping", "busy", "free", "winding_down"]
Heat = Literal["hot", "warm", "cold"]


class RhythmSnapshot(BaseModel):
    """某一时刻的作息快照。"""

    state: RhythmState
    since: datetime
    """当前状态从什么时候开始。"""
    until: datetime
    """当前状态到什么时候结束（下一次状态切换）。"""
    next_wake: datetime
    """下一次起床时间（如果现在没睡，就是下一次睡醒的时间）。"""
    next_sleep: datetime
    """下一次入睡时间（如果现在在睡，就是下一次入睡的时间）。"""
    block_title: str | None = None
    """当前 busy 区间的标题（如"上班"），非 busy 时为 None。"""


class MessageFeatures(BaseModel):
    """从一条（或一批）来信里提取的、影响回复时机的特征。"""

    is_question: bool = False
    is_urgent: bool = False
    length: int = 0
    has_image: bool = False


class TimingDecision(BaseModel):
    """回复时机决定。"""

    notice_at: datetime
    """人物"看到"消息的时间。"""
    reply_at: datetime
    """人物开始回复（开始打字）的时间。"""
    reason: str
    """人类可读的解释，写进日志，方便调参（如 "busy/warm: 中位数15min，采样到 22min"）。"""
    quick_before_sleep: bool = False
    """是否是"刚准备睡，最后回一句"的快速回复。"""


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    url: str
    filename: str
    content_type: str | None = None
    size: int = 0
    local_path: str | None = None
    """下载到本地后的路径（可能为空：太大或不是图片）。"""


class IncomingMessage(BaseModel):
    """对方发来的一条消息（已存库）。"""

    id: int | None = None
    """数据库主键。"""
    conversation_id: str
    discord_message_id: int
    author_id: int
    author_name: str = ""
    content: str
    attachments: list[Attachment] = Field(default_factory=list)
    created_at: datetime


class StoredMessage(BaseModel):
    """历史记录里的一条消息（双方都有）。"""

    id: int
    conversation_id: str
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


# ---------------------------------------------------------------------------
# 模型输出（结构化）
# ---------------------------------------------------------------------------


class ReplyPart(BaseModel):
    """一条聊天气泡。"""

    text: str = Field(description="这一条气泡的文字。像真人发 IM 一样短。可以包含 {photo} 占位符表示这条是配图说明。")
    pause_before_seconds: float = Field(
        default=0.0, ge=0.0, le=120.0, description="发这条之前先停顿几秒（在想、在做别的事）。通常 0～10。"
    )


class PhotoRequest(BaseModel):
    """想发一张照片。"""

    tags: list[str] = Field(description="照片标签，如 ['food','lunch']，用来在照片库里挑。")
    description: str = Field(description="这张照片大概是什么内容（如果需要生成图片会用到）。")


class FollowUp(BaseModel):
    """稍后要主动跟进的事。"""

    delay_minutes: int = Field(ge=1, le=60 * 24 * 3)
    note: str = Field(description="到时候提醒自己要说什么/做什么。")


class ReplyPlan(BaseModel):
    """对一批未读消息的回复方案。"""

    parts: list[ReplyPart] = Field(
        default_factory=list,
        description="要发的气泡列表，按顺序。可以为空（只加表情反应，或者干脆不回）。",
    )
    reaction: str | None = Field(
        default=None, description="给对方最后一条消息加的 emoji 反应，例如 '😂'。不需要就 null。"
    )
    photo_request: PhotoRequest | None = Field(default=None, description="要发的照片；没有就 null。")
    follow_up: FollowUp | None = Field(default=None, description="需要稍后主动跟进的事；没有就 null。")
    inner_note: str = Field(
        default="",
        description="一句话记下你此刻的心情/状态，会写进你的日记，不会发给对方。",
    )


class ProactivePlan(BaseModel):
    """主动发起的一次消息。"""

    send: bool = Field(description="现在到底要不要发。觉得没必要就 false。")
    parts: list[ReplyPart] = Field(default_factory=list)
    photo_request: PhotoRequest | None = None
    inner_note: str = ""


class PlanEvent(BaseModel):
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM")
    title: str = Field(description="简短标题，如 '上班'、'健身'、'和朋友吃饭'。也会显示为 Discord 自定义状态。")
    detail: str = Field(description="一两句细节，供之后聊天时引用。")
    shareable: bool = Field(description="这件事值不值得主动跟对方分享。")
    share_hint: str | None = Field(default=None, description="如果分享，大概说什么。")
    photo_tags: list[str] = Field(default_factory=list, description="如果想配图，图的标签。")


class DayPlan(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    mood: str = Field(description="今天的整体心情/状态，一句话。")
    events: list[PlanEvent] = Field(default_factory=list)
    thoughts: list[str] = Field(default_factory=list, description="今天心里挂念的两三件事。")


class MemoryUpdate(BaseModel):
    """对话摘要与事实抽取的结果。"""

    summary: str = Field(description="到目前为止这段对话的滚动摘要，第三人称，包含约定和未完成的话题。")
    user_facts: list[str] = Field(default_factory=list, description="关于对方的新的稳定事实。")
    self_facts: list[str] = Field(default_factory=list, description="关于人物自己在对话里说过的、以后要保持一致的事实。")


# ---------------------------------------------------------------------------
# 任务队列
# ---------------------------------------------------------------------------

JobKind = Literal["reply", "proactive", "follow_up", "day_plan", "memory_update", "presence"]
JobStatus = Literal["pending", "running", "done", "failed", "cancelled"]


class Job(BaseModel):
    id: int | None = None
    kind: JobKind
    run_at: datetime
    conversation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus = "pending"
    attempts: int = 0
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# 照片
# ---------------------------------------------------------------------------


class Photo(BaseModel):
    id: str
    file: str
    tags: list[str] = Field(default_factory=list)
    caption: str = ""
    taken_hint: str = ""
    """给模型看的提示，如"去年秋天在公园拍的"。"""


class ResolvedPhoto(BaseModel):
    path: str
    photo_id: str | None = None
    """来自照片库时有 id；临时生成的没有。"""
    caption: str = ""
