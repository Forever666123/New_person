"""备份守着一件事：**这个文件没了，她就不认识你了。**

所以这里测的不是"备份命令跑通了"，是"拷出来的东西真的能变回她"：
WAL 里还没落盘的话有没有拷到、有人正在写的时候拷会不会拿到半截、
拿到一个坏文件能不能当场认出来。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from newperson import backup


def make_db(path: Path, rows: int = 5) -> sqlite3.Connection:
    """建一个 WAL 模式的库，写进去 ``rows`` 条，**故意不关连接**。

    不关是重点：WAL 模式下这些写入还躺在 ``-wal`` 旁文件里，主库文件里没有。
    她线上就是这个状态——一直开着，一直在写。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO messages (content, created_at) VALUES (?, ?)",
        [(f"第 {i} 句", f"2026-09-1{i % 10}T20:00:00+00:00") for i in range(rows)],
    )
    conn.commit()
    return conn


def test_snapshot_captures_writes_still_sitting_in_the_wal(tmp_path: Path) -> None:
    """直接 cp 会丢掉最后一段对话，在线备份接口不会。

    这条是整个模块存在的理由。WAL 模式下刚说过的话还在 ``-wal`` 文件里，
    只拷主库文件拿到的是一个"她还没听见你说话"的版本——
    而且它完全正常，integrity_check 也过，你要等到真的去恢复那天才发现。
    """
    src = tmp_path / "live.db"
    conn = make_db(src, rows=5)
    try:
        naive = tmp_path / "naive.db"
        shutil.copy(src, naive)  # 天真的做法：只拷主文件

        proper = tmp_path / "proper.db"
        backup.snapshot(src, proper)
    finally:
        conn.close()

    assert backup.stats(proper).messages == 5
    assert backup.stats(naive).messages == 0, "如果这条挂了，说明 WAL 被提前落盘了，测试本身失效"


def test_snapshot_is_consistent_while_someone_else_is_writing(tmp_path: Path) -> None:
    """她一边写我们一边拷，拿到的仍然是某一个瞬间的完整状态。

    线上不会有"停下来让你备份"的时刻，所以带写入者拷是常态，不是边界情况。
    """
    src = tmp_path / "live.db"
    conn = make_db(src, rows=3)
    try:
        conn.execute(
            "INSERT INTO messages (content, created_at) VALUES (?, ?)",
            ("拷到一半时写的", "2026-09-12T21:00:00+00:00"),
        )
        conn.commit()
        dest = tmp_path / "snap.db"
        backup.snapshot(src, dest)
    finally:
        conn.close()

    assert backup.integrity_errors(dest) == []
    assert backup.stats(dest).messages == 4


def test_snapshot_does_not_blend_into_a_previous_backup(tmp_path: Path) -> None:
    """目标文件已经存在时，得到的是新的那一份，不是两份混在一起。

    SQLite 的 backup() 是往目标库里写，不是覆盖文件。不先删掉的话，
    上一次备份里多出来的表会留在这一次里面。
    """
    old_src = tmp_path / "old.db"
    conn = make_db(old_src, rows=9)
    conn.execute("CREATE TABLE only_in_the_old_one (x INTEGER)")
    conn.commit()
    conn.close()

    dest = tmp_path / "snap.db"
    backup.snapshot(old_src, dest)

    new_src = tmp_path / "new.db"
    conn = make_db(new_src, rows=2)
    conn.close()
    backup.snapshot(new_src, dest)

    assert backup.stats(dest).messages == 2
    check = sqlite3.connect(dest)
    try:
        leftovers = check.execute(
            "SELECT name FROM sqlite_master WHERE name = 'only_in_the_old_one'"
        ).fetchall()
    finally:
        check.close()
    assert leftovers == []


def test_a_corrupt_backup_is_caught(tmp_path: Path) -> None:
    """坏掉的备份要能当场认出来。

    一份坏备份比没有备份更危险：它让你以为自己有退路，
    等真出事那天才发现没有。
    """
    src = tmp_path / "live.db"
    conn = make_db(src, rows=40)
    conn.close()

    dest = tmp_path / "snap.db"
    backup.snapshot(src, dest)
    assert backup.integrity_errors(dest) == []

    # 把中间的页面抹掉，模拟传输截断或者磁盘出错
    raw = bytearray(dest.read_bytes())
    for i in range(len(raw) // 2, min(len(raw) // 2 + 2048, len(raw))):
        raw[i] = 0
    dest.write_bytes(bytes(raw))

    assert backup.integrity_errors(dest) != []


def test_missing_source_says_so_instead_of_creating_an_empty_one(tmp_path: Path) -> None:
    """路径写错时不能悄悄建一个空库然后"备份成功"。

    sqlite3.connect 对不存在的文件默认是新建。少了这个检查，
    DB_PATH 配错的那天你会每天收到一份 0 条消息的完美备份。
    """
    try:
        backup.snapshot(tmp_path / "nope.db", tmp_path / "out.db")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("源库不存在时应该报错")
    assert not (tmp_path / "out.db").exists()


def test_stats_describe_what_makes_it_recognisably_her(tmp_path: Path) -> None:
    """恢复演练时要一眼认出"这是她"，所以数出来的东西得有意义。"""
    src = tmp_path / "live.db"
    conn = make_db(src, rows=7)
    conn.close()

    info = backup.stats(src)
    assert info.messages == 7
    assert info.first_message is not None
    assert info.size_bytes > 0
    text = "\n".join(info.describe())
    assert "7 条" in text


def test_stats_survives_a_file_that_is_not_hers(tmp_path: Path) -> None:
    """拿错文件时给出 0，而不是抛一个看不懂的异常。

    上层要用数字说话——"0 条消息"比 "no such table: messages" 更容易让人
    意识到自己下载错了。
    """
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    assert backup.stats(other).messages == 0


def test_the_mark_is_only_meaningful_when_it_parses(tmp_path: Path) -> None:
    """写坏了就当没备份过，绝不能当成"刚备过"。

    这个标记唯一的作用是让 `!np status` 说实话。读不懂的时候
    宁可报"没有异地备份"吓你一跳，也不能报一个假的安全感。
    """
    db = tmp_path / "newperson.db"
    db.touch()

    assert backup.last_backup_at(db) is None  # 还没备份过

    backup.mark_path(db).write_text("上周吧", encoding="utf-8")
    assert backup.last_backup_at(db) is None

    backup.touch_mark(db)
    marked = backup.last_backup_at(db)
    assert marked is not None
    assert marked.tzinfo is not None


def test_the_mark_format_written_by_the_shell_script_parses(tmp_path: Path) -> None:
    """scripts/backup.sh 用 `date -u +%FT%TZ` 写这个文件。

    标记是 shell 写的、Python 读的——中间没有类型检查，只有这条测试。
    改任何一边的格式，这里会先叫。
    """
    db = tmp_path / "newperson.db"
    db.touch()
    written = subprocess.run(
        ["date", "-u", "+%FT%TZ"], capture_output=True, text=True, check=True
    ).stdout.strip()
    backup.mark_path(db).write_text(written, encoding="utf-8")

    parsed = backup.last_backup_at(db)
    assert parsed is not None, f"{written!r} 解析不了"
    assert parsed.tzinfo is not None
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 120


def test_backup_script_and_restore_script_are_valid_shell() -> None:
    """两个脚本要在出事那天才第一次跑，所以至少保证它们语法是对的。

    恢复脚本尤其：你需要它的时候，通常没有心情调试它。
    """
    root = Path(__file__).resolve().parent.parent
    for name in ("backup.sh", "restore.sh"):
        script = root / "scripts" / name
        assert script.exists(), f"{name} 不见了"
        assert script.stat().st_mode & 0o111, f"{name} 没有执行权限"
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{name} 语法错误：{result.stderr}"


def test_the_systemd_unit_protects_the_bot_token() -> None:
    """`RestartSec` 不是随手写的数。

    Discord 每 24 小时只给 1000 次 IDENTIFY，超了会**重置 bot token**。
    一个"连上就崩"的循环配上立刻重启，一小时就能烧光。
    有人为了"让她快点恢复"把这个值调小的话，这条会先叫。
    """
    unit = (Path(__file__).resolve().parent.parent / "scripts" / "chloe.service").read_text(
        encoding="utf-8"
    )
    restart_sec = int(re.search(r"^RestartSec=(\d+)", unit, re.M).group(1))
    burst = int(re.search(r"^StartLimitBurst=(\d+)", unit, re.M).group(1))
    interval = int(re.search(r"^StartLimitIntervalSec=(\d+)", unit, re.M).group(1))

    assert restart_sec >= 20, "重启间隔太短，崩溃循环会烧掉 Discord 的登录次数"
    # 熔断窗口里最多这么多次，换算成一天
    per_day = burst * (86400 / interval)
    assert per_day < 1000, f"一天最多可能重启 {per_day:.0f} 次，会撞上 Discord 的 1000 次上限"
    assert "ReadWritePaths" in unit, "她只该能写 data/"


def test_the_scripts_do_not_require_docker() -> None:
    """线上是 systemd + venv，机器上根本没有 docker。

    这两个脚本一度写死了 `docker compose`，结果是备份每天静默失败，
    而"没有备份"这件事在出事之前是没有任何症状的。
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    for name in ("backup.sh", "restore.sh"):
        body = scripts.joinpath(name).read_text(encoding="utf-8")
        commands = [
            line
            for line in body.splitlines()
            if "docker" in line and not line.lstrip().startswith("#")
        ]
        assert not commands, f"{name} 里还有依赖 docker 的命令：{commands}"


def test_the_env_example_documents_every_knob_the_scripts_read() -> None:
    """脚本读的每个变量，示例配置里都要提到。

    漏一个的结果是它悄悄用默认值——而默认值在别人的机器上多半是错的。
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    example = scripts.joinpath("backup.env.example").read_text(encoding="utf-8")
    for name in ("backup.sh", "restore.sh"):
        body = scripts.joinpath(name).read_text(encoding="utf-8")
        for var in re.findall(r'^(\w+)="\$\{\1:-', body, re.M):
            if var in ("SERVICE_STOP", "SERVICE_START"):
                continue  # 由 SERVICE_NAME 推出来的，不用单独配
            assert var in example, f"{name} 会读 {var}，但 backup.env.example 里没写"


def _fake_rclone(bin_dir: Path) -> None:
    """把"远端"当成一个本地目录。restore.sh 只用到 lsf / lsl / copy 这三个。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "rclone"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'case "$1" in\n'
        '  lsf) ls -1 "${2%/}" ;;\n'
        '  lsl) ls -l "${2%/}" ;;\n'
        '  copy) cp "$2" "$3" ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_restoring_keeps_the_replaced_database_wal_and_all(tmp_path: Path) -> None:
    """真的装回去时，被顶掉的那份要**连 -wal 一起**留着。

    这条走的是完整的 `restore.sh --install`：下载、解密、验、换文件、起服务。
    它是这个项目里唯一一条"你需要它的那天没有心情调试它"的路径，
    而在这之前从来没有任何东西真的跑过它。

    盯的是一个很容易被忽略的细节：WAL 模式下她刚说过的话还躺在 `-wal` 里，
    主库文件里没有。换文件时把 `-wal` 直接 rm 掉的话，那份
    "留着以防万一"的副本恰好缺了最后几句——而你只会在最需要它的那天发现。
    """
    if shutil.which("gpg") is None:
        import pytest

        pytest.skip("这台机器上没有 gpg")
    root = Path(__file__).resolve().parent.parent
    if (root / "scripts" / "backup.env").exists():
        import pytest

        pytest.skip("本机有 scripts/backup.env，它会覆盖测试里设的环境变量")

    # 线上那份：开着连接，最后几句只在 -wal 里
    live = tmp_path / "data" / "newperson.db"
    live.parent.mkdir()
    conn = make_db(live, rows=7)
    conn.execute(
        "INSERT INTO messages (content, created_at) VALUES (?, ?)",
        ("这句只在 wal 里", "2026-09-20T21:00:00+00:00"),
    )
    conn.commit()
    assert (tmp_path / "data" / "newperson.db-wal").exists(), "前提不成立：没有 -wal 文件"

    # 远端那份：一份更早的快照，内容和线上不一样
    older = tmp_path / "older.db"
    make_db(older, rows=3).close()
    passfile = tmp_path / "pass"
    passfile.write_text("drill-passphrase\n", encoding="utf-8")
    remote = tmp_path / "remote"
    remote.mkdir()
    subprocess.run(
        ["gpg", "--batch", "--yes", "--quiet", "--symmetric", "--cipher-algo", "AES256",
         "--passphrase-file", str(passfile),
         "--output", str(remote / "newperson-20260101T000000Z.db.gpg"), str(older)],
        check=True,
    )

    bin_dir = tmp_path / "bin"
    _fake_rclone(bin_dir)
    import os
    import sys

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RCLONE_REMOTE": f"{remote}/",
        "GPG_PASSPHRASE_FILE": str(passfile),
        "PYTHON_BIN": sys.executable,
        "DB_PATH": str(live),
        "SERVICE_STOP": "true",
        "SERVICE_START": "true",
    }
    result = subprocess.run(
        ["bash", str(root / "scripts" / "restore.sh"), "--install"],
        input="yes\n", capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, f"恢复失败：{result.stdout}\n{result.stderr}"
    conn.close()

    # 装回去的是远端那份（3 条）
    restored = sqlite3.connect(live)
    assert restored.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    restored.close()

    # 被顶掉的那份连 -wal 一起留着，而且打开它能读到只在 wal 里的那句
    aside = sorted(live.parent.glob("newperson.db.replaced-*"))
    aside = [p for p in aside if not p.name.endswith(("-wal", "-shm"))]
    assert len(aside) == 1, f"旧库没留下来：{sorted(p.name for p in live.parent.iterdir())}"
    assert aside[0].with_name(aside[0].name + "-wal").exists(), "旧库的 -wal 被删了，最后几句话没了"

    old = sqlite3.connect(aside[0])
    kept = [row[0] for row in old.execute("SELECT content FROM messages")]
    old.close()
    assert "这句只在 wal 里" in kept, f"旧库里少了只在 wal 里的那句：{kept}"

    # 旧库的旁文件不能留在新库旁边，那些和新库对不上
    assert not (live.parent / "newperson.db-wal").exists()
    assert not backup.integrity_errors(live), "装回去的库自己就是坏的"
