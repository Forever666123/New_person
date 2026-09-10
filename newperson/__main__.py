"""命令行入口。

```
python -m newperson run        启动
python -m newperson check      检查配置和人设（加 --online 会真的连一次 Discord）
python -m newperson simulate   用假时钟模拟，看她什么时候回消息（不联网）
python -m newperson plan       让她给今天编一份日程并打印（联网）
python -m newperson photos     扫描照片目录，生成索引草稿
python -m newperson backup     把她的记忆拷一份出来（一致快照，拷完就验）
python -m newperson verify     检查一份备份还能不能用
```
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

from .attention import AttentionPolicy, extract_features, heat_of
from .calendar import AcademicCalendar
from .config import Settings, load_settings
from .persona import Persona, load_persona, validate_persona
from .rhythm import Rhythm

OK, WARN, BAD = "✓", "!", "✗"


def setup_logging(level: str, tz: tzinfo | None = None) -> None:
    """日志时间戳跟她走，不跟服务器走。

    启动时那句"日志里的时间都是她那边的时间"原来是假的：格式化器用的是
    服务器本地时间（VPS 上通常是 UTC）。于是日志长这样——

        16:07:57 INFO [job] 排上 proactive#2 09-10 13:48 opener

    行首 16:07 是 UTC，行尾 13:48 是波士顿时间，看上去像是排到了三小时前。
    排查一个"她怎么不说话"的问题时，这种时间戳会把人直接带到沟里去。
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )
    if tz is not None:
        def converter(timestamp: float | None) -> time.struct_time:
            return datetime.fromtimestamp(timestamp or 0, tz).timetuple()

        for handler in logging.getLogger().handlers:
            if handler.formatter is not None:
                handler.formatter.converter = converter
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def load_all(args: argparse.Namespace) -> tuple[Settings, Persona] | None:
    settings = load_settings()
    try:
        persona = load_persona(settings.persona_path)
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} 人设读不了：{exc}")
        return None
    return settings, persona


# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings()
    problems = 0

    print("配置")
    for name in settings.missing_required():
        print(f"  {BAD} {name} 没填")
        problems += 1
    if not settings.missing_required():
        print(f"  {OK} Token、API key、你的用户 ID 都在")
    from .brain import EFFORT_SUPPORTED, PRICING_PER_MTOK

    print(f"  {OK} 回复用 {settings.model}，日程和记忆用 {settings.utility_model}")
    if settings.model not in PRICING_PER_MTOK:
        print(f"  {WARN} 不认识 {settings.model} 这个模型，费用估算会不准")
    if settings.model not in EFFORT_SUPPORTED:
        print(f"  {WARN} {settings.model} 不接受 effort 参数，NEWPERSON_EFFORT 会被跳过")
    if settings.force_awake:
        print(f"  {WARN} DEBUG_FORCE_AWAKE 开着，她不会睡觉。看完效果记得关掉")
    if settings.delay_scale != 1.0:
        print(f"  {WARN} DELAY_SCALE={settings.delay_scale}，时间是被压缩的，别在正式用的时候留着")

    print("人设")
    try:
        persona = load_persona(settings.persona_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} 读不了 {settings.persona_path}：{exc}")
        return 1

    print(f"  {OK} {persona.name}（{persona.english_name or '无英文名'}），{persona.timezone}")
    for level, message in validate_persona(persona):
        mark = BAD if level == "error" else WARN
        print(f"  {mark} {message}")
        if level == "error":
            problems += 1

    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    now = datetime.now(tz=persona.tz)
    snapshot = rhythm.state_at(now)
    daily = rhythm.for_day(rhythm.local_date(now))
    print("此刻")
    print(f"  {OK} {snapshot.period or '不在学期表里'}／{snapshot.phase}／{snapshot.variant}")
    print(
        f"  {OK} 状态 {snapshot.state}，活跃度 {snapshot.activity:.2f}，"
        f"今天 {daily.wake.strftime('%H:%M')} 起 {daily.sleep_start.strftime('%H:%M')} 睡"
    )
    if snapshot.trip_place:
        print(f"  {OK} 她人在 {snapshot.trip_place}")

    print("照片")
    from .media import PhotoLibrary

    library = PhotoLibrary(settings.photos_index)
    library.load()
    if library.photos:
        print(f"  {OK} {len(library.photos)} 张，标签：{'、'.join(library.all_tags()[:12])}")
    else:
        print(f"  {WARN} 照片库是空的，她不会发照片（往 {settings.photos_index.parent} 放图再跑 photos）")

    if args.online:
        problems += _check_online(settings)

    print()
    if problems:
        print(f"{BAD} 有 {problems} 个问题要先解决")
        return 1
    print(f"{OK} 配置没问题。")
    print("  注意：check 只是检查，她现在**没在跑**。启动是 python -m newperson run")
    return 0


def _check_online(settings: Settings) -> int:
    """真的连一次 Discord。新手最常卡在这两件事上。

    探针用隐身登录：不然它连上的那几秒她会显示在线，
    你以为跑起来了就去发消息，探针一退出她就离线，消息也没人处理。
    """
    import discord

    print("Discord")
    problems = 0

    class Probe(discord.Client):
        async def on_ready(self) -> None:
            nonlocal problems
            print(f"  {OK} 连上了，身份是 {self.user}")
            try:
                user = await self.fetch_user(settings.owner_user_id)
                await user.create_dm()
                print(f"  {OK} 能私聊 {user.display_name}")
            except discord.Forbidden:
                problems += 1
                print(
                    f"  {BAD} 私聊发不出去。你要和机器人在同一个服务器里，"
                    "并且在那个服务器的隐私设置里允许成员私信你"
                )
            except Exception as exc:  # noqa: BLE001
                problems += 1
                print(f"  {BAD} 找不到你的用户 ID：{exc}")
            await self.close()

    intents = discord.Intents.default()
    intents.message_content = True
    try:
        Probe(intents=intents, status=discord.Status.invisible).run(
            settings.discord_bot_token, log_handler=None
        )
    except discord.PrivilegedIntentsRequired:
        print(
            f"  {BAD} MESSAGE CONTENT INTENT 没打开。"
            "去 Developer Portal 的 Bot 页面打开它，否则她收到的消息内容永远是空的"
        )
        return 1
    except discord.LoginFailure:
        print(f"  {BAD} Token 不对")
        return 1
    return problems


# ---------------------------------------------------------------------------


def cmd_simulate(args: argparse.Namespace) -> int:
    """不联网，只跑作息和时机，看她什么时候回。调参数的时候用这个。

    这里会**模拟一整段对话**，而不是把每条消息当成孤立事件：
    真人聊天是成簇的，他连发两句、她回了之后他马上接话，
    这些时候手机还在手上，跟隔了半天冒出来一句完全不是一回事。
    """
    loaded = load_all(args)
    if loaded is None:
        return 1
    _settings, persona = loaded

    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    attention = AttentionPolicy(persona, rhythm)
    rng = random.Random(args.seed)

    start = datetime.now(tz=persona.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    owner_tz = persona.owner_tz
    heat_names = {"hot": "在聊", "warm": "刚聊过", "cold": "冷了"}

    print(f"{persona.name}　种子 {args.seed}　{args.days} 天")
    print("=" * 88)

    exchanges: list[datetime] = []
    """已经发生过的交流时刻。排在未来的回复不算，那还没发出去。"""
    pending_reply_at: datetime | None = None

    for day_offset in range(args.days):
        day = rhythm.local_date(start + timedelta(days=day_offset))
        daily = rhythm.for_day(day)
        trip = f"　✈ {daily.trip.place}" if daily.trip else ""
        classes = "、".join(c.title for c in daily.classes) or "没课"
        print(
            f"\n{day} 周{'一二三四五六日'[day.weekday()]}　"
            f"{daily.period}／{daily.phase}／{daily.variant}{trip}"
        )
        print(
            f"  {daily.wake.strftime('%H:%M')} 起　{daily.sleep_start.strftime('%H:%M')} 睡　"
            f"{classes}　今天当场回的基准 {daily.engage_probability:.0%}"
        )

        day_start = start + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        sent = day_start + timedelta(minutes=rng.uniform(0, 200))
        last_reply: datetime | None = None

        for _ in range(args.messages_per_day):
            if sent >= day_end:
                break
            snapshot = rhythm.state_at(sent)
            there = f"（他 {sent.astimezone(owner_tz).strftime('%H:%M')}）" if owner_tz else ""
            state = {
                "sleeping": "睡着",
                "busy": snapshot.block_title or "忙",
                "free": "有空",
                "winding_down": "快睡了",
            }[snapshot.state]

            # 已经排着一次回复了，这条并进去一起回，不会单独产生一次
            if pending_reply_at is not None and pending_reply_at > sent:
                heat = heat_of(
                    sent, _last_before(exchanges, sent), None,
                    persona.timing.hot_seconds, persona.timing.warm_seconds,
                )
                pending_reply_at = attention.merge_pending(pending_reply_at, sent, heat, rng)
                print(
                    f"    {sent.strftime('%H:%M')}{there} 他又发　她{state:<6}"
                    f"　　　并进上面那次，一起回"
                )
                exchanges.append(sent)
                last_reply = pending_reply_at
                sent = _next_message_time(sent, last_reply, day_end, rng)
                continue

            features = extract_features([rng.choice(SAMPLE_MESSAGES)], persona)
            heat = heat_of(
                sent,
                _last_before(exchanges, sent),
                None,
                persona.timing.hot_seconds,
                persona.timing.warm_seconds,
            )
            decision = attention.plan_reply(sent, heat, features, sent, rng)
            waited = (decision.reply_at - sent).total_seconds()

            engage = rhythm.engage_probability_at(sent)
            engage_text = "　—　" if engage <= 0 else f"{engage:.0%}"
            print(
                f"    {sent.strftime('%H:%M')}{there} 他发　她{state:<6}"
                f"{heat_names[heat]:<5}当场回 {engage_text:<5}"
                f"→ {decision.reply_at.strftime('%d日%H:%M')}（{_pretty(waited)}）"
            )
            if args.verbose:
                print(f"        {decision.reason}")

            exchanges.append(sent)
            exchanges.append(decision.reply_at)
            pending_reply_at = decision.reply_at
            last_reply = decision.reply_at
            sent = _next_message_time(sent, last_reply, day_end, rng)
    return 0


def _next_message_time(
    prev_sent: datetime, last_reply: datetime | None, day_end: datetime, rng: random.Random
) -> datetime:
    """他下一条消息什么时候发。

    真人的节奏是"她回了我就接着说"，不是在一天里均匀撒点。
    均匀撒点的话每条消息都是隔了很久的孤立事件，
    正在聊天时她回得多快这条路径永远测不到。
    """
    roll = rng.random()
    if last_reply is not None and roll < 0.45:
        # 她刚回完，他接着说
        follow_up = last_reply + timedelta(minutes=rng.uniform(0.3, 5))
        if follow_up < day_end:
            return follow_up
    if last_reply is not None and roll < 0.68:
        # 过了一会儿又想起一件事
        soon = last_reply + timedelta(minutes=rng.uniform(8, 40))
        if soon < day_end:
            return soon
    return prev_sent + timedelta(minutes=rng.uniform(40, 260))


def _last_before(moments: list[datetime], cutoff: datetime) -> datetime | None:
    """cutoff 之前最后一次交流。未来的不算。"""
    past = [t for t in moments if t < cutoff]
    return max(past) if past else None


SAMPLE_MESSAGES = ["在吗", "今天上班好累", "我那个筛选器改完了", "你看这个", "睡了没", "急 帮我看下这个参数"]


def _pretty(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    """真的调一次模型，让她给今天编一份日程。"""
    loaded = load_all(args)
    if loaded is None:
        return 1
    settings, persona = loaded
    setup_logging(settings.log_level)

    async def go() -> int:
        from .brain import Brain, DayPlanRequest, build_client
        from .memory import Memory

        calendar = AcademicCalendar(persona.academic, persona.seed)
        rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
        memory = Memory(settings.db_path)
        await memory.open()
        try:
            now = datetime.now(tz=persona.tz)
            day = rhythm.local_date(now)
            daily = rhythm.for_day(day)
            brain = Brain(build_client(settings), settings, persona, memory)
            plan = await brain.generate_day_plan(
                DayPlanRequest(
                    now=now,
                    state_line=f"{daily.period}。{daily.variant}。",
                    mood_notes=daily.mood_notes,
                    wake_at=daily.wake,
                    sleep_at=daily.sleep_start,
                    classes=[
                        f"{c.title} {c.start.strftime('%H:%M')}-{c.end.strftime('%H:%M')}"
                        for c in daily.classes
                    ],
                    yesterday=await memory.get_day_plan(day - timedelta(days=1)),
                    summary=(await memory.get_conversation("owner")).summary,
                ),
                day,
            )
            if plan is None:
                print(f"{BAD} 没生成出来，看看日志")
                return 1
            print(f"\n{plan.date}　{plan.mood}\n")
            for event in plan.events:
                mark = "◆" if event.shareable else "·"
                print(f"{mark} {event.start}-{event.end}  {event.title}")
                print(f"    {event.detail}")
            if plan.thoughts:
                print("\n心里挂着：" + "；".join(plan.thoughts))
            print("\n（◆ 是她可能会主动跟你提的）")
            if args.save:
                await memory.save_day_plan(day, plan)
                print("已存进日记")
            return 0
        finally:
            await memory.close()

    return asyncio.run(go())


def cmd_photos(args: argparse.Namespace) -> int:
    """扫一遍照片目录，为还没登记的图片生成索引草稿。"""
    loaded = load_all(args)
    if loaded is None:
        return 1
    settings, _persona = loaded

    from .media import PhotoLibrary

    index_path = settings.photos_index
    folder = index_path.parent
    library = PhotoLibrary(index_path)
    library.load()
    known = {Path(p.file).name for p in library.photos}

    found = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    )
    missing = [p for p in found if p.name not in known]

    print(f"{folder}：{len(found)} 张图，索引里有 {len(library.photos)} 条")
    if not missing:
        print(f"{OK} 都登记过了")
        return 0

    print(f"\n下面 {len(missing)} 张还没登记。把这段贴进 {index_path} 的 photos: 下面，")
    print("再补上 tags 和 caption。tags 用英文词，caption 写给她自己看的描述。\n")
    for path in missing:
        pid = path.stem.lower().replace(" ", "-")
        print(f"  - id: {pid}")
        print(f"    file: {path.name}")
        print("    tags: []")
        print("    caption: ")
        print("    taken_hint: ")
        print("    time_of_day: any      # any / morning / day / evening / night")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    """把她记下的交易陈述打出来。

    她拿这个指出你前后矛盾，你可以拿它当自己的交易日志：
    每一笔当时的理由、答应过要做的事，都在里面。
    """
    loaded = load_all(args)
    if loaded is None:
        return 1
    settings, _persona = loaded

    async def go() -> int:
        from .memory import Memory

        memory = Memory(settings.db_path)
        await memory.open()
        try:
            entries = await memory.ledger(args.kind, limit=args.limit)
            if not entries:
                print("还没记下什么。跟她聊到仓位、止损、回测这些的时候她才会记。")
                return 0
            print(f"她记着这些（{args.kind}，{len(entries)} 条，最近的在前）\n")
            for at, entry in entries:
                print(f"{at.strftime('%Y-%m-%d %H:%M')}  {entry.claim}")
                if entry.reason:
                    print(f"{'':20}理由：{entry.reason}")
                if entry.committed_to:
                    print(f"{'':20}你答应：{entry.committed_to}")
                print()
            return 0
        finally:
            await memory.close()

    return asyncio.run(go())


EMPTY_BACKUP = 3
"""``backup`` 拷出来一份没有消息的快照时的返回码。

不是失败——第一天就该是空的。但它和"DB_PATH 配错了"长得一模一样，
所以调用方要能区分对待：可以传上去，但不能拿它去顶掉旧的备份。
"""


def cmd_backup(args: argparse.Namespace) -> int:
    """拷一份一致快照出来，并且当场验一遍。

    验不过就返回非零、**不留下那个文件**——一份坏备份比没有备份更危险，
    它会让你以为自己有退路。
    """
    from . import backup as backup_mod

    settings = load_settings()
    src = Path(args.db) if args.db else settings.db_path
    dest = Path(args.dest)

    try:
        backup_mod.snapshot(src, dest)
    except Exception as exc:  # noqa: BLE001 - 备份失败要说人话，不要甩堆栈
        print(f"{BAD} 拷不出来：{exc}")
        return 1

    errors = backup_mod.integrity_errors(dest)
    if errors:
        print(f"{BAD} 拷出来的文件是坏的，已经删掉：")
        for line in errors[:10]:
            print(f"   {line}")
        dest.unlink(missing_ok=True)
        return 1

    info = backup_mod.stats(dest)
    print(f"{OK} {dest}")
    for line in info.describe():
        print(f"   {line}")
    # 这里**不**写 .last_backup_at。快照只是躺在同一块磁盘上，
    # 那块磁盘没了它也没了。标记由 scripts/backup.sh 在真正传出去之后才写。

    if info.messages == 0:
        # 一条消息都没有的备份是**合法的**——第一天就是这样。
        # 但它也可能是 DB_PATH 指错了、数据卷没挂上。两种情况长得一模一样。
        # 所以不报错（第一天不能失败），而是用一个单独的返回码告诉调用方：
        # 这份可以传，但**不要拿它去顶掉旧的**。
        print(f"{WARN} 这份备份里一条消息都没有")
        print("   要么她还没开始跟你说话，要么 DB_PATH 指错了、data/ 没挂上")
        print("   这份还是会传上去，但旧备份先不清理")
        return EMPTY_BACKUP
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """恢复演练：证明这个文件真的能变回她。

    只看文件在不在是不够的。要跑 integrity_check，还要把里面的东西数出来——
    一个 0 条消息的完好数据库同样完好，但那不是她。
    """
    from . import backup as backup_mod

    path = Path(args.path)
    if not path.exists():
        print(f"{BAD} 找不到 {path}")
        return 1

    try:
        errors = backup_mod.integrity_errors(path)
    except sqlite3.DatabaseError as exc:
        print(f"{BAD} 这不是一个能打开的 SQLite 文件：{exc}")
        return 1
    if errors:
        print(f"{BAD} 文件是坏的：")
        for line in errors[:10]:
            print(f"   {line}")
        return 1

    info = backup_mod.stats(path)
    print(f"{OK} {path} 能用")
    for line in info.describe():
        print(f"   {line}")
    if info.messages == 0:
        print(f"{WARN} 一条消息都没有。文件是好的，但里面不是她——确认一下拿对了没有")
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    loaded = load_all(args)
    if loaded is None:
        return 1
    settings, persona = loaded
    setup_logging(settings.log_level, persona.tz)

    missing = settings.missing_required()
    if missing:
        print(f"{BAD} 还缺：{'、'.join(missing)}。先跑 `python -m newperson check`")
        return 1

    blockers = [m for level, m in validate_persona(persona) if level == "error"]
    if blockers and not args.allow_placeholders:
        print(f"{BAD} 人设还没写完：")
        for message in blockers:
            print(f"   {message}")
        print("填完再跑，或者加 --allow-placeholders 硬跑（她会很平淡）")
        return 1

    from .discord_bot import run

    run(settings, persona)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="newperson", description="接入 Discord 的虚拟人物")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="启动机器人")
    run_p.add_argument("--allow-placeholders", action="store_true", help="人设没写完也硬跑")

    check_p = sub.add_parser("check", help="检查配置和人设")
    check_p.add_argument("--online", action="store_true", help="真的连一次 Discord")

    sim = sub.add_parser("simulate", help="模拟她什么时候回消息，不联网")
    sim.add_argument("--days", type=int, default=3)
    sim.add_argument("--seed", type=int, default=1)
    sim.add_argument("--messages-per-day", type=int, default=5)
    sim.add_argument("--verbose", action="store_true", help="打印每次的判断过程")

    plan_p = sub.add_parser("plan", help="让她编一份今天的日程")
    plan_p.add_argument("--save", action="store_true", help="存进日记")

    sub.add_parser("photos", help="扫描照片目录生成索引草稿")

    led = sub.add_parser("ledger", help="打印她记下的、你在交易上说过的话")
    led.add_argument("--kind", default="trading", help="trading / study / shift / project / english / sleep")
    led.add_argument("--limit", type=int, default=50)

    bak = sub.add_parser("backup", help="把她的记忆拷一份出来（一致快照，拷完就验）")
    bak.add_argument("dest", help="快照写到哪")
    bak.add_argument("--db", default="", help="源数据库，默认用 DB_PATH")

    ver = sub.add_parser("verify", help="检查一份备份还能不能用")
    ver.add_argument("path", help="要检查的 .db 文件")

    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "check": cmd_check,
        "simulate": cmd_simulate,
        "plan": cmd_plan,
        "photos": cmd_photos,
        "ledger": cmd_ledger,
        "backup": cmd_backup,
        "verify": cmd_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
