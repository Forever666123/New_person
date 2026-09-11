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


def test_an_api_error_carrying_model_output_is_not_printed(tmp_path: Path) -> None:
    """`last_api_error` 里可能夹带他的原话。

    pydantic 的校验错误会把 `input_value='...'` 整段贴出来，而那是模型的输出，
    常常在复述他刚说的事。报告是会被打印、会被贴给别人看的东西——
    它要是能漏出聊天内容，就变成了另一种形式的读取。
    """
    secret = "我明天要去复查，有点慌"
    path = make_db(tmp_path / "leak.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('last_api_error', ?)",
        (
            f"2026-09-24T10:00:00+00:00\t1 validation error for ReplyPlan parts "
            f"Input should be a valid array [type=list_type, input_value='{secret}']",
        ),
    )
    conn.commit()
    conn.close()

    text = doctor.run(path, NOW, days=40).render()
    assert secret not in text
    assert "input_value" not in text


def test_a_short_api_error_still_hides_the_quoted_part(tmp_path: Path) -> None:
    """短的那种更危险：截断根本救不了它。

    `input_value='慌'` 只有二十来个字符。按长度截到 60 字等于原样印出来，
    而"他说了什么"只要一个字就够了。所以判的是引号，不是长度。
    """
    secret = "慌"
    path = make_db(tmp_path / "short-leak.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('last_api_error', ?)",
        (f"2026-09-24T10:00:00+00:00\tbad input_value='{secret}'",),
    )
    conn.commit()
    conn.close()

    text = doctor.run(path, NOW, days=40).render()
    assert "input_value" not in text
    assert f"'{secret}'" not in text


def test_an_exception_in_a_job_reason_is_not_printed(tmp_path: Path) -> None:
    """任务失败时异常文本会盖掉 `jobs.reason`，而那也可能夹带内容。

    平时 reason 是人设里的种类名（own_life、ledger_check……），
    但 `!np retry` 救活一条失败过的主动任务之后，它的 reason 已经是异常串了。
    """
    secret = "SOXL 116 手动止损"
    path = make_db(tmp_path / "reason.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(5):
        conn.execute(
            "INSERT INTO jobs (kind, status, run_at, reason) VALUES ('proactive','done',?,?)",
            ((NOW - timedelta(days=i + 1)).isoformat(), f"ValueError: 处理 {secret} 时出错"),
        )
    conn.commit()
    conn.close()

    text = doctor.run(path, NOW, days=40).render()
    assert secret not in text


def test_silence_counts_replies_not_bubbles(tmp_path: Path) -> None:
    """她一次回复分成几个气泡发，不能把每个气泡都算成一次回应。

    拿气泡数去比消息数的话，真实 40% 的沉默会被算成 0%，
    于是这一项永远在报"她几乎有问必答"——一个永远响的警报等于没有警报。
    """
    path = tmp_path / "bubbles.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    # 十条他的消息，她只回应了六次，但每次都发三个气泡
    for i in range(10):
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at, read_at)"
            " VALUES ('dm','user','x',?,?)",
            (at.isoformat(), at.isoformat()),
        )
        at += timedelta(minutes=10)
        if i < 6:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
                    " VALUES ('dm','bot','y',?)",
                    (at.isoformat(),),
                )
                at += timedelta(seconds=20)
        at += timedelta(hours=3)
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=40).findings if "沉默" in f.line)
    assert "40%" in finding.line, f"应该报 40% 沉默，实际：{finding.line}"


def test_the_autumn_clock_change_does_not_fake_a_burst(tmp_path: Path) -> None:
    """秋令时回拨那一小时不能凭空报"任务挤在两分钟内"。

    run_at 存的是带偏移量的 ISO 串，靠 SQL 的字典序排序在那一小时会把顺序弄反
    （-04:00 和 -05:00 的串比大小没有意义），于是每年十一月都会误报一次。
    """
    from zoneinfo import ZoneInfo

    boston = ZoneInfo("America/New_York")
    path = make_db(tmp_path / "dst.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(12):
        # fold 要在构造时就定下来——`+ timedelta(...)` 会把它丢掉，
        # 于是十二条全变成 -04:00，那个歧义小时根本没造出来。
        at = datetime(2026, 11, 1, 1, 8 * (i % 6), tzinfo=boston, fold=0 if i < 6 else 1)
        conn.execute(
            "INSERT INTO jobs (kind, status, run_at, reason) VALUES ('reply','done',?,'')",
            (at.isoformat(),),
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, datetime(2026, 11, 2, 12, 0, tzinfo=boston), days=14)
    assert not [f for f in report.findings if "挤在两分钟内" in f.line], "夏令时切换被误报成积压"


def test_it_stays_fast_with_a_lot_of_history(tmp_path: Path) -> None:
    """体检会被随手跑、会进 cron，不能因为攒了半年历史就跑几十秒。"""
    import time

    path = make_db(tmp_path / "big.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    at = NOW - timedelta(days=300)
    conn.executemany(
        "INSERT INTO jobs (kind, status, run_at, reason) VALUES ('reply','done',?,'')",
        [((at + timedelta(minutes=8 * i)).isoformat(),) for i in range(20000)],
    )
    conn.commit()
    conn.close()

    started = time.monotonic()
    doctor.run(path, NOW, days=400)
    assert time.monotonic() - started < 5, "两万条任务跑太久了"
