"""把她的记忆完整地拷出来。

``data/newperson.db`` 是她的全部：你们说过的话、她记住的关于你的事、
交易台账、每天的日记。**这个文件没了，她就不认识你了。**

**不能直接 cp。** 数据库跑在 WAL 模式下，最近的写入还躺在 ``-wal`` 旁文件里，
只拷主文件会丢掉最后一段对话；三个文件一起拷，又可能拷到写了一半的中间状态。
这里用的是 SQLite 的在线备份接口：她一边写，我们一边拷，拿到的仍然是
某一个瞬间的一致快照。

拷完立刻验一遍。备份最常见的死法不是没备份，是**备了一年从来没人试过能不能恢复**。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
"""她随时可能在写。等锁的时间给足，别为了快而备份失败。"""


@dataclass(frozen=True)
class Stats:
    """一份备份里到底装了什么。

    数字是给人看的：恢复演练时你要一眼认出"这是她，不是个空壳"。
    """

    size_bytes: int
    messages: int
    first_message: str | None
    last_message: str | None
    facts: int
    ledger: int
    diary_days: int

    def describe(self) -> list[str]:
        lines = [f"大小 {self.size_bytes / 1024:.0f} KB", f"消息 {self.messages} 条"]
        if self.first_message and self.last_message:
            lines.append(f"从 {self.first_message[:16]} 到 {self.last_message[:16]}")
        lines.append(f"她记住的事 {self.facts} 条　交易台账 {self.ledger} 条")
        lines.append(f"日记 {self.diary_days} 天")
        return lines


def snapshot(src: Path, dest: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
    """把 ``src`` 的一致快照写到 ``dest``。

    用 ``Connection.backup()``（SQLite 的在线备份接口），一次拷完。
    分页拷贝会在中途放写入者进来，源库一变备份就得重来——
    我们这个库一年才长 2 MB，一次拷完更省事，也不会重来。
    """
    if not src.exists():
        raise FileNotFoundError(f"找不到数据库：{src}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # backup() 是往目标里写，不是覆盖。目标是上一次的残留时会拷出个混合体。
    dest.unlink(missing_ok=True)

    source = sqlite3.connect(src, timeout=timeout)
    try:
        source.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def integrity_errors(path: Path) -> list[str]:
    """跑一遍 ``PRAGMA integrity_check``，没问题返回空列表。

    一个能打开、能查询的数据库文件仍然可能是坏的。这一步才是"能恢复"的证据。
    """
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        # 坏得连 integrity_check 都跑不动，也是一种检查结果。
        # 抛出去的话调用方就得在两个地方处理同一件事，删备份那条路容易漏。
        return [f"打不开：{exc}"]
    finally:
        conn.close()
    messages = [row[0] for row in rows]
    return [] if messages == ["ok"] else messages


def stats(path: Path) -> Stats:
    """数一数这份备份里有什么。

    表可能不存在——比如拿错了文件，或者备份是空的。
    这种情况下当 0 处理，让上层用数字说话，而不是抛一个看不懂的异常。
    """
    conn = sqlite3.connect(path)
    try:

        def count(table: str) -> int:
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            except sqlite3.DatabaseError:
                return 0
            return int(row[0]) if row else 0

        first = last = None
        try:
            row = conn.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM messages"
            ).fetchone()
            if row:
                first, last = row[0], row[1]
        except sqlite3.DatabaseError:
            pass

        return Stats(
            size_bytes=path.stat().st_size,
            messages=count("messages"),
            first_message=first,
            last_message=last,
            facts=count("facts"),
            ledger=count("ledger"),
            diary_days=count("diary"),
        )
    finally:
        conn.close()


MARK_NAME = ".last_backup_at"
"""备份成功后写一下这个文件，``!np status`` 靠它报"上次备份多久之前"。

备份最常见的死法是**它停了三周而没有人知道**：cron 的报错邮件没人看，
磁盘满了、rclone 的令牌过期了，一切照旧，直到你需要它那天。
把"上次备份"放进你每天都会看的那个命令里，是唯一可靠的提醒。

放在 data/ 里是故意的：它跟着数据库一起走，换机器时不会留下一个假的"刚备过"。
**只在真正传到机器外面之后才写**——本地拷贝成功不算备份。
"""


def mark_path(db_path: Path) -> Path:
    return db_path.parent / MARK_NAME


def touch_mark(db_path: Path, when: datetime | None = None) -> None:
    mark = mark_path(db_path)
    mark.parent.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now(UTC)).astimezone(UTC)
    mark.write_text(stamp.isoformat(timespec="seconds"), encoding="utf-8")


def last_backup_at(db_path: Path) -> datetime | None:
    """上一次成功传出去是什么时候。读不到就当作没备份过。"""
    mark = mark_path(db_path)
    try:
        raw = mark.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
