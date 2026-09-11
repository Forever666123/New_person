"""Owner 控制命令。

私聊里以 ``!np`` 开头的消息是给程序看的，**不入库、不进模型**。
她不知道你在操控她。

为什么需要这个：她隔二十分钟才回是设计好的，但你盯着屏幕的时候
分不出"正常"和"坏了"。``!np status`` 就是回答这个问题的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from . import backup as backup_mod
from .memory import Memory

if TYPE_CHECKING:
    from .life import LifeEngine
    from .rhythm import Rhythm
    from .scheduler import Scheduler

log = logging.getLogger(__name__)

PREFIX = "!np"

HELP = """\
`!np status` 她现在什么状态，有什么排着队，今天花了多少钱，上次备份是什么时候
`!np now` 让排着的那条回复立刻发
`!np retry` 把重试到放弃的任务放回队列
`!np pause` / `!np resume` 暂停。暂停时她不回也不主动，但消息照常记着
`!np away 出差 [天数]` / `!np back` 请假。这期间她话少，只保留最低限度的主动
`!np chatty 0.5` 主动消息的频率倍率，0 到 2
`!np ledger [类别]` 她记下的、你说过要做的事
　　　类别：trading（默认）/ study / shift / project / english / sleep
`!np plan` 看今天她给自己编的日程
`!np help` 这些"""


@dataclass
class OwnerContext:
    memory: Memory
    rhythm: Rhythm
    scheduler: Scheduler
    life: LifeEngine
    conversation_id: str
    now: datetime
    max_calls_per_day: int = 0


def is_command(text: str) -> bool:
    return text.strip().lower().startswith(PREFIX)


async def handle(text: str, ctx: OwnerContext) -> str:
    """执行一条命令，返回要回给 Owner 的文本。"""
    parts = text.strip().split()
    args = parts[1:]
    action = args[0].lower() if args else "help"

    handlers = {
        "status": _status,
        "now": _now,
        "retry": _retry,
        "pause": _pause,
        "resume": _resume,
        "away": _away,
        "back": _back,
        "chatty": _chatty,
        "ledger": _ledger,
        "plan": _plan,
        "help": _help,
    }
    handler = handlers.get(action)
    if handler is None:
        return f"不认识 `{action}`。\n{HELP}"
    return await handler(args[1:], ctx)


async def _help(_args: list[str], _ctx: OwnerContext) -> str:
    return HELP


async def _status(_args: list[str], ctx: OwnerContext) -> str:
    snapshot = ctx.rhythm.state_at(ctx.now)
    daily = ctx.rhythm.daily_for(ctx.now)
    conv = await ctx.memory.get_conversation(ctx.conversation_id)

    state_names = {
        "sleeping": "在睡觉",
        "busy": f"在{snapshot.block_title or '忙'}",
        "free": "有空",
        "winding_down": "准备睡了",
    }
    lines = [
        f"**{state_names[snapshot.state]}**，到 {snapshot.until.strftime('%m-%d %H:%M')}",
        f"学期：{snapshot.period or '未知'}　阶段：{snapshot.phase}　今天：{snapshot.variant}",
        f"活跃度 {snapshot.activity:.2f}　当场回的概率 {daily.engage_probability:.0%}",
    ]
    if snapshot.trip_place:
        lines.append(f"人在 **{snapshot.trip_place}**（{daily.timezone}）")
    lines.append(
        f"起床 {daily.wake.strftime('%H:%M')}　入睡 {daily.sleep_start.strftime('%H:%M')}"
    )

    unread = await ctx.memory.unread_messages(ctx.conversation_id)
    if unread:
        lines.append(f"未读 {len(unread)} 条，最早一条 {unread[0].created_at.strftime('%m-%d %H:%M')}")

    # 重试用尽的任务会变成 failed，而 pending_jobs 只查 pending，
    # 于是它从所有你看得见的地方消失：消息还挂在未读里、她永远不会回，
    # 而 status 一片安静。这是"她坏了"和"她没说话"最难分的一种。
    failed = await ctx.memory.failed_jobs(ctx.conversation_id)
    if failed:
        lines.append(f"**有 {len(failed)} 个任务重试到放弃了**（她不会自己再试）：")
        for job in failed[:4]:
            lines.append(f"　{job.kind} 排在 {job.run_at.strftime('%m-%d %H:%M')}　{job.reason}")
        lines.append("　`!np retry` 让她重新试一次")

    jobs = await ctx.memory.pending_jobs(conversation_id=ctx.conversation_id)
    jobs += [j for j in await ctx.memory.pending_jobs() if j.conversation_id is None]
    if jobs:
        lines.append("**排着的：**")
        for job in sorted(jobs, key=lambda j: j.run_at)[:6]:
            gap = (job.run_at - ctx.now).total_seconds() / 60
            when = f"{gap:.0f} 分钟后" if abs(gap) < 90 else job.run_at.strftime("%m-%d %H:%M")
            lines.append(f"　{job.kind} {when}　{job.reason}")
    else:
        lines.append("没有排着的任务")

    if conv.unanswered_initiations:
        lines.append(f"她主动开的话头你有 {conv.unanswered_initiations} 次没回，主动频率已经降下来了")
    if not conv.deliverable:
        lines.append("**发不出去**：你需要和机器人在同一个服务器里，并允许服务器成员私信")

    used = await ctx.memory.usage_for(ctx.now.date())
    calls = used.get("calls", 0)
    cap = ctx.max_calls_per_day
    line = f"今天调了 {calls} 次模型（上限 {cap}），约 ${used.get('estimated_usd', 0):.2f}"
    if cap and calls >= cap:
        # 打满之后她会把回复推到明天，这是设计好的降级，但不说的话
        # 你只会觉得她今天忽然不理人了。
        line += "　**已经打满，今天她不会再回了**"
    lines.append(line)

    # 备份停了是不会有任何症状的，直到你需要它那天。所以放在你每天都看的这里。
    last_backup = backup_mod.last_backup_at(ctx.memory.db_path)
    if last_backup is None:
        lines.append("**没有异地备份**。她的记忆只存在这一台机器上")
    else:
        hours = (ctx.now - last_backup).total_seconds() / 3600
        if hours > 48:
            lines.append(f"**备份停了**：上一次是 {hours / 24:.0f} 天前，去看看 cron")
        else:
            lines.append(f"上次备份 {hours:.0f} 小时前")

    if err := await ctx.memory.kv_get("last_api_error"):
        stamp, _, detail = err.partition("\t")
        try:
            when = datetime.fromisoformat(stamp)
            hours = (ctx.now - when).total_seconds() / 3600
            ago = f"{hours * 60:.0f} 分钟前" if hours < 2 else f"{hours:.0f} 小时前"
            lines.append(f"接口出错（{ago}）：{detail or stamp}")
        except ValueError:
            lines.append(f"最近一次接口出错：{err}")
    if await ctx.memory.kv_get("paused"):
        lines.append("**已暂停**")
    if note := await ctx.memory.kv_get("away_note"):
        until = await ctx.memory.kv_get("away_until") or "?"
        lines.append(f"**请假中**：{note}，到 {until}")

    return "\n".join(lines)


async def _now(_args: list[str], ctx: OwnerContext) -> str:
    jobs = await ctx.memory.pending_jobs("reply", ctx.conversation_id)
    if not jobs:
        unread = await ctx.memory.unread_messages(ctx.conversation_id)
        return "没有排着的回复" + (f"，但有 {len(unread)} 条未读" if unread else "")
    for job in jobs:
        await ctx.scheduler.reschedule(job.id or 0, ctx.now)
    return f"催了 {len(jobs)} 条，马上发"


async def _retry(_args: list[str], ctx: OwnerContext) -> str:
    """把重试到放弃的任务放回队列。

    接口挂过一阵子之后，那几条消息就永远躺在未读里了——她不会自己再试。
    """
    count = await ctx.memory.revive_failed_jobs(ctx.conversation_id, ctx.now)
    if not count:
        return "没有失败的任务"
    return f"{count} 个任务放回队列了，她会重新试一次"


async def _pause(_args: list[str], ctx: OwnerContext) -> str:
    await ctx.memory.kv_set("paused", "1")
    return "暂停了。她不回也不主动，但你发的消息照常记着，resume 之后她会看到"


async def _resume(_args: list[str], ctx: OwnerContext) -> str:
    await ctx.memory.kv_delete("paused")
    return "恢复了"


async def _away(args: list[str], ctx: OwnerContext) -> str:
    if not args:
        return "用法：`!np away 出差 5`（第二个参数是天数，默认 3）"
    note = args[0]
    try:
        days = int(args[1]) if len(args) > 1 else 3
    except ValueError:
        return "天数要是个整数"
    until = ctx.now.date() + timedelta(days=days)
    await ctx.memory.kv_set("away_note", note)
    await ctx.memory.kv_set("away_until", until.isoformat())
    return f"记下了：{note}，到 {until}。这期间她话会少很多，也基本不主动"


async def _back(_args: list[str], ctx: OwnerContext) -> str:
    await ctx.memory.kv_delete("away_note")
    await ctx.memory.kv_delete("away_until")
    return "取消请假"


async def _chatty(args: list[str], ctx: OwnerContext) -> str:
    if not args:
        current = await ctx.memory.kv_get("chattiness") or "1.0"
        return f"现在是 {current}。用法：`!np chatty 0.5`（0 到 2）"
    try:
        value = float(args[0])
    except ValueError:
        return "要一个数字，比如 0.5"
    value = max(0.0, min(2.0, value))
    await ctx.memory.kv_set("chattiness", str(value))
    return f"主动消息频率设成 {value}。明天的日程生效"


async def _ledger(args: list[str], ctx: OwnerContext) -> str:
    """她记下的、你主动说出口的承诺和进展。一次看一类，默认交易。

    她拿这个回头问你做了没有。**只有你自己说出口的才在这儿**——
    关于你的事她不预先知道任何东西，要靠问。
    """
    kind = args[0] if args else "trading"
    entries = await ctx.memory.ledger(kind, limit=12)
    if not entries:
        return (
            f"「{kind}」这一类还没记下什么。\n"
            "她只记你主动说出口的承诺和进展——聊到课业、班次、项目、英语、作息、仓位的时候。\n"
            "别的类别：`!np ledger study` / `shift` / `project` / `english` / `sleep` / `trading`"
        )

    lines = [f"**她记着这些**（{kind}，最近 {len(entries)} 条）"]
    for _entry_id, at, entry in entries:
        line = f"`{at.strftime('%m-%d')}` {entry.claim}"
        if entry.reason:
            line += f"\n　　理由：{entry.reason}"
        if entry.committed_to:
            line += f"\n　　你答应：{entry.committed_to}"
        lines.append(line)
    return "\n".join(lines)


async def _plan(_args: list[str], ctx: OwnerContext) -> str:
    day = ctx.rhythm.local_date(ctx.now)
    plan = await ctx.memory.get_day_plan(day)
    if plan is None:
        return f"{day} 还没有日程"
    lines = [f"**{plan.date}**　{plan.mood}"]
    for event in plan.events:
        mark = "◆" if event.shareable else "·"
        lines.append(f"{mark} {event.start}-{event.end} {event.title}　{event.detail}")
    if plan.thoughts:
        lines.append("心里挂着：" + "；".join(plan.thoughts))
    notes = await ctx.memory.diary_notes(day)
    if notes:
        lines.append("**今天已经发生的：**")
        lines += [f"　{at.strftime('%H:%M')} {note}" for at, note in notes]
    return "\n".join(lines)


async def away_state(memory: Memory, today: date) -> str | None:
    """请假还在生效吗。到期自动清掉，免得忘了取消。"""
    note = await memory.kv_get("away_note")
    if not note:
        return None
    raw = await memory.kv_get("away_until")
    if raw:
        try:
            if today > date.fromisoformat(raw):
                await memory.kv_delete("away_note")
                await memory.kv_delete("away_until")
                return None
        except ValueError:
            pass
    return note


async def is_paused(memory: Memory) -> bool:
    return bool(await memory.kv_get("paused"))
