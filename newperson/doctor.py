"""体检：**只看元数据，不读一个字的聊天内容。**

为什么这么定：她像不像人，不在她说了什么——那部分是模型的功劳，基本不会坏。
在时机上：多久回、隔多久看手机、什么时候不说话、会不会变得有规律。
这些全都能从时间戳和任务表里算出来。

而且这条线本身就该划在这儿：那是他们两个人的对话，不该为了 debug 每天被读一遍。

这个项目唯一真正的失败模式是**她变得有规律**。所以这里最核心的一项是
回复间隔的离散度：真人的间隔是长尾的、乱的，程序的间隔会挤成一团。
这几天修掉的 bug 基本都会在这份报告里露出来——
秒回（间隔全挤在一分钟）、台账变成待办清单（主动消息全是回访）、
重启后积压涌出（一堆任务在同一分钟执行）。
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

OK, WARN, BAD = "✓", "!", "✗"


@dataclass
class Finding:
    level: str
    """OK / WARN / BAD。"""
    line: str
    detail: str = ""


@dataclass
class Report:
    days: int
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: str, line: str, detail: str = "") -> None:
        self.findings.append(Finding(level, line, detail))

    @property
    def worst(self) -> str:
        for level in (BAD, WARN):
            if any(f.level == level for f in self.findings):
                return level
        return OK

    def render(self) -> str:
        out = [f"最近 {self.days} 天"]
        for f in self.findings:
            out.append(f"  {f.level} {f.line}")
            if f.detail:
                out += [f"      {piece}" for piece in f.detail.split("\n")]
        return "\n".join(out)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.DatabaseError:
        return []


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _span(minutes: float) -> str:
    """把分钟数说成人话。一分钟的间隔印成"0.0 小时"没人看得懂。"""
    if minutes < 1:
        return f"{minutes * 60:.0f} 秒"
    if minutes < 90:
        return f"{minutes:.0f} 分钟"
    if minutes < 60 * 36:
        return f"{minutes / 60:.1f} 小时"
    return f"{minutes / 1440:.1f} 天"


def reply_gaps(conn: sqlite3.Connection, since: datetime) -> list[float]:
    """她每次回复隔了多久（分钟）。

    配对方式：一条她发的消息，往前找最近的一条他发的且还没被配过的。
    一来一回中间他连发三条的话，只算最早那条到她回复的间隔——
    那才是"他等了多久"。
    """
    rows = _rows(
        conn,
        "SELECT author_kind, created_at FROM messages"
        " WHERE created_at >= ? AND deleted = 0 ORDER BY id",
        (since.isoformat(),),
    )
    gaps: list[float] = []
    waiting: datetime | None = None
    for row in rows:
        at = _parse(row["created_at"])
        if at is None:
            continue
        if row["author_kind"] == "user":
            if waiting is None:
                waiting = at
        elif waiting is not None:
            gaps.append((at - waiting).total_seconds() / 60)
            waiting = None
    return [g for g in gaps if g >= 0]


def check_rhythm(report: Report, gaps: list[float]) -> None:
    """回复间隔散不散。**这一项是整份报告的重点。**

    真人的间隔是长尾的：多数几分钟到几十分钟，偶尔几小时，偶尔第二天。
    程序的间隔会挤成一团。用四分位距比中位数（一个抗离群值的离散度指标），
    太小就说明她变规律了。
    """
    if len(gaps) < 8:
        report.add(OK, f"回复间隔样本还不够（{len(gaps)} 次），再聊几天才看得出规律")
        return

    ordered = sorted(gaps)
    median = statistics.median(ordered)
    q1 = ordered[len(ordered) // 4]
    q3 = ordered[len(ordered) * 3 // 4]
    spread = (q3 - q1) / median if median > 0 else 0.0
    shape = (
        f"中位 {_span(median)}，一半落在 {_span(q1)}–{_span(q3)}，"
        f"最快 {_span(ordered[0])}，最慢 {_span(ordered[-1])}"
    )

    if median < 2:
        report.add(
            BAD,
            "她几乎是秒回的",
            f"{shape}\n这是这个项目最严重的一种故障——整个设计就是为了让她别秒回。\n"
            "先查 .env 里的 DELAY_SCALE 是不是还留着调试值。",
        )
    elif spread < 0.35:
        report.add(
            BAD,
            "回复间隔挤成一团，她变得有规律了",
            f"{shape}\n四分位距只有中位数的 {spread:.0%}，真人应该是长尾的。",
        )
    elif spread < 0.7:
        report.add(WARN, "回复间隔偏集中，留意一下", shape)
    else:
        report.add(OK, "回复间隔够散", shape)


def check_silence(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """她有多少次看见了但没接话。

    一次都不沉默，说明"有问必答"——那是助手，不是人。
    但沉默太多也不对，多半是模型在判"这条不用回"。
    """
    rows = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM messages WHERE author_kind = 'user'"
        " AND created_at >= ? AND read_at IS NOT NULL AND deleted = 0",
        (since.isoformat(),),
    )
    read = rows[0]["n"] if rows else 0
    rows = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM messages WHERE author_kind != 'user' AND created_at >= ?",
        (since.isoformat(),),
    )
    hers = rows[0]["n"] if rows else 0
    if read < 10:
        return
    ratio = max(0.0, 1 - hers / read)
    if ratio > 0.5:
        report.add(WARN, f"她看了却没接话的比例 {ratio:.0%}，偏高", "多半是模型老在判'这条不用回'")
    elif ratio < 0.02:
        report.add(WARN, f"她几乎有问必答（沉默 {ratio:.0%}）", "真人会漏掉一些话不接")
    else:
        report.add(OK, f"沉默比例 {ratio:.0%}")


def check_proactive(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """她主动开口的频率和花样。

    全是回访（"那件事做了吗"）的话，她就成了一份待办清单。
    """
    rows = _rows(
        conn,
        "SELECT reason, run_at FROM jobs WHERE kind = 'proactive'"
        " AND status = 'done' AND run_at >= ? ORDER BY run_at",
        (since.isoformat(),),
    )
    if not rows:
        report.add(OK, "这段时间她没主动开过口")
        return

    kinds: dict[str, int] = {}
    for row in rows:
        kinds[row["reason"] or "?"] = kinds.get(row["reason"] or "?", 0) + 1
    mix = "　".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
    per_day = len(rows) / max(report.days, 1)

    checks = kinds.get("ledger_check", 0)
    if len(rows) >= 4 and checks / len(rows) > 0.6:
        report.add(
            WARN,
            f"她主动说的话里 {checks / len(rows):.0%} 是在追问你做没做",
            f"{mix}\n再高就像待办清单了，不像朋友。",
        )
    elif per_day > 2:
        report.add(WARN, f"主动开口 {per_day:.1f} 次/天，偏黏", mix)
    else:
        report.add(OK, f"主动开口 {per_day:.1f} 次/天", mix)


def check_bursts(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """有没有一堆任务挤在同一分钟执行。

    那是"重启之后积压一起涌出来"的样子：她会在进程起来三十秒后
    回一条你三小时前发的消息。这是最容易被一眼看穿的一幕。
    """
    rows = _rows(
        conn,
        "SELECT run_at FROM jobs WHERE status = 'done' AND run_at >= ? ORDER BY run_at",
        (since.isoformat(),),
    )
    stamps = [t for t in (_parse(r["run_at"]) for r in rows) if t is not None]
    worst = 0
    for i, at in enumerate(stamps):
        n = sum(1 for other in stamps[i:] if (other - at).total_seconds() <= 120)
        worst = max(worst, n)
    if worst >= 4:
        report.add(
            WARN,
            f"有 {worst} 个任务挤在两分钟内执行",
            "像是重启之后积压一起涌出来的。正常情况下它们该被打散。",
        )


def check_health(report: Report, conn: sqlite3.Connection, db_path: Path, now: datetime) -> None:
    """接口、任务、备份——"她坏了"的那几种可能。"""
    rows = _rows(conn, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'failed'")
    failed = rows[0]["n"] if rows else 0
    if failed:
        report.add(BAD, f"{failed} 个任务重试到放弃了", "她不会自己再试。`!np retry` 放回队列。")

    rows = _rows(conn, "SELECT value FROM kv WHERE key = 'last_api_error'")
    if rows:
        stamp, _, detail = str(rows[0]["value"]).partition("\t")
        when = _parse(stamp)
        ago = f"{(now - when).total_seconds() / 3600:.0f} 小时前" if when else "时间不明"
        report.add(WARN, f"接口出过错（{ago}）", detail or stamp)

    mark = db_path.parent / ".last_backup_at"
    when = _parse(mark.read_text(encoding="utf-8").strip()) if mark.exists() else None
    if when is None:
        report.add(BAD, "没有异地备份", "她的记忆只存在这一台机器上。看 DEPLOY.md。")
    else:
        hours = (now - when).total_seconds() / 3600
        if hours > 48:
            report.add(BAD, f"备份停了 {hours / 24:.0f} 天", "去看 cron 和 /var/log/chloe-backup.log")
        else:
            report.add(OK, f"上次备份 {hours:.0f} 小时前")


def check_memory(report: Report, conn: sqlite3.Connection) -> None:
    """记忆和台账的规模，顺便看看有没有只进不出。"""
    rows = _rows(conn, "SELECT COUNT(*) AS n FROM messages WHERE deleted = 0")
    messages = rows[0]["n"] if rows else 0
    rows = _rows(conn, "SELECT COUNT(*) AS n FROM facts WHERE superseded = 0")
    facts = rows[0]["n"] if rows else 0
    rows = _rows(
        conn, "SELECT COUNT(*) AS n, SUM(resolved) AS done FROM ledger"
    )
    total = rows[0]["n"] if rows else 0
    done = (rows[0]["done"] or 0) if rows else 0
    report.add(OK, f"消息 {messages} 条　她记住的事 {facts} 条　台账 {total} 条（{done} 条已了结）")

    rows = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM ledger WHERE resolved = 0 AND asked_count >= 2",
    )
    stuck = rows[0]["n"] if rows else 0
    if stuck >= 3:
        report.add(
            WARN,
            f"{stuck} 条承诺问过两次还没下文",
            "她已经不再问了，但这说明你们之间有一批悬着的事。",
        )


def run(db_path: Path, now: datetime, days: int = 14) -> Report:
    """跑一遍体检。**全程只读，不改任何东西。**"""
    report = Report(days=days)
    if not db_path.exists():
        report.add(BAD, f"找不到数据库 {db_path}")
        return report

    since = now - timedelta(days=days)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        check_rhythm(report, reply_gaps(conn, since))
        check_silence(report, conn, since)
        check_proactive(report, conn, since)
        check_bursts(report, conn, since)
        check_memory(report, conn)
        check_health(report, conn, db_path, now)
    finally:
        conn.close()
    return report
