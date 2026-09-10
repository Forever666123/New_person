"""SQLite 持久化：消息、会话、事实、台账、日记、任务、照片、用量。

几个设计决定：

- **单连接 + WAL**。整个进程一个 ``aiosqlite`` 连接，所有查询在它的工作线程里排队，
  不会阻塞 Discord 的心跳。所有读-改-写走 ``BEGIN IMMEDIATE`` 事务。
- **时间存 ISO8601 带时区偏移的字符串**。SQLite 没有时间类型，字符串比较即时间顺序。
- **``discord_message_id`` 唯一**。网关会重发事件，插重了就当没发生。
- **事实会淡**。``facts`` 带 ``strength``，随时间衰减，被提起时重新增强。
  想不起来的事就不带进上下文。这是故意的：一个什么都记得的人，聊天就没意思了。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    Attachment,
    ConversationState,
    DayPlan,
    IncomingMessage,
    Job,
    JobKind,
    JobStatus,
    LedgerEntry,
    StoredMessage,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    discord_message_id INTEGER UNIQUE,
    author_kind TEXT NOT NULL,
    author_id INTEGER NOT NULL DEFAULT 0,
    author_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    read_at TEXT,
    edited_at TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(conversation_id, read_at);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'dm',
    last_user_message_at TEXT,
    last_bot_message_at TEXT,
    pending_reply_job_id INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    summary_upto_message_id INTEGER NOT NULL DEFAULT 0,
    unanswered_initiations INTEGER NOT NULL DEFAULT 0,
    last_initiation_date TEXT,
    deliverable INTEGER NOT NULL DEFAULT 1,
    hot_session_started_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    fact TEXT NOT NULL,
    source_message_id INTEGER,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 1.0,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject, superseded);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'trading',
    claim TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    committed_to TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger(kind, resolved);

CREATE TABLE IF NOT EXISTS diary (
    day TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    day_plan_json TEXT,
    notes_json TEXT NOT NULL DEFAULT '[]',
    claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    run_at TEXT NOT NULL,
    conversation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    covers_upto_message_id INTEGER NOT NULL DEFAULT 0,
    original_run_at TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS photo_usage (
    photo_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    framed_as_fresh INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_photo_usage ON photo_usage(photo_id, sent_at);

CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class Memory:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        """读-改-写要串起来。

        Discord 事件回调、调度器任务、presence 循环是三条并发的协程共用一条连接。
        aiosqlite 只保证单条语句排队，不保证事务边界，任何一方的 commit
        都会把别人写到一半的东西提交掉。结果是日记或事实偶尔少一条，无迹可查。
        """

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Memory 还没 open()")
        return self._db

    async def open(self) -> None:
        """打开连接、建表。幂等。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
            "PRAGMA busy_timeout=5000;"
            "PRAGMA foreign_keys=ON;"
        )
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Memory:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- 消息 -------------------------------------------------------------

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> StoredMessage:
        return StoredMessage(
            id=row["id"],
            conversation_id=row["conversation_id"],
            discord_message_id=row["discord_message_id"],
            author_kind=row["author_kind"],
            author_id=row["author_id"],
            author_name=row["author_name"],
            content=row["content"],
            attachments=[Attachment(**a) for a in json.loads(row["attachments_json"])],
            created_at=datetime.fromisoformat(row["created_at"]),
            read_at=parse_dt(row["read_at"]),
        )

    async def add_user_message(self, msg: IncomingMessage) -> int:
        """存一条对方的消息。网关重发导致的重复插入会被忽略，返回已有的 id。"""
        await self.get_conversation(msg.conversation_id)
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO messages"
            " (conversation_id, discord_message_id, author_kind, author_id, author_name,"
            "  content, attachments_json, created_at)"
            " VALUES (?, ?, 'user', ?, ?, ?, ?, ?)",
            (
                msg.conversation_id,
                msg.discord_message_id,
                msg.author_id,
                msg.author_name,
                msg.content,
                json.dumps([a.model_dump() for a in msg.attachments], ensure_ascii=False),
                msg.created_at.isoformat(),
            ),
        )
        if cur.rowcount == 0:  # 重复事件，什么都不做
            row = await self._fetch_one(
                "SELECT id FROM messages WHERE discord_message_id = ?", (msg.discord_message_id,)
            )
            await self.db.commit()
            return int(row["id"]) if row else 0

        await self.db.execute(
            "UPDATE conversations SET last_user_message_at = ?,"
            " unanswered_initiations = 0 WHERE id = ?",
            (msg.created_at.isoformat(), msg.conversation_id),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def add_bot_message(
        self,
        conversation_id: str,
        content: str,
        created_at: datetime,
        discord_message_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> int:
        await self.get_conversation(conversation_id)
        # 用 OR IGNORE：消息已经真的发到对方手机上了，这里再因为主键冲突抛异常
        # 会让整个回复任务失败重试，后面的日记、台账、跟进全部跳过。
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO messages"
            " (conversation_id, discord_message_id, author_kind, content, attachments_json, created_at)"
            " VALUES (?, ?, 'bot', ?, ?, ?)",
            (
                conversation_id,
                discord_message_id,
                content,
                json.dumps(attachments or [], ensure_ascii=False),
                created_at.isoformat(),
            ),
        )
        await self.db.execute(
            "UPDATE conversations SET last_bot_message_at = ? WHERE id = ?",
            (created_at.isoformat(), conversation_id),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def unread_messages(self, conversation_id: str) -> list[StoredMessage]:
        rows = await self._fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? AND author_kind = 'user'"
            " AND read_at IS NULL AND deleted = 0 ORDER BY id",
            (conversation_id,),
        )
        return [self._row_to_message(r) for r in rows]

    async def mark_read(self, message_ids: list[int], read_at: datetime) -> None:
        if not message_ids:
            return
        marks = ",".join("?" * len(message_ids))
        await self.db.execute(
            f"UPDATE messages SET read_at = ? WHERE id IN ({marks})",  # noqa: S608
            (read_at.isoformat(), *message_ids),
        )
        await self.db.commit()

    async def recent_messages(
        self, conversation_id: str, limit: int, after_id: int = 0
    ) -> list[StoredMessage]:
        """最近 limit 条（双方），按时间升序返回。"""
        rows = await self._fetch_all(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? AND id > ?"
            " AND deleted = 0 ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conversation_id, after_id, limit),
        )
        return [self._row_to_message(r) for r in rows]

    async def count_messages_after(self, conversation_id: str, after_id: int) -> int:
        row = await self._fetch_one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND id > ?",
            (conversation_id, after_id),
        )
        return int(row["n"]) if row else 0

    async def max_unread_id(self, conversation_id: str) -> int:
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(id), 0) AS n FROM messages WHERE conversation_id = ?"
            " AND author_kind = 'user' AND read_at IS NULL",
            (conversation_id,),
        )
        return int(row["n"]) if row else 0

    async def last_exchange_before(
        self, conversation_id: str, before_id: int
    ) -> datetime | None:
        """在这条消息之前，双方最后一次说话是什么时候。

        热度问的是"这批消息到来之前对话有多热"。不能拿会话表上的
        ``last_user_message_at``：那个字段在消息入库时就被刚收到的这条更新了，
        算出来的间隔永远是 0 秒，于是永远判定成正在热聊，她就永远秒回。
        """
        row = await self._fetch_one(
            "SELECT created_at FROM messages WHERE conversation_id = ? AND id < ?"
            " AND deleted = 0 ORDER BY id DESC LIMIT 1",
            (conversation_id, before_id),
        )
        return parse_dt(row["created_at"]) if row else None

    async def restore_unread(self, conversation_id: str, upto_id: int) -> int:
        """把一批消息放回未读。

        存下来的回复读不出来时用：那批消息已经标成已读了，
        不放回去的话它们就再也不会被回复，而且没有任何征兆。
        """
        cur = await self.db.execute(
            "UPDATE messages SET read_at = NULL WHERE conversation_id = ?"
            " AND author_kind = 'user' AND id <= ? AND read_at IS NOT NULL",
            (conversation_id, upto_id),
        )
        await self.db.commit()
        return cur.rowcount

    async def has_newer_user_message(self, conversation_id: str, after_id: int) -> bool:
        """投递到一半检查对方有没有又发新的。"""
        row = await self._fetch_one(
            "SELECT 1 AS x FROM messages WHERE conversation_id = ? AND author_kind = 'user'"
            " AND id > ? LIMIT 1",
            (conversation_id, after_id),
        )
        return row is not None

    async def edit_message(self, discord_message_id: int, content: str, at: datetime) -> None:
        await self.db.execute(
            "UPDATE messages SET content = ?, edited_at = ? WHERE discord_message_id = ?",
            (content, at.isoformat(), discord_message_id),
        )
        await self.db.commit()

    async def delete_message(self, discord_message_id: int) -> None:
        await self.db.execute(
            "UPDATE messages SET deleted = 1 WHERE discord_message_id = ?", (discord_message_id,)
        )
        await self.db.commit()

    # ---- 会话 -------------------------------------------------------------

    async def get_conversation(self, conversation_id: str) -> ConversationState:
        row = await self._fetch_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO conversations (id) VALUES (?)", (conversation_id,)
            )
            await self.db.commit()
            return ConversationState(id=conversation_id)
        return ConversationState(
            id=row["id"],
            kind=row["kind"],
            last_user_message_at=parse_dt(row["last_user_message_at"]),
            last_bot_message_at=parse_dt(row["last_bot_message_at"]),
            pending_reply_job_id=row["pending_reply_job_id"],
            summary=row["summary"],
            summary_upto_message_id=row["summary_upto_message_id"],
            unanswered_initiations=row["unanswered_initiations"],
            last_initiation_date=date.fromisoformat(row["last_initiation_date"])
            if row["last_initiation_date"]
            else None,
            deliverable=bool(row["deliverable"]),
            hot_session_started_at=parse_dt(row["hot_session_started_at"]),
        )

    async def update_conversation(self, conversation_id: str, **fields: Any) -> None:
        if not fields:
            return
        await self.get_conversation(conversation_id)
        allowed = {
            "kind",
            "last_user_message_at",
            "last_bot_message_at",
            "pending_reply_job_id",
            "summary",
            "summary_upto_message_id",
            "unanswered_initiations",
            "last_initiation_date",
            "deliverable",
            "hot_session_started_at",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"conversations 没有 {key} 这一列")
            sets.append(f"{key} = ?")
            if isinstance(value, datetime | date):
                value = value.isoformat()
            elif isinstance(value, bool):
                value = int(value)
            values.append(value)
        values.append(conversation_id)
        await self.db.execute(
            f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", values  # noqa: S608
        )
        await self.db.commit()

    async def list_conversations(self) -> list[ConversationState]:
        rows = await self._fetch_all("SELECT id FROM conversations ORDER BY id", ())
        return [await self.get_conversation(r["id"]) for r in rows]

    # ---- 事实（会淡） -----------------------------------------------------

    async def add_facts(
        self,
        subject: str,
        facts: list[str],
        at: datetime,
        source_message_id: int | None = None,
    ) -> None:
        """记下新事实。已经记过的只是重新变清晰，不重复插入。"""
        async with self._write_lock:
            await self._add_facts(subject, facts, at, source_message_id)

    async def _add_facts(
        self,
        subject: str,
        facts: list[str],
        at: datetime,
        source_message_id: int | None = None,
    ) -> None:
        for fact in facts:
            text = fact.strip()
            if not text:
                continue
            row = await self._fetch_one(
                "SELECT id FROM facts WHERE subject = ? AND fact = ? AND superseded = 0",
                (subject, text),
            )
            if row is not None:
                await self.db.execute(
                    "UPDATE facts SET strength = MIN(1.0, strength + 0.4), last_seen_at = ?"
                    " WHERE id = ?",
                    (at.isoformat(), row["id"]),
                )
                continue
            await self.db.execute(
                "INSERT INTO facts (subject, fact, source_message_id, created_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (subject, text, source_message_id, at.isoformat(), at.isoformat()),
            )
        await self.db.commit()

    async def recall_facts(
        self,
        subject: str | None,
        now: datetime,
        half_life_days: float,
        threshold: float,
        limit: int = 60,
    ) -> list[str]:
        """她现在还记得的事。

        太久没提起的会淡到想不起来，就不带进上下文了。记不清她会直接问。
        """
        if subject:
            rows = await self._fetch_all(
                "SELECT fact, last_seen_at, strength FROM facts"
                " WHERE subject = ? AND superseded = 0",
                (subject,),
            )
        else:
            rows = await self._fetch_all(
                "SELECT fact, last_seen_at, strength FROM facts WHERE superseded = 0", ()
            )

        remembered: list[tuple[float, str]] = []
        for row in rows:
            last_seen = datetime.fromisoformat(row["last_seen_at"])
            days = max(0.0, (now - last_seen).total_seconds() / 86400)
            decayed = row["strength"] * math.pow(0.5, days / max(half_life_days, 0.1))
            if decayed >= threshold:
                remembered.append((decayed, row["fact"]))
        remembered.sort(key=lambda x: -x[0])
        return [fact for _, fact in remembered[:limit]]

    async def all_facts(self, subject: str | None = None) -> list[tuple[str, str]]:
        """不做衰减的全量，给 ``!np status`` 之类的排查用。"""
        if subject:
            rows = await self._fetch_all(
                "SELECT subject, fact FROM facts WHERE subject = ? AND superseded = 0 ORDER BY id",
                (subject,),
            )
        else:
            rows = await self._fetch_all(
                "SELECT subject, fact FROM facts WHERE superseded = 0 ORDER BY id", ()
            )
        return [(r["subject"], r["fact"]) for r in rows]

    # ---- 台账（用来对质） -------------------------------------------------

    async def add_ledger_entries(self, entries: list[LedgerEntry], at: datetime) -> None:
        for entry in entries:
            if not entry.claim.strip():
                continue
            await self.db.execute(
                "INSERT INTO ledger (kind, claim, reason, committed_to, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (entry.kind, entry.claim, entry.reason, entry.committed_to, at.isoformat()),
            )
        await self.db.commit()

    async def ledger(self, kind: str, limit: int = 25) -> list[tuple[datetime, LedgerEntry]]:
        """他之前说过的话，按时间倒序。她拿这个指出前后矛盾。"""
        rows = await self._fetch_all(
            "SELECT * FROM ledger WHERE kind = ? AND resolved = 0 ORDER BY id DESC LIMIT ?",
            (kind, limit),
        )
        return [
            (
                datetime.fromisoformat(r["created_at"]),
                LedgerEntry(
                    kind=r["kind"],
                    claim=r["claim"],
                    reason=r["reason"],
                    committed_to=r["committed_to"],
                ),
            )
            for r in rows
        ]

    # ---- 日记 -------------------------------------------------------------

    async def get_day_plan(self, day: date) -> DayPlan | None:
        row = await self._fetch_one("SELECT day_plan_json FROM diary WHERE day = ?", (day.isoformat(),))
        if row is None or not row["day_plan_json"]:
            return None
        return DayPlan.model_validate_json(row["day_plan_json"])

    async def claim_day_plan(
        self, day: date, now: datetime, stale_after_minutes: float = 30
    ) -> bool:
        """抢占今天的日程生成权。返回 True 表示该你生成。

        **不能拿"diary 行存不存在"当锁。** 她过了午夜还在回消息的话，
        inner_note 会先把那一天的行建出来，早上再想生成日程就永远抢不到，
        结果是那一整天没有日程、没有任何主动消息，而且一个字的日志都没有。
        人设的入睡中位数在午夜之后，这条路径每周都会踩到。

        所以锁看的是 ``day_plan_json`` 有没有内容，外加一个会过期的抢占标记，
        免得生成到一半进程被杀，那一天就再也生成不出来了。
        """
        await self.db.execute(
            "INSERT OR IGNORE INTO diary (day, status) VALUES (?, 'idle')", (day.isoformat(),)
        )
        cutoff = (now - timedelta(minutes=stale_after_minutes)).isoformat()
        cur = await self.db.execute(
            "UPDATE diary SET status = 'generating', claimed_at = ?"
            " WHERE day = ? AND day_plan_json IS NULL"
            "   AND (status != 'generating' OR claimed_at IS NULL OR claimed_at < ?)",
            (now.isoformat(), day.isoformat(), cutoff),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def release_day_plan(self, day: date) -> None:
        """生成失败了就把抢占放掉，否则这一整天都不会再有日程。

        注意只放抢占标记，不删行：那一行里可能已经有日记了。
        """
        await self.db.execute(
            "UPDATE diary SET status = 'idle', claimed_at = NULL"
            " WHERE day = ? AND day_plan_json IS NULL",
            (day.isoformat(),),
        )
        await self.db.commit()

    async def save_day_plan(self, day: date, plan: DayPlan) -> None:
        await self.db.execute(
            "INSERT INTO diary (day, status, day_plan_json) VALUES (?, 'ready', ?)"
            " ON CONFLICT(day) DO UPDATE SET status = 'ready', day_plan_json = excluded.day_plan_json",
            (day.isoformat(), plan.model_dump_json()),
        )
        await self.db.commit()

    async def add_diary_note(self, day: date, note: str, at: datetime) -> None:
        async with self._write_lock:  # 读-改-写，见 _write_lock 的说明
            row = await self._fetch_one(
                "SELECT notes_json FROM diary WHERE day = ?", (day.isoformat(),)
            )
            notes = json.loads(row["notes_json"]) if row else []
            notes.append({"at": at.isoformat(), "note": note})
            await self.db.execute(
                "INSERT INTO diary (day, notes_json) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET notes_json = excluded.notes_json",
                (day.isoformat(), json.dumps(notes, ensure_ascii=False)),
            )
            await self.db.commit()

    async def diary_notes(self, day: date) -> list[tuple[datetime, str]]:
        row = await self._fetch_one("SELECT notes_json FROM diary WHERE day = ?", (day.isoformat(),))
        if row is None:
            return []
        return [
            (datetime.fromisoformat(n["at"]), n["note"]) for n in json.loads(row["notes_json"])
        ]

    # ---- 任务队列 ---------------------------------------------------------

    @staticmethod
    def _row_to_job(row: aiosqlite.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            dedupe_key=row["dedupe_key"],
            run_at=datetime.fromisoformat(row["run_at"]),
            conversation_id=row["conversation_id"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            lease_until=parse_dt(row["lease_until"]),
            progress=json.loads(row["progress_json"]),
            covers_upto_message_id=row["covers_upto_message_id"],
            original_run_at=parse_dt(row["original_run_at"]),
            reason=row["reason"],
            created_at=parse_dt(row["created_at"]),
        )

    async def add_job(self, job: Job, now: datetime) -> int:
        """新建任务。带 ``dedupe_key`` 的重复插入会被忽略，返回 0。"""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO jobs"
            " (kind, dedupe_key, run_at, conversation_id, payload_json, status, reason,"
            "  original_run_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                job.kind,
                job.dedupe_key,
                job.run_at.isoformat(),
                job.conversation_id,
                json.dumps(job.payload, ensure_ascii=False),
                job.reason,
                (job.original_run_at or job.run_at).isoformat(),
                now.isoformat(),
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0) if cur.rowcount else 0

    async def get_job(self, job_id: int) -> Job | None:
        row = await self._fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._row_to_job(row) if row else None

    async def due_jobs(self, now: datetime, limit: int = 20) -> list[Job]:
        rows = await self._fetch_all(
            "SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at LIMIT ?",
            (now.isoformat(), limit),
        )
        return [self._row_to_job(r) for r in rows]

    async def claim_job(self, job_id: int, now: datetime, lease_seconds: float = 300) -> Job | None:
        """抢占一个任务。只有抢到的那一方拿得到 Job，别人拿到 None。

        这是崩溃恢复的关键：租约过期的任务会被 :meth:`sweep_expired_leases` 放回队列。
        """
        cur = await self.db.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_until = ?"
            " WHERE id = ? AND status = 'pending'",
            ((now + timedelta(seconds=lease_seconds)).isoformat(), job_id),
        )
        await self.db.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_job(job_id)

    async def sweep_expired_leases(self, now: datetime) -> int:
        """把租约过期的 running 任务放回队列。进程崩了就靠这个。"""
        cur = await self.db.execute(
            "UPDATE jobs SET status = 'pending', lease_until = NULL"
            " WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)",
            (now.isoformat(),),
        )
        await self.db.commit()
        return cur.rowcount

    async def next_job_run_at(self) -> datetime | None:
        row = await self._fetch_one(
            "SELECT MIN(run_at) AS t FROM jobs WHERE status = 'pending'", ()
        )
        return parse_dt(row["t"]) if row and row["t"] else None

    async def set_job_status(
        self, job_id: int, status: JobStatus, reason: str | None = None
    ) -> None:
        if reason is None:
            await self.db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        else:
            await self.db.execute(
                "UPDATE jobs SET status = ?, reason = ? WHERE id = ?", (status, reason, job_id)
            )
        await self.db.commit()

    async def reschedule_job(
        self, job_id: int, run_at: datetime, payload: dict[str, Any] | None = None
    ) -> None:
        if payload is None:
            await self.db.execute(
                "UPDATE jobs SET run_at = ?, status = 'pending', lease_until = NULL WHERE id = ?",
                (run_at.isoformat(), job_id),
            )
        else:
            await self.db.execute(
                "UPDATE jobs SET run_at = ?, payload_json = ?, status = 'pending',"
                " lease_until = NULL WHERE id = ?",
                (run_at.isoformat(), json.dumps(payload, ensure_ascii=False), job_id),
            )
        await self.db.commit()

    async def save_job_progress(
        self, job_id: int, progress: dict[str, Any], covers_upto_message_id: int | None = None
    ) -> None:
        """记下发到第几条了。重启之后从这里接着发，不重新调模型。"""
        if covers_upto_message_id is None:
            await self.db.execute(
                "UPDATE jobs SET progress_json = ? WHERE id = ?",
                (json.dumps(progress, ensure_ascii=False), job_id),
            )
        else:
            await self.db.execute(
                "UPDATE jobs SET progress_json = ?, covers_upto_message_id = ? WHERE id = ?",
                (json.dumps(progress, ensure_ascii=False), covers_upto_message_id, job_id),
            )
        await self.db.commit()

    async def pending_jobs(
        self, kind: JobKind | None = None, conversation_id: str | None = None
    ) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE status = 'pending'"
        args: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if conversation_id:
            sql += " AND conversation_id = ?"
            args.append(conversation_id)
        rows = await self._fetch_all(sql + " ORDER BY run_at", tuple(args))
        return [self._row_to_job(r) for r in rows]

    async def jobs_of_kind(self, kind: JobKind, conversation_id: str | None = None) -> list[Job]:
        """某一类的全部任务，不限状态。排查和测试用。"""
        if conversation_id:
            rows = await self._fetch_all(
                "SELECT * FROM jobs WHERE kind = ? AND conversation_id = ? ORDER BY id",
                (kind, conversation_id),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM jobs WHERE kind = ? ORDER BY id", (kind,)
            )
        return [self._row_to_job(r) for r in rows]

    async def running_jobs(self, conversation_id: str | None = None) -> list[Job]:
        if conversation_id:
            rows = await self._fetch_all(
                "SELECT * FROM jobs WHERE status = 'running' AND conversation_id = ?",
                (conversation_id,),
            )
        else:
            rows = await self._fetch_all("SELECT * FROM jobs WHERE status = 'running'", ())
        return [self._row_to_job(r) for r in rows]

    # ---- 照片 -------------------------------------------------------------

    async def recently_used_photo_ids(self, now: datetime, cooldown_days: float = 30) -> set[str]:
        """最近发过的照片。同一张隔太近再发就露馅了。"""
        cutoff = (now - timedelta(days=cooldown_days)).isoformat()
        rows = await self._fetch_all(
            "SELECT DISTINCT photo_id FROM photo_usage WHERE sent_at >= ?", (cutoff,)
        )
        return {r["photo_id"] for r in rows}

    async def mark_photo_used(
        self, photo_id: str, conversation_id: str, at: datetime, framed_as_fresh: bool = True
    ) -> None:
        await self.db.execute(
            "INSERT INTO photo_usage (photo_id, sent_at, conversation_id, framed_as_fresh)"
            " VALUES (?, ?, ?, ?)",
            (photo_id, at.isoformat(), conversation_id, int(framed_as_fresh)),
        )
        await self.db.commit()

    # ---- 用量 -------------------------------------------------------------

    async def record_usage(
        self,
        day: date,
        input_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        output_tokens: int = 0,
        estimated_usd: float = 0.0,
    ) -> None:
        await self.db.execute(
            "INSERT INTO usage (day, calls, input_tokens, cache_read_tokens,"
            " cache_creation_tokens, output_tokens, estimated_usd)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET"
            "  calls = calls + 1,"
            "  input_tokens = input_tokens + excluded.input_tokens,"
            "  cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,"
            "  cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,"
            "  output_tokens = output_tokens + excluded.output_tokens,"
            "  estimated_usd = estimated_usd + excluded.estimated_usd",
            (
                day.isoformat(),
                input_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                output_tokens,
                estimated_usd,
            ),
        )
        await self.db.commit()

    async def usage_for(self, day: date) -> dict[str, Any]:
        row = await self._fetch_one("SELECT * FROM usage WHERE day = ?", (day.isoformat(),))
        if row is None:
            return {"calls": 0, "estimated_usd": 0.0}
        return dict(row)

    # ---- KV ---------------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        row = await self._fetch_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def kv_delete(self, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE key = ?", (key,))
        await self.db.commit()

    # ---- 内部 -------------------------------------------------------------

    async def _fetch_one(self, sql: str, args: tuple[Any, ...]) -> aiosqlite.Row | None:
        async with self.db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _fetch_all(self, sql: str, args: tuple[Any, ...]) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, args) as cur:
            return list(await cur.fetchall())
