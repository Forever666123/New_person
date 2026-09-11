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

from newperson import backup, doctor
from newperson.memory import SCHEMA
from newperson.persona import load_persona

PERSONA = load_persona(Path(__file__).resolve().parent.parent / "persona" / "persona.yaml")

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

# **用真的建表语句，不手抄一份。**
# 手抄的那份会慢慢和真库对不上，而对不上的地方恰恰是不会被测到的地方：
# `jobs.finished_at` 加进去之后，手抄的 schema 里没有它，
# 于是"任务扎堆"那一项在测试里跑的是另一套数据形状。


def say(conn: sqlite3.Connection, at: datetime, read_at: datetime | None = None) -> None:
    """他说一句。``read_at`` 是**她处理这一批的时刻**，不是他说话的时刻。

    线上就是这样的：`mark_read` 在模型返回之后才调，用的是那一刻的时间，
    而且一次把整批未读都标上。体检正是靠这个值把"一批"认出来的——
    造数据时写成他自己的时间，测出来的就是另一套东西。
    """
    conn.execute(
        "INSERT INTO messages (conversation_id, author_kind, content, created_at, read_at)"
        " VALUES ('dm','user','他说的话',?,?)",
        (at.isoformat(), read_at.isoformat() if read_at else None),
    )


def she_says(
    conn: sqlite3.Connection,
    at: datetime,
    bubbles: int = 1,
    batch: datetime | None = None,
) -> None:
    """她说一次话。几个气泡共用同一个时刻，线上就是这么记的。

    ``batch`` 是她在回的那一批的 ``read_at``；**不传就是主动开口**。
    线上由 `_record_sent` 写进 `reply_batch` 列，体检靠它分回复和主动，
    不靠时间去猜——猜过两轮，两轮都把真实流量算反了。
    """
    for _ in range(bubbles):
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at, reply_batch)"
            " VALUES ('dm','bot','她说的话',?,?)",
            (at.isoformat(), batch.isoformat() if batch else None),
        )


def make_db(path: Path, gaps: list[float], seed: int = 1) -> Path:
    """按给定的"他发完到她回"的间隔造一个库。"""
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    for gap in gaps:
        spoke = at
        at += timedelta(minutes=gap)
        say(conn, spoke, read_at=at)
        she_says(conn, at, batch=at)
        at += timedelta(hours=rng.uniform(2, 20))
    conn.commit()
    conn.close()
    return path


def add_job(
    conn: sqlite3.Connection,
    kind: str,
    finished_at: datetime,
    reason: str = "",
    run_at: datetime | None = None,
    status: str = "done",
) -> None:
    """往任务表里塞一条。``finished_at`` 是**真的执行完**的时刻，体检看的就是它。"""
    conn.execute(
        "INSERT INTO jobs (kind, status, run_at, finished_at, reason, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            kind,
            status,
            (run_at or finished_at).isoformat(),
            finished_at.isoformat(),
            reason,
            finished_at.isoformat(),
        ),
    )


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
    """重启之后积压一起涌出来，是最容易被一眼看穿的一幕。

    **看的必须是执行时刻，不是排期时刻。** 积压涌出来时这两个数差着几小时：
    库里记的 run_at 恰好是分散的（06:00、06:15、06:30……），
    而它们全在 09:05 那一分钟真的发出去。所以这里把 run_at 故意排得很开，
    finished_at 挤在一起——按 run_at 判的话什么都报不出来。
    """
    path = make_db(tmp_path / "burst.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    scheduled = NOW - timedelta(days=2, hours=3)
    ran = NOW - timedelta(days=2)
    for i in range(6):
        add_job(
            conn,
            "reply",
            ran + timedelta(seconds=i * 15),
            run_at=scheduled + timedelta(minutes=i * 15),
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any("挤在两分钟内" in f.line for f in report.findings), (
        f"没报出来：\n{report.render()}"
    )


def test_a_checklist_of_follow_ups_is_flagged(tmp_path: Path) -> None:
    """她主动说的话要是大半都在追问你做没做，她就成了待办清单。"""
    path = make_db(tmp_path / "nag.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(8):
        add_job(conn, "proactive", NOW - timedelta(days=i + 1), "ledger_check")
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
        add_job(
            conn, "proactive", NOW - timedelta(days=i + 1), f"ValueError: 处理 {secret} 时出错"
        )
    conn.commit()
    conn.close()

    text = doctor.run(path, NOW, days=40).render()
    assert secret not in text


def test_silence_is_counted_per_batch_she_processed(tmp_path: Path) -> None:
    """沉默比例数的是"她处理过的一批"，不是"他说了几轮"。

    **她是把整批未读并成一次回复的**——那正是这个项目的设计。
    他隔两小时说的三句话，只要她还没回，就是同一批，她回一次就是全接住了。
    按"他每隔多久算新一轮"去切的话，每隔一段就凭空多出一个"没接话"：
    实测她一条都没漏的十四天被算成沉默 35%，而他在悉尼、她在波士顿，
    他白天说的话正落在她睡觉的时候，这种间隔是常态不是边角。

    这里造二十批，她接住了十二批，答案就是 40%。
    每一批里他说的话条数、隔了多久都故意不一样，那些都不该影响结果。
    """
    path = tmp_path / "batches.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    for i in range(20):
        spoke = [at + timedelta(minutes=45 * j) for j in range(1 + i % 3)]
        read_at = spoke[-1] + timedelta(minutes=18)
        for one in spoke:
            say(conn, one, read_at=read_at)
        if i < 12:
            she_says(conn, read_at, bubbles=1 + i % 3, batch=read_at)
        at = read_at + timedelta(hours=4)
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=40).findings if "沉默" in f.line)
    assert "40%" in finding.line, f"应该报 40% 沉默，实际：{finding.line}"


def test_answering_every_batch_is_flagged_even_when_he_spreads_it_over_hours(
    tmp_path: Path,
) -> None:
    """他隔几小时连着说了几句、她一次全回——这是"有问必答"，不是"漏了大半"。

    这一项是整份报告里唯一盯"她变成助手"的判据。按他说话的间隔切轮次的话，
    它在真实流量下**永远不会响**（实测八个种子最低 19%）。
    """
    path = tmp_path / "everybatch.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    for _ in range(15):
        spoke = [at, at + timedelta(minutes=45), at + timedelta(minutes=100)]
        read_at = spoke[-1] + timedelta(minutes=12)
        for one in spoke:
            say(conn, one, read_at=read_at)
        she_says(conn, read_at, bubbles=2, batch=read_at)
        at = read_at + timedelta(hours=5)
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=40).findings if "沉默" in f.line)
    assert "有问必答" in finding.line, f"她一条没漏却被说成漏了：{finding.line}"


def test_days_of_total_silence_are_not_reported_as_normal(tmp_path: Path) -> None:
    """她一句话没回的时候，报告不能是 ✓。

    按条数算的那版会把"三天一句没回"读成"沉默 46%"，落在 OK 区间里。
    """
    path = tmp_path / "mute.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = NOW - timedelta(days=3)
    for _ in range(20):
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at, read_at)"
            " VALUES ('dm','user','x',?,?)",
            (at.isoformat(), at.isoformat()),
        )
        at += timedelta(hours=3)
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=14)
    finding = next(f for f in report.findings if "没接话" in f.line)
    assert finding.level == doctor.WARN, f"她彻底哑了却没报：{report.render()}"


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
        add_job(conn, "reply", at)
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
        "INSERT INTO jobs (kind, status, run_at, finished_at, reason, created_at)"
        " VALUES ('reply','done',?,?,'',?)",
        [
            ((at + timedelta(minutes=8 * i)).isoformat(),) * 3
            for i in range(20000)
        ],
    )
    conn.commit()
    conn.close()

    started = time.monotonic()
    doctor.run(path, NOW, days=400)
    assert time.monotonic() - started < 5, "两万条任务跑太久了"


def test_the_checkup_stays_quiet_when_she_is_healthy() -> None:
    """**体检在她正常的时候不能报警。** 永远响的警报等于没有警报。

    这里不造数据，用 `simulate` 真跑出来的回复延迟去喂规律性那一项——
    也就是把这个项目里唯一的"她像不像人"判据，接到唯一的"她像不像人"检查上。
    任何一次把延迟调窄的改动（提示词、参数、DELAY_SCALE 忘了关）
    都会让这条先失败，而不是等到体检报告里出现一条谁也不敢信的 BAD。
    """
    import re
    import subprocess
    import sys

    pattern = re.compile(r"（([\d.]+) (秒|分钟|小时)）\s*$")
    per_minute = {"秒": 1 / 60, "分钟": 1.0, "小时": 60.0}
    root = Path(__file__).resolve().parent.parent

    for seed in (1, 2, 3):
        out = subprocess.run(
            [sys.executable, "-m", "newperson", "simulate", "--days", "60", "--seed", str(seed)],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout
        gaps = [
            float(m.group(1)) * per_minute[m.group(2)]
            for line in out.splitlines()
            if (m := pattern.search(line))
        ]
        assert len(gaps) > 150, f"种子 {seed} 只解析出 {len(gaps)} 条延迟，simulate 的输出格式变了？"

        report = doctor.Report(days=60)
        doctor.check_rhythm(report, gaps)
        assert report.worst == doctor.OK, (
            f"种子 {seed} 的正常作息被体检判成了 {report.worst}：\n{report.render()}"
        )


def test_a_database_it_cannot_read_is_not_reported_as_healthy(tmp_path: Path) -> None:
    """**读不出来的库不能印出一份全绿的体检单。**

    每一项检查都把空结果当成"这段时间很安静"，所以只要查询失败被吞掉，
    "打不开这个库"和"库里确实没事发生"就完全同形——
    体检报假平安比没有体检更糟，因为你会信它。

    真实触发方式不少：doctor 跑在和她不同的用户下、库所在目录不可写
    （WAL 要建 -shm）、别的进程正握着独占锁。这里用最直接的一种：
    文件在，但根本不是个 SQLite 库。
    """
    path = tmp_path / "junk.db"
    path.write_bytes("这不是一个 sqlite 文件".encode() * 50)

    report = doctor.run(path, NOW, days=14)
    assert report.worst == doctor.BAD, f"读不了却报了 {report.worst}：\n{report.render()}"
    assert "读不了" in report.findings[0].line


def test_a_backup_stamp_in_the_future_is_not_taken_as_fresh(tmp_path: Path) -> None:
    """未来的备份时间戳不能把整份报告里最该响的那个 BAD 按住。

    时钟跳变、从别的机器搬过来的 data/、手写的标记文件都会造出这个。
    算出来是负的小时数，正好绕过"停了 48 小时"那个判断，永久报 ✓。
    """
    path = make_db(tmp_path / "future.db", [5, 20, 60])
    backup.touch_mark(path, NOW + timedelta(days=30))

    report = doctor.run(path, NOW, days=40)
    assert report.worst == doctor.BAD
    assert any("未来" in f.line for f in report.findings), report.render()


def test_an_unreadable_backup_mark_does_not_crash_the_checkup(tmp_path: Path) -> None:
    """标记文件读不出来（比如变成了目录）时，体检要照常出结果。"""
    path = make_db(tmp_path / "weird.db", [5, 20, 60])
    backup.mark_path(path).mkdir()

    report = doctor.run(path, NOW, days=40)  # 不能抛
    assert any("异地备份" in f.line for f in report.findings)


def test_an_api_error_that_has_not_cleared_is_loud(tmp_path: Path) -> None:
    """还没恢复的接口报错是"坏了"，不是"看看就行"。

    成功一次就会把这条清掉，所以它还在，就意味着最后一次调用是失败的——
    密钥过期、余额用光、模型名写错，这些她不会自己好。
    原来一律 WARN，而 WARN 的退出码是 0：接口两小时前还在报 401，
    cron 里那行体检一声不吭。
    """
    path = make_db(tmp_path / "err.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('last_api_error', ?)",
        (f"{(NOW - timedelta(hours=2)).isoformat()}\t接口返回 401",),
    )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    finding = next(f for f in report.findings if "接口出过错" in f.line)
    assert finding.level == doctor.BAD, f"两小时前还在报错却只是 {finding.level}"


def test_messages_he_sent_yesterday_that_are_still_unread_are_the_loudest_thing(
    tmp_path: Path,
) -> None:
    """他说的话躺了一天没人回——**这是唯一一种真正意义上的"她坏了"。**

    报告里原来根本没有这一项。于是最该被喊出来的那件事是沉默的：
    她三天一句没回、接口两小时前还在报 401，整份报告的退出码仍然是 0。
    她隔二十分钟才回是设计好的，正因为如此，从外面看"正常"和"坏了"
    一模一样，只有这一项分得开。
    """
    path = make_db(tmp_path / "stuck.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
        " VALUES ('dm','user','那个你看了吗',?)",
        ((NOW - timedelta(hours=30)).isoformat(),),
    )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any(f.level == doctor.BAD and "还没回" in f.line for f in report.findings), (
        report.render()
    )


def test_proactive_jobs_that_sent_nothing_are_not_counted_as_speaking_up(
    tmp_path: Path,
) -> None:
    """排了六次、一句没说，不能报成"她主动开口 6 次"。

    `handle_proactive_job` 有七八条提前 return（在睡觉、有未读、正热聊、请假、
    没照片、没到期的承诺、"没什么要说的"），这些照样被标成 done。
    任务表记的是排期，消息表记的才是她真的说了话。
    """
    path = make_db(tmp_path / "silentjobs.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(6):
        add_job(conn, "proactive", NOW - timedelta(days=i + 1), "own_life")
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=14).findings if "主动开口" in f.line)
    assert "0.0 次/天" in finding.line, f"把没发出去的也数上了：{finding.line}"


def test_a_new_proactive_kind_from_the_persona_is_named_not_lumped_into_other(
    tmp_path: Path,
) -> None:
    """人设里新加的主动种类要按名字显示。

    CLAUDE.md 写着"新的主动消息方式改 yaml，代码不用动"，而种类名原来是
    写死在代码里的一份清单。加一个 `gym_note` 之后，报告会变成
    "ledger_check 14　其它 14"——而这一项的全部价值就在种类分布这一行上。
    """
    path = make_db(tmp_path / "kinds.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(5):
        add_job(conn, "proactive", NOW - timedelta(days=i + 1), "gym_note")
    conn.commit()
    conn.close()

    finding = next(
        f for f in doctor.run(path, NOW, days=14, kinds_known=frozenset({"gym_note"})).findings
        if "主动开口" in f.line
    )
    assert "gym_note" in finding.detail, f"人设里的种类被压成了'其它'：{finding.detail}"


def test_follow_ups_count_towards_the_checklist_warning(tmp_path: Path) -> None:
    """`follow_up` 也要算进主动消息的口味里。

    她说"我查完告诉你"排的就是这种，而且它**没有每天的上限**——
    "她变成一份待办清单"最可能就从这条路来，原来整类被漏掉了。
    """
    path = make_db(tmp_path / "fu.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    for i in range(8):
        add_job(conn, "follow_up", NOW - timedelta(days=i + 1), "ledger_check")
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=14)
    assert any("追问你做没做" in f.line for f in report.findings), report.render()


def test_background_jobs_bunching_up_is_not_reported(tmp_path: Path) -> None:
    """日程生成、记忆整理挤在一起他根本看不见，不该报。

    这些后台任务的打散窗口只有 30–300 秒，三四个落进同一个两分钟窗口是常事。
    把它们算进去的结果是：把"打散正常工作"报成了故障。
    """
    path = make_db(tmp_path / "bg.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    at = NOW - timedelta(days=2)
    for i in range(6):
        add_job(conn, "memory_update" if i % 2 else "day_plan", at + timedelta(seconds=i * 12))
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert not [f for f in report.findings if "挤在两分钟内" in f.line], report.render()


def test_a_fast_but_long_tailed_median_is_not_called_instant_replies(tmp_path: Path) -> None:
    """又快又**散**不是故障，是最像人的一种分布。

    只看中位数的话，"多数半分钟、偶尔十小时"会拿到整份报告里最严厉的措辞。
    真的秒回是又快又齐。
    """
    gaps = [0.2, 0.4, 0.5, 0.6, 0.3, 0.5, 0.4, 0.7, 0.3, 0.6,
            3.0, 6.2, 26.4, 44.0, 90.0, 150.0, 300.0, 616.0, 40.0, 12.0]
    report = doctor.run(make_db(tmp_path / "tail.db", gaps), NOW, days=60)
    assert rhythm_finding(report).level != doctor.BAD, rhythm_finding(report).line


def test_speaking_up_soon_after_her_own_reply_still_counts_as_speaking_up(
    tmp_path: Path,
) -> None:
    """她回完他之后隔一会儿又自己开口——那是两件事，第二件是主动开口。

    按"上一条是不是也是她说的"去合并气泡，会把这一整类吞掉；
    改成看时间间隔，也只是把盲区从"她刚说过话"缩成"她 N 分钟内说过话"——
    而 `handle_proactive_job` 只在热聊（180 秒内）时才退出，
    三分钟之后她就允许主动开口了。所以这里隔五分钟就该数上。
    """
    path = tmp_path / "speakup.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = NOW - timedelta(days=10)
    for _ in range(10):
        read_at = at + timedelta(minutes=20)
        say(conn, at, read_at=read_at)
        she_says(conn, read_at, bubbles=2, batch=read_at)
        she_says(conn, read_at + timedelta(minutes=5))  # 隔五分钟她自己又开口
        at = read_at + timedelta(hours=8)
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=10).findings if "主动开口" in f.line)
    assert "1.0 次/天" in finding.line, f"十次主动一次都没数上：{finding.line}"


def test_speaking_up_after_a_message_she_chose_not_to_answer_is_counted(
    tmp_path: Path,
) -> None:
    """他上午说了一句她没接，晚上她自己开口——那也是主动开口。

    "他说过话之后她说的都算回复"这种判法会把这一整类记成 0，
    而"他说了她没接"恰恰是这个人物最像人的行为之一。
    """
    path = tmp_path / "unanswered.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    at = NOW - timedelta(days=10)
    for _ in range(10):
        read_at = at + timedelta(minutes=25)
        say(conn, at, read_at=read_at)       # 她看了，但没接话
        she_says(conn, read_at + timedelta(hours=10))
        at = read_at + timedelta(hours=14)
    conn.commit()
    conn.close()

    finding = next(f for f in doctor.run(path, NOW, days=10).findings if "主动开口" in f.line)
    assert "1.0 次/天" in finding.line, finding.line


def test_pausing_her_does_not_make_the_checkup_scream(tmp_path: Path) -> None:
    """`!np pause` 期间未读堆着是他自己要求的，不是故障。

    天天喊 BAD 的警报等于没有警报。
    """
    path = make_db(tmp_path / "paused.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO kv (key, value) VALUES ('paused', '1')")
    conn.execute(
        "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
        " VALUES ('dm','user','在吗',?)",
        ((NOW - timedelta(days=2)).isoformat(),),
    )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert not [f for f in report.findings if "还没回" in f.line], report.render()
    assert any("pause" in f.line for f in report.findings)


def test_one_missing_column_does_not_swallow_the_backup_and_api_checks(
    tmp_path: Path,
) -> None:
    """一项查不成，不能把后面整段吞掉——尤其是最该响的那几项。

    真实场景：`finished_at` 是后加的迁移列，只在她的进程 `open()` 时补上。
    部署完新代码、她还没重启的那个窗口里跑一次 cron 体检，就是这个形状。
    原来全部检查共用一个 try，而"没有异地备份""密钥过期"恰好排在最后。
    """
    path = tmp_path / "oldschema.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace("    finished_at TEXT,\n", ""))
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('last_api_error', ?)",
        (f"{(NOW - timedelta(hours=1)).isoformat()}\t401 invalid x-api-key",),
    )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=14)
    lines = "\n".join(f.line for f in report.findings)
    assert "没有异地备份" in lines, f"备份那一项被吞掉了：\n{report.render()}"
    assert "接口出过错" in lines, f"接口那一项被吞掉了：\n{report.render()}"
    assert any("没查成" in f.line for f in report.findings), "坏掉的那一项应该说一声"


def test_the_burst_check_says_when_it_has_nothing_to_look_at(tmp_path: Path) -> None:
    """没数据也要说一声。别的每一项都会说"样本还不够"，只有这一项原来是直接蒸发的。

    报告里少一行，谁也不会注意到——而那正是"报假平安"的一种。
    """
    path = make_db(tmp_path / "nofinish.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    at = NOW - timedelta(days=2)
    for i in range(20):  # 迁移之前就结束的任务，finished_at 永远是 NULL
        conn.execute(
            "INSERT INTO jobs (kind, status, run_at, reason, created_at)"
            " VALUES ('reply','done',?,'',?)",
            ((at + timedelta(minutes=i)).isoformat(),) * 2,
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any("看不出事情挤不挤" in f.line for f in report.findings), report.render()


def test_her_words_not_reaching_him_at_all_is_the_loudest_thing(tmp_path: Path) -> None:
    """私聊被 Discord 挡掉——她想说的话一个字都到不了，而报告里每一项都正常。

    消息照样被 `mark_read`、任务照样 done，只有 `deliverable` 被置 0，
    而报告从不读这一列。`check_stuck` 只看得见未读那一半。
    """
    path = make_db(tmp_path / "blocked.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO conversations (id, kind, deliverable) VALUES ('owner','dm',0)"
    )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any(f.level == doctor.BAD and "发不出去" in f.line for f in report.findings), (
        report.render()
    )


async def test_the_counters_match_what_the_real_app_actually_did(tmp_path: Path) -> None:
    """**跑一遍真的她，再用体检去数，两边必须对得上。**

    上一版的沉默比例和主动开口都是照着"他说了几轮"手推出来的，
    合成数据全过，接上真实流量就整个反了：她一条没漏被算成沉默 35%。
    合成的库测的是"我以为她怎么工作"，这条测的是"她实际怎么工作"。

    这里走完整条路——`on_user_message` → 排回复 → 模型 → `mark_read` →
    真的投递——他连发、隔几小时再说、她有时不接。
    然后拿 doctor 去数，跟真实发生的事对。
    """
    import random
    from types import SimpleNamespace

    from newperson.models import ReplyPart, ReplyPlan
    from tests.test_integration import _OPEN, EVENING, build, drain

    # 十二批：他一到三句连发，她接住其中八批
    answers = [
        ReplyPlan(parts=[ReplyPart(text="嗯"), ReplyPart(text="知道了")]) if i < 8
        else ReplyPlan(parts=[])
        for i in range(12)
    ]
    app, channel, _llm, clock, memory = await build(tmp_path, PERSONA, answers)
    app._should_handle = lambda _m: True
    app.client = SimpleNamespace(user=SimpleNamespace(id=999))
    app._default_channel = channel
    app._channels[555] = channel

    rng = random.Random(5)
    at = EVENING
    for i in range(12):
        for j in range(1 + i % 3):  # 他一轮里连发一到三句，中间隔几十分钟
            clock.set(at)
            await app.on_user_message(
                SimpleNamespace(
                    id=1000 + i * 10 + j,
                    content=f"第 {i}-{j} 句",
                    created_at=at,
                    author=SimpleNamespace(id=42, display_name="Leo", bot=False),
                    channel=SimpleNamespace(id=555),
                    attachments=[],
                )
            )
            at += timedelta(minutes=rng.choice([40, 55, 70]))
        await drain(app, clock, hops=12)
        at = clock.now() + timedelta(hours=rng.uniform(3, 9))

    await memory.close()
    _OPEN.remove(memory)

    report = doctor.run(app.settings.db_path, clock.now() + timedelta(minutes=1), days=30)
    silence = next(f for f in report.findings if "沉默" in f.line or "没接话" in f.line)
    # 十二批接住八批 = 沉默 33%
    assert "33%" in silence.line, f"真实跑出来是 8/12，体检说：{silence.line}\n{report.render()}"

    opened = next((f for f in report.findings if "主动开口" in f.line), None)
    assert opened is None or "0.0 次/天" in opened.line, (
        f"她一次都没主动开口，体检却说：{opened.line if opened else ''}"
    )


async def test_a_fast_back_and_forth_is_not_reported_as_silence(tmp_path: Path) -> None:
    """热聊时她两次回复天然挨得很近，**不能把它们并成一次**。

    上一版按三分钟的间隔把她的气泡聚成"一轮"。可一次投递的每个气泡在库里
    是同一个时刻，那个间隔唯一能做的事就是把两次**独立的**回复并掉——
    而人设里 `hot_reply_median_seconds` 是 75 秒。
    实测：十二轮全接住的对话被报成"没接话 83%"。

    这条走完整条路：他在她回完二十秒后就接话，她一条没漏。
    """
    from types import SimpleNamespace

    from newperson.models import ReplyPart, ReplyPlan
    from tests.test_integration import _OPEN, EVENING, build, drain

    answers = [ReplyPlan(parts=[ReplyPart(text="嗯"), ReplyPart(text="在的")]) for _ in range(12)]
    app, channel, _llm, clock, memory = await build(tmp_path, PERSONA, answers)
    app._should_handle = lambda _m: True
    app.client = SimpleNamespace(user=SimpleNamespace(id=999))
    app._default_channel = channel
    app._channels[555] = channel

    at = EVENING
    for i in range(12):
        clock.set(at)
        await app.on_user_message(
            SimpleNamespace(
                id=2000 + i,
                content=f"第 {i} 句",
                created_at=at,
                author=SimpleNamespace(id=42, display_name="Leo", bot=False),
                channel=SimpleNamespace(id=555),
                attachments=[],
            )
        )
        await drain(app, clock, hops=12)
        at = clock.now() + timedelta(seconds=20)  # 她回完二十秒他就接话

    await memory.close()
    _OPEN.remove(memory)

    report = doctor.run(app.settings.db_path, clock.now() + timedelta(minutes=1), days=30)
    finding = next(f for f in report.findings if "沉默" in f.line or "没接话" in f.line)
    assert "有问必答" in finding.line, f"她十二轮全接住了，体检说：{finding.line}"


async def test_an_old_database_is_not_read_as_her_talking_to_herself(
    tmp_path: Path,
) -> None:
    """迁移之前的消息分不出回复和主动开口，那一段要排除掉。

    不排的话，她过去所有的回复都会被当成主动开口——刚更新完那天，
    体检会说她天天在自说自话。而"她是不是变黏了"正是这一项要回答的问题。
    """
    from newperson.memory import SCHEMA as REAL
    from newperson.memory import Memory

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(REAL.replace(",\n    reply_batch TEXT\n", "\n"))
    at = NOW - timedelta(days=10)
    for _ in range(20):  # 二十轮老对话，全都没有批次信息（那一列还不存在）
        read_at = at + timedelta(minutes=15)
        say(conn, at, read_at=read_at)
        conn.execute(
            "INSERT INTO messages (conversation_id, author_kind, content, created_at)"
            " VALUES ('dm','bot','她说的话',?)",
            (read_at.isoformat(),),
        )
        at += timedelta(hours=8)
    conn.commit()
    conn.close()

    memory = Memory(path)
    await memory.open()  # 迁移：加列，并记下分界线
    await memory.close()

    report = doctor.run(path, NOW, days=14)
    opened = next((f for f in report.findings if "主动开口" in f.line), None)
    assert opened is None or "0.0 次/天" in opened.line, (
        f"老数据被当成她在自说自话：{opened.line if opened else ''}\n{report.render()}"
    )
    assert not [f for f in report.findings if "沉默" in f.line], "老数据段不该报沉默比例"


def test_a_fast_conversation_is_not_reported_as_a_restart_pile_up(tmp_path: Path) -> None:
    """他在她回完两秒后就接话，来回三十轮——那不是积压涌出来。

    光看密度的话，这两件事长得一模一样。分得开的信号库里现成就有：
    积压的特征是**排期早就过了才执行**（run_at 在几小时前，finished_at
    挤在重启后那一分钟），实时的那些两者只差几秒。
    """
    path = make_db(tmp_path / "fastjobs.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    at = NOW - timedelta(days=1)
    for _ in range(30):  # 排期和执行只差两秒——实时
        add_job(conn, "reply", at + timedelta(seconds=2), "reply", run_at=at)
        at += timedelta(seconds=25)
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert not [f for f in report.findings if "挤在两分钟内" in f.line], report.render()


def test_a_real_restart_pile_up_is_still_caught(tmp_path: Path) -> None:
    """而真的积压涌出来要照样报：排期在几小时前，全在同一分钟执行。"""
    path = make_db(tmp_path / "pileup.db", [5, 20, 60])
    conn = sqlite3.connect(path)
    scheduled = NOW - timedelta(days=1, hours=3)
    ran = NOW - timedelta(days=1)
    for i in range(5):
        add_job(
            conn,
            "reply",
            ran + timedelta(seconds=i * 10),
            "reply",
            run_at=scheduled + timedelta(minutes=i * 20),
        )
    conn.commit()
    conn.close()

    report = doctor.run(path, NOW, days=40)
    assert any("挤在两分钟内" in f.line for f in report.findings), report.render()
