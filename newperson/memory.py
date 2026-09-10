"""SQLite 持久化：消息、会话、事实、日记、任务队列、照片使用记录、KV。

使用 aiosqlite；单写者，开启 WAL；所有 datetime 以 ISO8601（带时区）字符串存储。
表结构见 DESIGN.md 2.7。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import (
    ConversationState,
    DayPlan,
    IncomingMessage,
    Job,
    JobKind,
    JobStatus,
    StoredMessage,
)


class Memory:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def open(self) -> None:
        """打开连接、建表（幂等）。"""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    # ---- 消息 ----
    async def add_user_message(self, msg: IncomingMessage) -> int:
        """存一条对方消息，返回主键；同时更新 conversations.last_user_message_at。重复的 discord_message_id 忽略并返回已有 id。"""
        raise NotImplementedError

    async def add_bot_message(self, conversation_id: str, content: str, created_at: datetime,
                              discord_message_id: int | None = None, attachments: list[dict[str, Any]] | None = None) -> int:
        """存一条人物自己发的消息；更新 last_bot_message_at。"""
        raise NotImplementedError

    async def unread_messages(self, conversation_id: str) -> list[StoredMessage]:
        """该会话所有 read_at 为空的对方消息，按时间升序。"""
        raise NotImplementedError

    async def mark_read(self, message_ids: list[int], read_at: datetime) -> None:
        raise NotImplementedError

    async def recent_messages(self, conversation_id: str, limit: int, after_id: int = 0) -> list[StoredMessage]:
        """最近 limit 条（双方），按时间升序返回；after_id 用于只取摘要之后的消息。"""
        raise NotImplementedError

    async def count_messages_after(self, conversation_id: str, after_id: int) -> int:
        raise NotImplementedError

    async def last_user_message_at_after(self, conversation_id: str, since: datetime) -> datetime | None:
        """since 之后对方最后一条消息时间；没有返回 None。用于投递中的"被打断"检测。"""
        raise NotImplementedError

    # ---- 会话 ----
    async def get_conversation(self, conversation_id: str) -> ConversationState:
        """不存在则创建（kind 默认 dm）。"""
        raise NotImplementedError

    async def update_conversation(self, conversation_id: str, **fields: Any) -> None:
        raise NotImplementedError

    async def list_conversations(self) -> list[ConversationState]:
        raise NotImplementedError

    # ---- 事实 ----
    async def add_facts(self, subject: str, facts: list[str], source_message_id: int | None = None) -> None:
        raise NotImplementedError

    async def facts(self, subject: str | None = None, limit: int = 100) -> list[tuple[str, str]]:
        """返回 [(subject, fact)]，未被 superseded 的，按时间升序。"""
        raise NotImplementedError

    # ---- 日记 ----
    async def get_day_plan(self, day: date) -> DayPlan | None:
        raise NotImplementedError

    async def save_day_plan(self, day: date, plan: DayPlan) -> None:
        raise NotImplementedError

    async def add_diary_note(self, day: date, note: str, at: datetime) -> None:
        """追加一条当天的日记（如 inner_note、"主动发了午饭照片"）。"""
        raise NotImplementedError

    async def diary_notes(self, day: date) -> list[tuple[datetime, str]]:
        raise NotImplementedError

    # ---- 任务队列 ----
    async def add_job(self, job: Job) -> int:
        raise NotImplementedError

    async def get_job(self, job_id: int) -> Job | None:
        raise NotImplementedError

    async def due_jobs(self, now: datetime, limit: int = 20) -> list[Job]:
        """status=pending 且 run_at<=now，按 run_at 升序。"""
        raise NotImplementedError

    async def next_job_run_at(self) -> datetime | None:
        """最早的 pending 任务时间，供调度器睡到那一刻。"""
        raise NotImplementedError

    async def set_job_status(self, job_id: int, status: JobStatus, attempts: int | None = None) -> None:
        raise NotImplementedError

    async def reschedule_job(self, job_id: int, run_at: datetime, payload: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def pending_jobs(self, kind: JobKind | None = None, conversation_id: str | None = None) -> list[Job]:
        raise NotImplementedError

    async def reset_running_jobs(self) -> int:
        """启动时把上次崩溃遗留的 running 任务改回 pending，返回条数。"""
        raise NotImplementedError

    # ---- 照片 ----
    async def used_photo_ids(self, conversation_id: str | None = None) -> set[str]:
        raise NotImplementedError

    async def mark_photo_used(self, photo_id: str, conversation_id: str, at: datetime) -> None:
        raise NotImplementedError

    # ---- KV ----
    async def kv_get(self, key: str) -> str | None:
        raise NotImplementedError

    async def kv_set(self, key: str, value: str) -> None:
        raise NotImplementedError
