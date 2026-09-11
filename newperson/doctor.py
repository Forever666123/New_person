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

import contextlib
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import backup

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
    """**故意不吞异常。**

    原来这里 `except sqlite3.DatabaseError: return []`，而每一项检查都把空结果
    当成"这段时间很安静"。于是"打不开这个库"和"库里确实没事发生"完全同形：
    doctor 跑在别的用户下、库被 `BEGIN EXCLUSIVE` 握着、目录不可写——
    任何一种都会印出一份全绿的体检单，退出码 0。
    体检报假平安比没有体检更糟，所以让它炸出来，由 run() 统一说明白。
    """
    return conn.execute(sql, args).fetchall()


def _timeline(conn: sqlite3.Connection, since: datetime) -> list[tuple[str, datetime]]:
    """窗口内的消息，``(谁说的, 什么时候)``，**按时间排**。

    不按 id 排：补抓是一个频道一个频道整段写库的，插入顺序不等于说话顺序。
    也不在 SQL 里按 created_at 排：那是带偏移量的 ISO 字符串，
    她那边一年换两次夏令时，换季那天字符串序和时间序对不上。
    """
    rows = _rows(
        conn,
        "SELECT author_kind, created_at FROM messages"
        " WHERE created_at >= ? AND deleted = 0 ORDER BY id",
        (since.isoformat(),),
    )
    out = [(r["author_kind"], _parse(r["created_at"])) for r in rows]
    return sorted(((k, t) for k, t in out if t is not None), key=lambda kt: kt[1])


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# 有些字段名义上是"诊断信息"，实际可能夹带聊天内容：
#   - last_api_error 里可能是 pydantic 的校验错误，而那种错误会把
#     `input_value='...'` 整段原样贴出来，那是模型的输出，常常复述他刚说的话
#   - jobs.reason 平时是人设里的种类名，但任务失败时会被异常文本盖掉
# 报告是会被打印、会被贴给别人看的东西，所以这里只放**认得出的**词，
# 别的一律压成一句话。
SAFE_REASONS = {
    "opener", "own_life", "callback", "ledger_check", "travel_note", "window_photo",
    "follow_up", "sign_off", "day_plan", "memory_update", "reply", "proactive",
}
"""内置的种类名。**人设里新加的种类由 run() 传进来并进这个集合。**

写死一份清单和 CLAUDE.md 那条"新的主动消息方式改 yaml，代码不用动"是冲突的：
加一个 `gym_note` 之后报告会写成"ledger_check 14　其它 14"，
而这一项的全部价值就在种类分布这一行上。
"""
MAX_DETAIL = 60

LATE_SECONDS = 300.0
"""任务比排期晚这么多秒才算"迟到"。见 :func:`check_bursts` 为什么不能只看密度。"""


def _safe_detail(raw: str) -> str:
    """把诊断文本压成不会夹带聊天内容的形状。

    两种情形要分开处理，**不能都用截断**：带引号的那种截断了也还是漏——
    ``input_value='慌'`` 只有二十来个字符，截到 60 字就是原样输出。
    所以只要出现引号或者 ``input_value``，整条都不印。
    """
    text = " ".join(str(raw).split())
    if not text:
        return ""
    if any(mark in text for mark in ("input_value", "'", '"', "“", "”", "‘", "’")):
        return "（这条可能夹带聊天内容，没有印出来；完整内容在 journalctl 里）"
    if len(text) > MAX_DETAIL:
        return f"{text[:MAX_DETAIL]}…（已截断，完整内容在 journalctl 里）"
    return text


def _safe_reason(raw: str | None, known: frozenset[str] = frozenset()) -> str:
    name = raw or "?"
    return name if (name in SAFE_REASONS or name in known) else "其它"


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
    gaps: list[float] = []
    waiting: datetime | None = None
    for kind, at in _timeline(conn, since):
        if kind == "user":
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
    # 门槛从 8 提到 20：八个样本里，真实的长尾（0.4 分钟到 10 小时）
    # 也能让中位数落到 2 分钟以下，于是最严厉的那句话被判给一条完全健康的曲线。
    # 两百个种子里 n=8 误判两次，n=20 一次都没有。
    if len(gaps) < 20:
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

    # "秒回"要**同时**看离散度。只看中位数的话，长尾数据（中位两分钟、
    # 最慢十小时）会被判成秒回——而那恰恰是最像人的一种分布。
    # 真的秒回是"又快又齐"，快而散不是故障。
    if median < 2 and spread < 0.7:
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


def _batches_and_runs(
    conn: sqlite3.Connection, since: datetime
) -> tuple[list[datetime], int, datetime | None]:
    """``(她处理过的每一批未读, 她主动开口的次数, 从什么时候起数得准)``。

    **不靠时间去猜哪句话在回哪一批。** 猜过两轮，两轮都把真实流量算反了：

    - 第一轮按"他隔多久算新一轮"切他那一侧。可她是把整批未读并成一次回复的——
      他隔两小时说的三句，只要她还没回就是同一批。她一条没漏的十四天
      被算成沉默 35%。
    - 第二轮改对了他那一侧，却在她那一侧犯了同样的错：按间隔把她的气泡聚成轮。
      而一次投递的每个气泡在库里是同一个时刻，那个间隔唯一能做的事
      就是把**两次独立的回复**并掉。热聊时她两次回复之间天然不到三分钟，
      于是 12 轮全接住的对话被报成"没接话 83%"。

    所以现在不猜了：她每条消息上记着自己在回哪一批（``reply_batch``，
    存的是那一批的 ``read_at``），主动开口是 NULL。一批一轮直接对上。

    第三个返回值是这次实际从什么时候起算的。``reply_batch`` 是后加的列，
    更早的消息全是 NULL，分不出回复和主动——那一段要排除掉，
    否则她过去的每一次回复都会被算成主动开口。
    """
    # 分界线是**迁移那一刻记下来的**，不是从数据里猜的：一个只主动开口、
    # 从没回过话的库，和一个迁移之前的老库，在数据上长得一模一样。
    row = _rows(conn, "SELECT value FROM kv WHERE key = 'reply_batch_since'")
    raw = str(row[0]["value"] or "") if row else ""
    boundary = _parse(raw) if raw else None
    # 没有这条 kv 就是建库时本来就带着这一列，整段都记得准。
    # 加一微秒：分界线那条消息**自己**也是迁移之前的，要排除在外。
    floor = max(since, boundary + timedelta(microseconds=1)) if boundary else since

    rows = _rows(
        conn,
        "SELECT read_at FROM messages WHERE author_kind = 'user'"
        " AND read_at IS NOT NULL AND read_at >= ? AND deleted = 0",
        (floor.isoformat(),),
    )
    batches = sorted({t for t in (_parse(r["read_at"]) for r in rows) if t is not None})

    # 主动开口：她的消息里没有批次的那些，按**确切时刻**聚成一次。
    # 一次投递的所有气泡共用一个 created_at，所以确切相等就够了，不用猜间隔。
    rows = _rows(
        conn,
        "SELECT created_at FROM messages WHERE author_kind != 'user'"
        " AND reply_batch IS NULL AND created_at >= ? AND deleted = 0",
        (floor.isoformat(),),
    )
    opened = len({r["created_at"] for r in rows})
    return batches, opened, floor


def _answered(conn: sqlite3.Connection, batches: list[datetime]) -> int:
    """这些批里有多少批她真的说了话。"""
    if not batches:
        return 0
    rows = _rows(
        conn,
        "SELECT DISTINCT reply_batch FROM messages WHERE author_kind != 'user'"
        " AND reply_batch IS NOT NULL AND deleted = 0",
    )
    done = {t for t in (_parse(r["reply_batch"]) for r in rows) if t is not None}
    return sum(1 for b in batches if b in done)


def check_silence(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """她有多少次看见了但没接话。

    一次都不沉默，说明"有问必答"——那是助手，不是人。
    但沉默太多也不对，多半是模型在判"这条不用回"。
    """
    batches, _opened, _floor = _batches_and_runs(conn, since)
    if len(batches) < 10:
        return
    ratio = max(0.0, 1 - _answered(conn, batches) / len(batches))
    if ratio > 0.5:
        report.add(WARN, f"她看了却没接话的比例 {ratio:.0%}，偏高", "多半是模型老在判'这条不用回'")
    elif ratio < 0.05:
        report.add(WARN, f"她几乎有问必答（沉默 {ratio:.0%}）", "真人会漏掉一些话不接")
    else:
        report.add(OK, f"沉默比例 {ratio:.0%}")


def check_proactive(
    report: Report,
    conn: sqlite3.Connection,
    since: datetime,
    max_per_day: float = 2.0,
    kinds_known: frozenset[str] = frozenset(),
) -> None:
    """她主动开口的频率和花样。

    全是"那件事做了吗"和"我查完告诉你"的话，她就成了一份待办清单。

    **频率从消息表数，不从任务表数。** 任务表记的是排期，而
    ``handle_proactive_job`` 有七八条提前 return（在睡觉、有未读、正热聊、
    请假、没照片、没到期的承诺、"没什么要说的"），这些照样被标成 done：
    六个什么都没发的任务会被报成"她开口了 6 次"。
    真正的"她主动开口"是她说的话里，**没有在接他哪一批未读**的那些轮。
    """
    _batches, opened, floor = _batches_and_runs(conn, since)
    # 窗口被分界线截短时按截短之后的天数算，否则刚更新完那几天分母偏大，
    # 什么都显得不黏。用小数天，不然会差出一整天来。
    now = since + timedelta(days=report.days)
    days = max((now - floor).total_seconds() / 86400, 1.0)
    per_day = opened / days

    # 口味只看任务表。`follow_up` 也算：她说"我查完告诉你"排的就是这种，
    # 而且它**没有每天的上限**，"她变成一份待办清单"最可能就从这条路来。
    rows = _rows(
        conn,
        "SELECT reason FROM jobs WHERE kind IN ('proactive', 'follow_up')"
        " AND status = 'done' AND run_at >= ?",
        (since.isoformat(),),
    )
    kinds: dict[str, int] = {}
    for row in rows:
        name = _safe_reason(row["reason"], kinds_known)
        kinds[name] = kinds.get(name, 0) + 1
    mix = "　".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))

    if not opened and not rows:
        report.add(OK, "这段时间她没主动开过口")
        return

    # **办事型的两种都算在分子里。** 只把 ledger_check 当分子、
    # 却把 follow_up 加进分母的话，加得越多这条警报越小：
    # 十四次追问加十个"我查完告诉你"——二十四条主动没有一条是闲聊——
    # 反而从 100% 降到 58%，安静通过。
    errands = kinds.get("ledger_check", 0) + kinds.get("follow_up", 0)
    if len(rows) >= 4 and errands / len(rows) > 0.6:
        report.add(
            WARN,
            f"她主动说的话里 {errands / len(rows):.0%} 是在办事（追问你做没做、汇报查完了）",
            f"{mix}\n再高就像待办清单了，不像朋友。",
        )
    elif per_day >= max_per_day - 0.05:
        # 上限是人设里的 proactive.max_per_day，代码里写死一个数的话
        # 这一项永远够不着（上限 2，判据是 > 2）：黏了四倍也还是 ✓。
        # 天天顶着上限本身就是信号——真人不会每天都正好想起你两次。
        report.add(
            WARN,
            f"主动开口 {per_day:.1f} 次/天，天天顶着上限（{max_per_day:g}）",
            mix,
        )
    else:
        report.add(OK, f"主动开口 {per_day:.1f} 次/天", mix)


def check_bursts(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """有没有一堆**对着他的**事挤在同一分钟发生。

    那是"重启之后积压一起涌出来"的样子：她会在进程起来三十秒后
    回一条你三小时前发的消息。这是最容易被一眼看穿的一幕。

    两个地方原来是错的：

    - 看的是 ``run_at``，那是**排期时刻**，执行时从不回写。积压涌出来的时候，
      库里那几条的 run_at 恰好是分散的（06:00、06:15、06:30……），
      而它们全在 09:05 这一分钟执行——该报的一声不吭。现在看 ``finished_at``。
    - 不分种类。日程生成、记忆整理这些后台任务挤在一起他根本看不见，
      而它们的打散窗口只有 30–300 秒，三四个落进同一个两分钟窗口是常事——
      于是把"打散正常工作"报成了故障。现在只数他看得见的那几种。
    """
    # **只数"迟到的"。** 光看密度的话，积压涌出和热聊长得一模一样：
    # 他在她回完两秒后接话、来回三十轮，同样是"两分钟内三件事"，
    # 而那是最正常不过的一段对话。分得开两者的信号库里现成就有——
    # 积压的特征是**排期早就过了才执行**：run_at 在几小时前，
    # finished_at 挤在重启后的那一分钟。实时的那些两者只差几秒。
    rows = _rows(
        conn,
        "SELECT finished_at, COALESCE(original_run_at, run_at) AS due FROM jobs"
        " WHERE kind IN ('reply', 'proactive', 'follow_up', 'sign_off')"
        " AND status = 'done' AND finished_at IS NOT NULL AND finished_at >= ?",
        (since.isoformat(),),
    )
    # 迟到多久在 Python 里算，不用 SQL 的日期函数：存的是带偏移量的 ISO 串，
    # 而这个文件里其它地方也都是自己 parse 的，口径统一。
    late: list[datetime] = []
    for row in rows:
        done_at, due = _parse(row["finished_at"]), _parse(row["due"])
        if done_at is not None and due is not None and (done_at - due).total_seconds() > LATE_SECONDS:
            late.append(done_at)
    # **在 Python 里排序，不靠 SQL 的字符串序。** 存的是带偏移量的 ISO 串，
    # 字典序在夏令时切换那一小时会把顺序弄反，于是秋天回拨的那晚会凭空报一批扎堆。
    stamps = sorted(late)
    # 滑动窗口，线性。原来是 O(n²) 且每轮复制一次列表，两万条要跑七十秒。
    worst = 0
    left = 0
    for right, at in enumerate(stamps):
        while (at - stamps[left]).total_seconds() > 120:
            left += 1
        worst = max(worst, right - left + 1)
    if worst >= 3:
        report.add(
            WARN,
            f"有 {worst} 件对着他的事挤在两分钟内发生",
            "像是重启之后积压一起涌出来的。正常情况下它们该被打散。",
        )
    elif stamps:
        report.add(OK, f"{len(stamps)} 件事分布正常（同一两分钟里最多 {worst} 件）")
    else:
        # **没数据也要说一声。** 别的每一项都会说"样本还不够"，
        # 只有这一项原来是直接蒸发的——报告里少一行，谁也不会注意到。
        # 真实场景：`finished_at` 是后加的列，迁移之前就结束的任务永远是 NULL，
        # 所以刚更新完那几天这一项本来就没东西可看。
        done = _rows(conn, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'done'")
        n = done[0]["n"] if done else 0
        report.add(
            OK,
            "还看不出事情挤不挤（没有带执行时刻的任务）"
            + (f"，库里有 {n} 个更早的任务不带这个时刻" if n else ""),
        )


def check_failed_jobs(report: Report, conn: sqlite3.Connection, since: datetime) -> None:
    """重试到放弃的任务。

    也按窗口算：不限窗口的话，半年前那一次失败会让退出码永远是 1，
    而一个永远非零的退出码等于没有退出码。

    用 `finished_at`（真的放弃是几点）优先，没有才退回 run_at——
    和"事情挤不挤"同一个口径，理由也一样：run_at 是排期时刻，
    每次重试都会被改写，不是"这件事什么时候出的问题"。
    """
    rows = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'failed'"
        " AND COALESCE(finished_at, run_at) >= ?",
        (since.isoformat(),),
    )
    failed = rows[0]["n"] if rows else 0
    if failed:
        report.add(BAD, f"{failed} 个任务重试到放弃了", "她不会自己再试。`!np retry` 放回队列。")


def check_api(report: Report, conn: sqlite3.Connection, now: datetime) -> None:
    """接口现在通不通。

    **还没恢复的接口报错是 BAD，不是 WARN。** 成功一次就会把这条清掉，
    所以它还在，就意味着最后一次调用是失败的——密钥过期、余额用光、
    模型名写错，这些她不会自己好。半年前那次抖动才是"看看就行"。
    """
    rows = _rows(conn, "SELECT value FROM kv WHERE key = 'last_api_error'")
    if not rows:
        return
    stamp, _, detail = str(rows[0]["value"]).partition("\t")
    when = _parse(stamp)
    hours = (now - when).total_seconds() / 3600 if when else None
    if hours is None:
        ago = "时间不明"
    elif hours < 0:
        ago = "时间戳在未来，这台机器的时钟不对"
    elif hours < 1:
        ago = "刚刚"
    else:
        ago = f"{hours:.0f} 小时前"
    level = BAD if hours is None or hours < 6 else WARN
    report.add(level, f"接口出过错（{ago}）", _safe_detail(detail or stamp))


def check_backup(report: Report, db_path: Path, now: datetime) -> None:
    """异地备份还在不在。那个文件没了，她就不认识你了。

    复用 `backup` 模块：它已经处理过读不出来（OSError）和内容不是时间（ValueError），
    而这里原来是 `mark.read_text()` 裸调——标记文件变成目录就直接把体检打崩。
    """
    when = backup.last_backup_at(db_path)
    if when is None:
        report.add(BAD, "没有异地备份", "她的记忆只存在这一台机器上。看 DEPLOY.md。")
        return
    hours = (now - when).total_seconds() / 3600
    if hours < -1:
        # 未来的标记（时钟跳变、从别的机器搬过来的 data/、手写的文件）
        # 原来会算出负的小时数，一路走到 `hours > 48` 那个分支的反面，
        # 把整份报告里最该响的那个 BAD 永久按住。
        ahead = f"{-hours / 24:.0f} 天后" if -hours >= 24 else f"{-hours:.0f} 小时后"
        report.add(
            BAD,
            f"备份的时间戳在未来（{ahead}）",
            "这台机器的时钟不对，或者这份 data/ 是从别处搬来的。"
            "在查清楚之前，别把这个当成备份正常。",
        )
    elif hours > 48:
        report.add(BAD, f"备份停了 {hours / 24:.0f} 天", "去看 cron 和 /var/log/chloe-backup.log")
    else:
        report.add(OK, f"上次备份 {max(hours, 0):.0f} 小时前")


def check_stuck(report: Report, conn: sqlite3.Connection, now: datetime) -> None:
    """他说的话有没有躺在那儿没人回。**这是唯一一种真正意义上的"她坏了"。**

    报告里原来根本没有这一项，于是最该被喊出来的那件事是沉默的：
    她三天一句没回、接口两小时前还在报 401，整份报告的退出码是 0。
    而她隔二十分钟才回是设计好的——正因为如此，"正常"和"坏了"
    从外面看一模一样，只有这一项分得开。
    """
    if _rows(conn, "SELECT 1 FROM kv WHERE key = 'paused'"):
        # `!np pause` 期间她本来就不回，未读堆着是**他自己要求的**。
        # 不排掉的话这一项会天天喊 BAD，而一个天天喊的警报等于没有警报。
        report.add(OK, "她被 `!np pause` 停着，这期间不回消息")
        return
    rows = _rows(
        conn,
        "SELECT created_at FROM messages WHERE author_kind = 'user'"
        " AND read_at IS NULL AND deleted = 0 ORDER BY id",
        (),
    )
    stamps = sorted(t for t in (_parse(r["created_at"]) for r in rows) if t is not None)
    if not stamps:
        return
    hours = (now - stamps[0]).total_seconds() / 3600
    if hours > 24:
        report.add(
            BAD,
            f"他 {_span(hours * 60)}前说的话还没回（一共 {len(stamps)} 条未读）",
            "她睡得再久也不会超过一天。查 journalctl，看是不是卡在某个任务上。",
        )
    elif hours > 12:
        report.add(WARN, f"有 {len(stamps)} 条未读，最早那条是 {_span(hours * 60)}前的")


def check_deliverable(report: Report, conn: sqlite3.Connection) -> None:
    """她的话发不发得出去。**这是"她坏了"的另一半。**

    私聊被 Discord 挡掉时（他退了共同服务器、关了对陌生人的私信），
    消息照样被 `mark_read`、任务照样 done，只是 `deliverable` 被置 0——
    她想说的话一个字都到不了他手里，而报告里每一项都正常。
    `check_stuck` 只看得见未读那一半，这一列才是另一半。
    """
    rows = _rows(conn, "SELECT deliverable FROM conversations")
    if rows and any(not r["deliverable"] for r in rows):
        report.add(
            BAD,
            "她的话发不出去（Discord 拒收）",
            "多半是你退了共同的服务器，或者关掉了那个服务器的成员私信。"
            "加回来之后她自己会恢复——她发出去一条就把这个判断收回来了。",
        )


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


def run(
    db_path: Path,
    now: datetime,
    days: int = 14,
    max_per_day: float = 2.0,
    kinds_known: frozenset[str] = frozenset(),
) -> Report:
    """跑一遍体检。**全程只读，不改任何东西。**

    ``max_per_day`` 和 ``kinds_known`` 从人设里来：判据和种类名都不该写死在代码里。
    """
    report = Report(days=days)
    if not db_path.exists():
        report.add(BAD, f"找不到数据库 {db_path}")
        return report

    since = now - timedelta(days=days)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # 先探一下。不探的话，"打不开这个库"的报错会等到第一条检查才冒出来，
        # 而更早以前它被 _rows 整个吞掉——印出一份全绿的体检单，退出码 0。
        conn.execute("SELECT 1 FROM messages LIMIT 1").fetchall()
    except sqlite3.DatabaseError as exc:
        with contextlib.suppress(Exception):
            conn.close()
        report.add(
            BAD,
            f"读不了 {db_path}",
            f"{_safe_detail(str(exc))}\n"
            "**这份报告什么都没检查。** 常见原因：doctor 跑在和她不同的用户下、"
            "库所在的目录不可写（WAL 要建 -shm）、或者别的进程正握着独占锁。",
        )
        return report

    # 顺序是故意的：**"她坏了"那几项排在最前**。
    # 每项各自兜异常——一列缺失（比如刚部署完、她还没重启，jobs 还没有
    # finished_at）原来会把后面整段吞掉，而最该响的备份和接口恰好在最后。
    checks: list[tuple[str, Callable[[], None]]] = [
        ("她是不是卡住了", lambda: check_stuck(report, conn, now)),
        ("话发不发得出去", lambda: check_deliverable(report, conn)),
        # **拆成三项，各自兜异常。** 合成一项的话，"重试到放弃的任务"那句 SQL
        # 碰上一列缺失，就把后面的"接口"和"备份"一起带走了——
        # 而那两项恰恰是最该响的。
        ("失败的任务", lambda: check_failed_jobs(report, conn, since)),
        ("接口", lambda: check_api(report, conn, now)),
        ("备份", lambda: check_backup(report, db_path, now)),
        ("回复间隔", lambda: check_rhythm(report, reply_gaps(conn, since))),
        ("沉默比例", lambda: check_silence(report, conn, since)),
        ("主动开口", lambda: check_proactive(report, conn, since, max_per_day, kinds_known)),
        ("事情挤不挤", lambda: check_bursts(report, conn, since)),
        ("记忆和台账", lambda: check_memory(report, conn)),
    ]
    try:
        for name, check in checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - 一项坏了不能连累别的
                report.add(WARN, f"「{name}」这一项没查成", _safe_detail(str(exc)))
    finally:
        conn.close()
    return report
