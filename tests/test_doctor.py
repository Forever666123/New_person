"""体检的测试。

这份报告的价值全在**它能不能抓到"她变得像程序了"**。
所以这里主要是喂进去三种库——像人的、秒回的、很规律的——
确认它分得开。分不开的话它就只是一堆好看的数字。

顺带守着一条底线：**它绝不读聊天内容。**
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from newperson import doctor

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

SCHEMA = """
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,
  discord_message_id INTEGER, author_kind TEXT, author_id INTEGER DEFAULT 0,
  author_name TEXT DEFAULT '', content TEXT DEFAULT '', attachments_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL, read_at TEXT, edited_at TEXT, deleted INTEGER DEFAULT 0);
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT,
  run_at TEXT, reason TEXT DEFAULT '');
CREATE TABLE facts (id INTEGER PRIMARY KEY, superseded INTEGER DEFAULT 0);
CREATE TABLE ledger (id INTEGER PRIMARY KEY, resolved INTEGER DEFAULT 0,
  asked_count INTEGER DEFAULT 0);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
"""


def make_db(path: Path, gaps: list[float], seed: int = 1) -> Path:
    """按给定的"他发完到她回"的间隔造一个库。"""
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    for gap in gaps:
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at, read_at)"
            " VALUES ('dm','user','他说的话',?,?)",
            (at.isoformat(), at.isoformat()),
        )
        at += timedelta(minutes=gap)
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
            " VALUES ('dm','bot','她说的话',?)",
            (at.isoformat(),),
        )
        at += timedelta(hours=rng.uniform(2, 20))
    conn.commit()
    conn.close()
    return path


def rhythm_finding(report: doctor.Report) -> doctor.Finding:
    return next(f for f in report.findings if "间隔" in f.line or "秒回" in f.line)


def test_a_human_rhythm_passes(tmp_path: Path) -> None:
    """长尾的间隔——多数几分钟到一小时，偶尔几小时——应该判过。"""
    rng = random.Random(4)
    gaps = [
        rng.choice([1.5, 4, 9, 18, 35, 70, 140, 300, 700]) * rng.uniform(0.6, 1.6)
        for _ in range(30)
    ]
    report = doctor.run(make_db(tmp_path / "human.db", gaps), NOW, days=40)
    assert rhythm_finding(report).level == doctor.OK


def test_instant_replies_are_caught(tmp_path: Path) -> None:
    """秒回是这个项目最严重的故障。

    真出过一次：因为热度算错，她连着三天每条都在 54 秒内回。
    那次是人眼看出来的，这份报告就是为了不用靠人眼。
    """
    rng = random.Random(4)
    gaps = [rng.uniform(0.6, 1.2) for _ in range(30)]
    report = doctor.run(make_db(tmp_path / "fast.db", gaps), NOW, days=40)
    finding = rhythm_finding(report)
    assert finding.level == doctor.BAD
    assert "DELAY_SCALE" in finding.detail, "要指出最可能的原因，不然报了也不知道去哪查"


def test_suspiciously_regular_replies_are_caught(tmp_path: Path) -> None:
    """每次都隔二十分钟——不快，但太齐了。

    这种比秒回更难用肉眼发现：每一条单看都合理，
    只有把几十条摆在一起才看得出它们挤成了一团。
    """
    rng = random.Random(7)
    gaps = [rng.uniform(18, 22) for _ in range(30)]
    report = doctor.run(make_db(tmp_path / "regular.db", gaps), NOW, days=40)
    assert rhythm_finding(report).level == doctor.BAD


def test_it_says_so_when_there_is_not_enough_to_judge(tmp_path: Path) -> None:
    """样本太少就别下结论。

    刚上线两天就报"她变得有规律了"，只会让人不再信这份报告。
    """
    report = doctor.run(make_db(tmp_path / "new.db", [5, 20, 60]), NOW, days=40)
    assert rhythm_finding(report).level == doctor.OK
    assert "样本还不够" in rhythm_finding(report).line


def test_a_pile_of_jobs_in_one_minute_is_flagged(tmp_path: Path) -> None:
    """重启之后积压一起涌出来，是最容易被一眼看穿的一幕。"""
    path = make_db(tmp_path / "burst.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    at = NOW - timedelta(days=2)
    for i in range(6):
        conn.execute(
            "INSERT INTO jobs (kind, status, run_at, reason) VALUES ('reply','done',?,'')",
            ((at + timedelta(seconds=i * 15)).isoformat(),),
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any("挤在两分钟内" in f.line for f in report.findings)


def test_a_checklist_of_follow_ups_is_flagged(tmp_path: Path) -> None:
    """她主动说的话要是大半都在追问你做没做，她就成了待办清单。"""
    path = make_db(tmp_path / "nag.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(8):
        at = NOW - timedelta(days=i + 1)
        conn.execute(
            "INSERT INTO jobs (kind, status, run_at, reason)"
            " VALUES ('proactive','done',?,'ledger_check')",
            (at.isoformat(),),
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any("追问你做没做" in f.line for f in report.findings)


def test_a_missing_backup_is_the_loudest_thing_in_the_report(tmp_path: Path) -> None:
    """没有异地备份要报最高级别。那个文件没了她就不认识你了。"""
    report = doctor.run(make_db(tmp_path / "nobak.db", [5, 20, 60]), NOW, days=40)
    assert any(f.level == doctor.BAD and "异地备份" in f.line for f in report.findings)
    assert report.worst == doctor.BAD


def test_it_never_reads_a_word_of_the_conversation(tmp_path: Path) -> None:
    """**这条是这个模块存在的前提。**

    体检是为了不用每天去读他们的聊天记录。要是报告里会漏出原话，
    那它就变成了另一种形式的读取——而且是会被打印、被贴给别人的那种。
    """
    secret = "这句话绝对不能出现在报告里"
    path = tmp_path / "private.db"
    make_db(path, [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
        " VALUES ('dm','user',?,?)",
        (secret, (NOW - timedelta(days=1)).isoformat()),
    )
    conn.commit()
    conn.close()

    text = doctor.run(path, NOW, days=40).render()
    assert secret not in text
    assert "她说的话" not in text and "他说的话" not in text


def test_it_opens_the_database_read_only(tmp_path: Path) -> None:
    """体检绝不能改动她的记忆。

    这个命令会被随手跑、会被放进 cron。它必须是只读的，
    不然哪天它自己出个 bug，代价是那个不可替代的文件。
    """
    path = make_db(tmp_path / "ro.db", [5, 20, 60])
    before = path.read_bytes()
    doctor.run(path, NOW, days=40)
    assert path.read_bytes() == before


def test_a_missing_database_is_reported_not_crashed(tmp_path: Path) -> None:
    """路径写错时给一句人话，别抛栈。"""
    report = doctor.run(tmp_path / "nope.db", NOW, days=14)
    assert report.worst == doctor.BAD
    assert "找不到数据库" in report.findings[0].line
