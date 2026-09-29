"""Discord 适配层与组合根。

这里把所有模块串起来，并处理和 Discord 打交道的那部分：收消息、发消息、在线状态。

几个关键决定：

- **在线状态跟着行为走，不跟着时钟走。** 每天准点上线下线是最大的破绽。
  她睡觉时离线，醒着时默认 idle（手机在口袋里），只有真的在看手机的那几分钟才 online。
- **她只有一部手机。** 一次把所有未读一起处理，不会出现一边发主动消息、
  一边让半小时前的私信躺着没读。但"已读"要等她真的想好了怎么回才落库，
  模型没给出结果时消息还留着，重试时不会凭空消失。
- **``!np`` 开头的消息不入库、不进模型。** 她不知道你在操控她。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from . import owner as owner_cmds
from .attention import SLEEP_SOON_HINT, AttentionPolicy, extract_features, heat_of
from .brain import Brain, MemoryUpdateRequest, ProactiveRequest, ReplyRequest, build_client
from .calendar import AcademicCalendar
from .clock import Clock, RealClock, later
from .config import Settings
from .delivery import Deliverer, DeliveryBlocked
from .life import LEDGER_CHECK, LifeEngine
from .media import CommandImageGenerator, MediaService, NullImageGenerator, PhotoLibrary
from .memory import Memory
from .models import (
    IncomingMessage,
    Job,
    LedgerEntry,
    PhotoRequest,
    RhythmSnapshot,
    TimeOfDay,
)
from .persona import Persona
from .prompts import build_situation
from .rhythm import Rhythm
from .scheduler import Scheduler

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 20.0
"""下一张图最多等这么久（秒）。见 _download_images 里为什么不能不设。"""
CONVERSATION_ID = "owner"
PHOTO_SHORTLIST = 20
"""进上下文的照片最多这么多条，免得库大了把提示词撑爆。"""

CATCHUP_CURSOR = "catchup_cursor:"
"""补抓的游标前缀，后面接频道 id。**一个频道一个**，见 _catch_up_channels。"""

MEMORY_RETRY_BACKOFF = timedelta(hours=6)
"""记忆整理失败之后隔这么久才再试。见 _maybe_summarize 为什么不能不退避。"""

CATCHUP_SEEN = "catchup_channels"
"""见过的入口频道 id，逗号分隔。用来在开始翻之前就把每个入口的基线钉住。"""

LAST_INBOUND = "last_inbound_channel"
"""他最后一次说话的频道。消息表里不存频道，补救路径只能靠它才知道该回哪儿。"""

SIGNED_OFF_UNTIL = "signed_off_until"
"""她今晚已经说过要睡了，值是那晚的入睡时刻。那之前不再另起话头、不再道一次晚安。"""


def time_of_day_at(dt: datetime) -> TimeOfDay:
    """给照片挑选用的时段。半夜不该发白天拍的照片。"""
    hour = dt.hour
    if hour < 5 or hour >= 23:
        return "night"
    if hour < 11:
        return "morning"
    if hour < 17:
        return "day"
    return "evening"


def _tagged(photos: list, tags: list[str] | None) -> list:
    """只留标签对得上的。没给标签就全留。"""
    if not tags:
        return photos
    wanted = set(tags)
    return [p for p in photos if wanted & set(p.tags)]


def _said_at(unread: list) -> datetime | None:
    """他这一批里最晚那句是几点说的。续发那条路上可能拿不到，就是 None。"""
    return max((m.created_at for m in unread), default=None)


class App:
    """组合根。所有任务的 handler 都挂在这里。"""

    def __init__(
        self,
        settings: Settings,
        persona: Persona,
        clock: Clock,
        rhythm: Rhythm,
        calendar: AcademicCalendar,
        attention: AttentionPolicy,
        memory: Memory,
        scheduler: Scheduler,
        brain: Brain,
        media: MediaService,
        life: LifeEngine,
        deliverer: Deliverer,
        rng: random.Random,
    ) -> None:
        self.settings = settings
        self.persona = persona
        self.clock = clock
        self.rhythm = rhythm
        self.calendar = calendar
        self.attention = attention
        self.memory = memory
        self.scheduler = scheduler
        self.brain = brain
        self.media = media
        self.life = life
        self.deliverer = deliverer
        self.rng = rng
        self.client: discord.Client | None = None
        self._default_channel: Any = None
        self._channels: dict[int, Any] = {}
        self._started = False
        self._catching_up = False
        """补抓正在跑。重连风暴时两个 on_ready 会重叠。"""
        self._missing_channel = False
        """上一轮补抓有没有该翻却拿不到的频道。"""
        self._tasks: set[asyncio.Task] = set()
        """留着引用。只 create_task 不保存的话，任务可能被 GC 掉，循环无声无息就停了。"""
        # 照片库空着的时候，要照片的那几种主动不进当天的候选。
        # 原来它照样抽签、占掉名额，到点再发现没照片跳过——那天就什么都不说了。
        self.life.has_photos = lambda tags: bool(
            _tagged(self.media.library.available(set(), None), tags)
        )
        self.on_phone: Callable[[], Awaitable[None]] | None = None
        """她拿起手机了（在线状态马上亮，亮几分钟）。连上 Discord 之后由 client 接上。"""

    # -- 启动 ---------------------------------------------------------------

    async def start(self, client: discord.Client) -> None:
        """网关就绪之后跑一次。

        ``on_ready`` 会被反复触发：断线之后 RESUME 失败就重新 IDENTIFY，
        长跑的机器人一天可能好几次。不挡住的话每次都会再开一条 SQLite 连接
        （旧的从不关闭）、再起一个 presence 循环，最后撞上 Discord 的
        presence 频率限制，而被限流又会导致断线重连，正反馈。
        """
        self.client = client
        if self._started:
            log.info("[app] 网关重连了，沿用已有的状态")
            return
        await self.memory.open()
        self.scheduler.register("reply", self.handle_reply_job)
        self.scheduler.register("proactive", self.handle_proactive_job)
        self.scheduler.register("follow_up", self.handle_proactive_job)
        self.scheduler.register("sign_off", self.handle_sign_off_job)
        self.scheduler.register("day_plan", self.life.handle_day_plan_job)
        self.scheduler.register("memory_update", self.handle_memory_update_job)

        await self.scheduler.recover()
        self._prune_downloads()
        self._prune_downloads(folder=self.settings.generated_dir)
        await self.life.schedule_next_day_plan()
        plan = None
        if not self.rhythm.is_sleeping(self.clock.now()):
            plan = await self.life.ensure_today_plan(CONVERSATION_ID)
        # 第一次上线时先说一句。有了今天的日程她才有具体的事可说，
        # 所以排在 ensure_today_plan 后面。
        await self.life.ensure_opener(CONVERSATION_ID, plan)

        # **标记要放在这儿，不能放在开头。**
        # 放开头的话，中间任何一步抛异常（数据库打不开、日程生成炸了）
        # 都会留下一个"已启动"的半残进程：discord.py 吞掉 on_ready 的异常继续跑，
        # 网关连着、头像亮着、消息也收得到并入库，但调度循环从来没起来——
        # 她永远不回你，而重连时 _started 已经是真，直接早返回，永远修不好。
        # 唯一的症状就是她不说话，而那正是她的正常状态。
        self._started = True
        self.spawn(self.scheduler.run_forever(), "scheduler")

        now = self.clock.now()
        snapshot = self.rhythm.state_at(now)
        state_names = {
            "sleeping": "在睡觉",
            "busy": snapshot.block_title or "在忙",
            "free": "有空",
            "winding_down": "准备睡了",
        }
        # 日志里的时间**全部是她那边的**。你和她多半不在一个时区，
        # 不说清楚的话会一直对不上。
        line = f"[app] {self.persona.name} 上线了。她那边 {now.strftime('%m-%d %H:%M')}，{state_names[snapshot.state]}"
        if self.persona.owner_tz:
            line += f"（你那边 {now.astimezone(self.persona.owner_tz).strftime('%H:%M')}）"
        log.info(line)
        log.info(
            "[app] 日志里的时间都是她那边的时间（%s）", self.persona.timezone
        )
        # **打绝对路径。** DB_PATH 默认是相对的（data/newperson.db），
        # 相对谁取决于进程的工作目录——systemd 不写 WorkingDirectory 时是 /，
        # 于是她会在 /data/ 下面开一个全新的空库，而你在仓库里怎么看都看不出问题：
        # 她不记得任何事，备份备的是空的，日志里一切正常。
        # 真出过一次：同一个 opener 任务在两次启动里都拿到了 id 2，
        # 而 jobs 表是 AUTOINCREMENT，同一个库里 id 绝不会重复——
        # 那是两个库。写一行绝对路径，这种事一眼就能看出来。
        log.info("[app] 记忆在 %s", Path(self.settings.db_path).resolve())
        if self.settings.force_awake:
            log.warning("[app] DEBUG_FORCE_AWAKE 开着，她不会睡觉。看完效果记得关掉")
        if self.settings.delay_scale != 1.0:
            log.warning(
                "[app] DELAY_SCALE=%s，等待时间被压缩了 %.0f 倍",
                self.settings.delay_scale,
                1 / self.settings.delay_scale,
            )

    def spawn(self, coro, name: str = "") -> asyncio.Task:
        """起一个后台循环，留住引用，并且死了要有日志。

        循环无声无息地停掉是最难查的故障：你只会觉得她再也不理你了。
        """
        task = asyncio.create_task(coro, name=name or None)
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            if exc := finished.exception():
                log.error("[app] 后台循环 %s 停了：%r", name or finished.get_name(), exc)

        task.add_done_callback(_done)
        return task

    def _prune_downloads(self, keep_days: float = 14, folder: Path | None = None) -> None:
        """本地的图片久了会把磁盘撑满。模型早就看过了，留两周够了。"""
        folder = folder or self.settings.downloads_dir
        if not folder.exists():
            return
        cutoff = self.clock.now().timestamp() - keep_days * 86400
        removed = 0
        for path in folder.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:  # 删不掉就算了，不值得为这个崩
                continue
        if removed:
            log.info("[app] 清掉 %d 张过期的图片", removed)

    async def resolve_channel(self, channel_id: int | None = None) -> Any:
        """她说话的地方。

        **要回到他说话的那个地方。** 配了 ``PROACTIVE_CHANNEL_ID`` 之后，
        如果一律回到那个频道，他在私聊说的话就会被回到公开频道里，
        而且引用回复和表情反应会因为消息不在那个频道而全部静默失效。
        所以回复走消息的来源，只有主动消息才用默认频道。
        """
        if self.client is None:
            raise RuntimeError("Discord 还没连上")

        if channel_id is not None:
            cached = self._channels.get(channel_id)
            if cached is None:
                cached = self.client.get_channel(channel_id) or await self.client.fetch_channel(
                    channel_id
                )
                self._channels[channel_id] = cached
            return cached

        if self._default_channel is None:
            if self.settings.proactive_channel_id:
                self._default_channel = await self.resolve_channel(
                    self.settings.proactive_channel_id
                )
            else:
                user = self.client.get_user(
                    self.settings.owner_user_id
                ) or await self.client.fetch_user(self.settings.owner_user_id)
                self._default_channel = user.dm_channel or await user.create_dm()
        return self._default_channel

    # -- 收消息 -------------------------------------------------------------

    async def catch_up(self, page: int = 100, max_pages: int = 10) -> int:
        """把停机期间漏掉的消息补回来。

        **Discord 不补发新会话之前的事件。** 进程每重启一次就是一个新会话，
        所以部署、宿主机维护、崩溃拉起的那几十秒里他说的话，
        原来是不入库、不进未读、她永远不会回——而且他不会收到任何提示。
        一次部署要好几分钟，这个窗口一点都不小。

        几条关键细节：

        - **按"他会在哪说话"去找，不是"她主动消息发去哪"。**
          ``resolve_channel()`` 不带参数返回的是主动消息的目的地——配了
          ``PROACTIVE_CHANNEL_ID`` 就是那个公开频道。拿它去补抓的话，
          他在私聊里说的话一条都找不回来，而游标还会被公开频道的消息推过去，
          于是那些私聊消息**永久**跳过。所以这里两个入口都翻。
        - **翻页翻到底。** 只翻一页的话，超出的部分不但这次补不到，
          游标推进之后就再也补不到了——而分页是从旧往新走的，
          被丢掉的恰恰是他**最后说的**那几条。
        - **用消息本身的时间。** 正常收消息存的是 ``clock.now()``，
          实时收时两者差不多；补抓时差好几个小时，存成"刚刚"的话
          她会按"刚收到"算回复时机，三小时前的话被秒回过去。
        - **库是空的就什么都不做。** 不然第一次上线会把整段历史拉进来，
          而那正是"她不该知道的事"。
        """
        newest = await self.memory.newest_discord_message_id(CONVERSATION_ID)
        if newest is None:
            return 0
        if self._catching_up:
            return 0  # 重连风暴时两个 on_ready 会重叠，别让它们互相数重复

        self._catching_up = True
        try:
            return await self._catch_up_channels(newest, page, max_pages)
        finally:
            self._catching_up = False

    async def _remember_inbound(self, channel_id: int | None) -> None:
        """记下他最后一次说话的地方。消息表里不存频道，只能单独记一份。

        顺手记进 ``CATCHUP_SEEN``。**私聊的 id 只能从这儿知道**——
        它不在配置里（配置里只有公开频道那个），而 `_pin_baselines`
        只钉得住"已知"的频道。私聊第一次解析失败的那一轮
        （`on_ready` 正是最容易撞限流的时刻），它连基线都不会建，
        而同一轮公开频道补抓成功会把全库最大编号推上去——
        下一轮私聊恢复，起点就落在被推过去的位置，中间的话永久跳过。
        实测两百个随机场景里有十一个这样丢消息，丢的全是私聊的。
        """
        if not isinstance(channel_id, int):
            return
        await self.memory.kv_set(LAST_INBOUND, str(channel_id))
        raw = await self.memory.kv_get(CATCHUP_SEEN) or ""
        known = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        if channel_id not in known:
            known.add(channel_id)
            await self.memory.kv_set(CATCHUP_SEEN, ",".join(str(i) for i in sorted(known)))

    async def _pin_baselines(self, newest: int) -> None:
        """**开始翻之前**就把每个已知入口的游标基线钉住。

        只在"翻到这个频道"的时候才钉是不够的：某一轮拿不到它
        （限流、权限刚变），它连 kv 都不会被建；而同一轮另一个频道补抓成功，
        会把库里最大的编号推上去。等下一轮它恢复了，起点就落在被推过去的位置，
        中间他在那儿说的话永久跳过。实测两百个随机场景里有二十九个这样丢消息。

        公开频道的 id 在配置里就有，拿不到频道也知道；私聊的 id 第一次
        解析成功之后记进 kv，之后就一直知道了。
        """
        known: set[int] = set()
        if self.settings.proactive_channel_id:
            known.add(self.settings.proactive_channel_id)
        raw = await self.memory.kv_get(CATCHUP_SEEN) or ""
        known |= {int(x) for x in raw.split(",") if x.strip().isdigit()}
        for cid in known:
            key = f"{CATCHUP_CURSOR}{cid}"
            if not await self.memory.kv_get(key):
                await self.memory.kv_set(key, str(newest))

    async def _remember_channel_ids(self, channels: list[Any]) -> None:
        raw = await self.memory.kv_get(CATCHUP_SEEN) or ""
        known = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        known |= {c.id for c in channels if isinstance(getattr(c, "id", None), int)}
        await self.memory.kv_set(CATCHUP_SEEN, ",".join(str(i) for i in sorted(known)))

    async def _inbound_channels(self) -> list[Any]:
        """他可能说话的所有地方。私聊永远算一个。拿不到的记在 ``_missing_channel`` 上。"""
        self._missing_channel = False
        found: list[Any] = []
        seen: set[int] = set()
        targets = [None]
        if self.settings.proactive_channel_id:
            targets.append(self.settings.proactive_channel_id)
        for target in targets:
            try:
                channel = (
                    await self.resolve_channel(target)
                    if target is not None
                    else await self._owner_dm()
                )
            except Exception:  # noqa: BLE001 - 拿不到一个不该拖垮另一个
                log.warning("[inbox] 补抓时拿不到频道 %s，跳过", target)
                self._missing_channel = True
                continue
            if channel is None:
                self._missing_channel = True
            # 按频道 id 去重，不按对象身份：私聊和 PROACTIVE_CHANNEL_ID
            # 指同一个地方时，两条路径未必拿到同一个对象，重了就会数两遍。
            key = getattr(channel, "id", None)
            key = key if isinstance(key, int) else id(channel)
            if channel is not None and key not in seen:
                seen.add(key)
                found.append(channel)
        return found

    async def _owner_dm(self) -> Any:
        """他的私聊。**不走 resolve_channel()**，那个无参分支返回的是主动消息的去处。"""
        if self.client is None:
            raise RuntimeError("Discord 还没连上")
        user = self.client.get_user(self.settings.owner_user_id) or await self.client.fetch_user(
            self.settings.owner_user_id
        )
        return user.dm_channel or await user.create_dm()

    async def _catch_up_channels(self, newest: int, page: int, max_pages: int) -> int:
        """逐个频道往回翻。**每个频道各有各的游标。**

        共用一个全局游标（库里最大的那个 discord_message_id）会永久丢消息：
        私聊补抓成功、公开频道正好限流，游标就被私聊那条推了过去，
        下次重连时公开频道从那个位置往后翻——中间他在公开频道说的话
        **再也不会被翻到**。两个频道的编号各走各的，一个共用的高水位管不住它们。

        游标只推进到"这之前都处理妥当了"为止。中途抛异常、某一条处理不了，
        它就停在那儿，下次从那里重来（重复的 ``add_user_message`` 返回 0，
        不会重复排队）。但**页数用满不算出错**——那些页是好好处理完的，
        游标照常跟到底，否则下次重连会从同一个位置重翻同样的内容，永远翻不过去。
        """
        recovered = 0
        restored_hers = 0
        hers_latest: datetime | None = None
        command_replies = await self._command_reply_ids()
        marker = self._restore_marker()
        restoring = marker.exists()
        """刚从备份恢复。只有这时才补她自己说过的话。"""
        complete = True
        """这一轮每个频道都翻到底、没出错。没有的话恢复记号留着，下次重连接着补。"""
        latest: tuple[datetime, int | None] | None = None
        """他最后说话的时刻和地方。按时间取最大，**不是按遍历顺序取最后一个**——
        私聊先翻、公开频道后翻的话，取遍历顺序会把他在私聊里说的最后一句
        回到公开频道去。"""

        channels = await self._inbound_channels()
        await self._remember_channel_ids(channels)
        await self._pin_baselines(newest)

        for channel in channels:
            key = f"{CATCHUP_CURSOR}{getattr(channel, 'id', 0)}"
            stored_cursor = await self.memory.kv_get(key)
            if (stored_cursor or "").isdigit():
                start = int(stored_cursor)
            else:
                # 第一次见到这个频道：**当场把基线钉住**，别等翻成功了才记。
                # 不钉的话，这个频道只要一次没翻成（限流、权限没给全），
                # 下次它的起点就变成了"全库最大编号"——而那个编号已经被
                # 另一个频道推过去了，中间的消息就此永久跳过。
                start = newest
                await self.memory.kv_set(key, str(start))
            cursor = start
            safe = start
            """确认**全部处理妥当**的位置，只有它会被写回去。

            和 ``cursor`` 分开是因为两件事不一样：翻页要往前走，
            记账只能记到"这之前都没问题"为止。中间抛了异常、某一条处理不了，
            ``safe`` 就停在那儿，下次从那里重来；页数用满不是出错，
            那些页是好好处理完的，``safe`` 照常跟到底。
            """
            stuck = False
            last_command_at: datetime | None = None
            """这个频道里他上一条 !np 的时刻。紧跟着的那句是程序的回执，不是她说的。"""
            her_batch: datetime | None = None
            """她正在回的那一批（补回来的连续几个气泡属于同一批）。他一开口就清掉。"""
            for _ in range(max_pages):
                try:
                    batch = [
                        m
                        async for m in channel.history(
                            limit=page, after=discord.Object(id=cursor), oldest_first=True
                        )
                    ]
                except Exception:  # noqa: BLE001 - 限流、权限变更都不该让启动失败
                    log.warning("[inbox] 补抓历史失败，跳过", exc_info=True)
                    complete = False
                    break
                if not batch:
                    break
                for message in batch:
                    cursor = max(cursor, message.id)
                    if self._is_hers(message):
                        if not restoring:
                            # 平时重连：她说过的话库里都有了（或者本来就不记，比如纯图片）。
                            if not stuck:
                                safe = cursor
                            continue
                        if self._is_command_reply(message, command_replies, last_command_at):
                            # !np 的回执不入库、不进模型（她不知道你在操控她），
                            # 也不能算作"她回过了"——那会把他的未读吞掉。
                            if not stuck:
                                safe = cursor
                            continue
                        try:
                            got, her_batch = await self._recover_her_message(message, her_batch)
                            if got is not None:
                                restored_hers += 1
                                hers_latest = max(hers_latest or got, got)
                        except Exception:  # noqa: BLE001 - 跟他的消息一样：一条不行，别连累整批
                            log.warning("[inbox] 补抓时她这条记不下，跳过 %s", message.id, exc_info=True)
                            stuck = True
                            continue
                        if not stuck:
                            safe = cursor
                        continue
                    if not self._should_handle(message) or owner_cmds.is_command(message.content):
                        # 不该处理的、以及 !np（给程序看的，补抓时更不该执行）：
                        # 跳过不等于没处理妥当，游标照常跟上，否则别人的闲聊
                        # 会把这个频道永远卡在原地。
                        if owner_cmds.is_command(message.content):
                            last_command_at = message.created_at
                        if not stuck:
                            safe = cursor
                        continue
                    her_batch = None
                    at = message.created_at.astimezone(self.persona.tz)
                    # **一条处理不了不能连累整批。** 下附件要联网、要写盘，
                    # 磁盘满了或者 aiohttp 抛一下就够了。往外抛的后果不是少补几条：
                    # 前面几条已经进库，而函数在排回复之前退出，没人给它们排；
                    # 下次重连全是重复（返回 0），recovered 还是 0，还是没人排。
                    # 那些话就永远躺在库里没人回。
                    try:
                        stored = await self.memory.add_user_message(
                            IncomingMessage(
                                conversation_id=CONVERSATION_ID,
                                discord_message_id=message.id,
                                author_id=message.author.id,
                                author_name=message.author.display_name,
                                content=message.content,
                                attachments=await self._download_images(message),
                                created_at=at,
                            )
                        )
                    except Exception:  # noqa: BLE001 - 单条失败，跳过它，别停下
                        log.warning("[inbox] 补抓时这条处理不了，跳过 %s", message.id, exc_info=True)
                        stuck = True  # 从这条起都不算数了，下次重来
                        continue
                    if not stuck:
                        safe = cursor
                    if stored:
                        recovered += 1
                        if latest is None or at > latest[0]:
                            latest = (at, getattr(message.channel, "id", None))
                            await self._remember_inbound(latest[1])
                if len(batch) < page:
                    break
            else:
                # 页数用满**不是出错**。这些页是好好处理完的，游标必须跟上，
                # 否则下次重连从同一个位置重翻同样这些，全是重复，
                # 于是永远翻不过去——"下次重连继续"就成了一句空话，
                # 而且每次重连都白烧 max_pages 次 history 调用。
                log.info(
                    "[inbox] 补抓翻满 %d 页，剩下的下次重连接着翻", max_pages
                )
                complete = False
            if stuck:
                complete = False
            if safe > start:
                await self.memory.kv_set(key, str(safe))

        if self._missing_channel:
            complete = False
        if restoring and complete:
            with contextlib.suppress(OSError):
                marker.unlink()
            log.info("[inbox] 恢复之后的补抓做完了")
        if restored_hers and hers_latest is not None:
            log.info("[inbox] 她自己说过、库里没有的 %d 条也补上了（多半是刚从备份恢复）", restored_hers)
            # 频道是一个一个翻的：他在公开频道说的那句，翻私聊时还没入库，
            # 她在私聊那句回复就没法把它算成回过了。一次回复本来就合并所有频道的未读，
            # 所以翻完之后，早于她补回来的最后一句的未读都是她当时回过的。
            older = [
                m.id
                for m in await self.memory.unread_messages(CONVERSATION_ID)
                if m.created_at <= hers_latest
            ]
            if older:
                await self.memory.mark_read(older, hers_latest)
            # 恢复出来的库里还排着备份那会儿的主动消息。排期早于她补回来的最后一句的，
            # 多半在备份之后已经发过了（Discord 上就有），再发一遍就是同一句话说两次。
            dropped = await self.memory.cancel_initiatives_due_before(hers_latest)
            if dropped:
                log.info("[inbox] 备份之后多半已经说过的 %d 个主动任务作废", dropped)
        if recovered and await self.memory.unread_messages(CONVERSATION_ID):
            log.info("[inbox] 停机期间漏了 %d 条，补回来了", recovered)
            # 回到他说话的那个地方，不是默认频道
            await self._schedule_reply(self.clock.now(), channel_id=latest[1] if latest else None)
        elif recovered:
            log.info("[inbox] 补回 %d 条，她当时都回过了，不用再回", recovered)
        elif await self.memory.unread_messages(CONVERSATION_ID) and not await self.memory.pending_jobs(
            "reply", CONVERSATION_ID
        ):
            # 上一轮补抓半途出了事：消息已经进库，那一轮却没走到排回复这一步。
            # 这一轮它们全是重复，recovered 是 0——光看 recovered 的话，
            # 永远没人给它们排，那些话就永远没人回。
            log.warning("[inbox] 有未读却没有排着的回复，补排一条")
            await self._schedule_reply(self.clock.now())
        return recovered

    BUBBLE_GAP = timedelta(minutes=3)
    """补回来的她的气泡，彼此隔多久以内算同一次回复。"""

    COMMAND_REPLY_KEY = "command_reply_ids"
    COMMAND_REPLY_WINDOW = timedelta(seconds=5)
    """老版本没记回执的编号，只能认"紧跟在 !np 后面几秒内"的那句。
    她正常回一句要调模型、要打字，不会这么快。"""

    async def _remember_command_reply(self, message_id: int | None) -> None:
        """记下 !np 回执的编号。补抓时要认出它：它是程序说的，不是她说的。"""
        if not message_id:
            return
        ids = await self._command_reply_ids()
        ids.add(int(message_id))
        await self.memory.kv_set(self.COMMAND_REPLY_KEY, json.dumps(sorted(ids)[-200:]))

    async def _command_reply_ids(self) -> set[int]:
        raw = await self.memory.kv_get(self.COMMAND_REPLY_KEY)
        try:
            return {int(i) for i in json.loads(raw)} if raw else set()
        except (ValueError, TypeError):
            return set()

    def _is_command_reply(
        self, message: discord.Message, known: set[int], last_command_at: datetime | None
    ) -> bool:
        if message.id in known:
            return True
        if last_command_at is None:
            return False
        gap = message.created_at - last_command_at
        return timedelta(0) <= gap <= self.COMMAND_REPLY_WINDOW

    def _is_hers(self, message: discord.Message) -> bool:
        """这条是她自己在私聊（或者那个公开频道）里发的。"""
        me = getattr(getattr(self.client, "user", None), "id", None)
        if me is None or message.author.id != me:
            return False
        if isinstance(message.channel, discord.DMChannel):
            return True
        return bool(
            self.settings.proactive_channel_id
            and message.channel.id == self.settings.proactive_channel_id
        )

    def _restore_marker(self) -> Path:
        """restore.sh --install 装好库之后留的记号，跟 .last_backup_at 放在一起。"""
        return Path(self.settings.db_path).parent / ".just_restored"

    async def _recover_her_message(
        self, message: discord.Message, continuing: datetime | None
    ) -> tuple[datetime | None, datetime | None]:
        """把她在 Discord 上说过、库里却没有的一句补进来。

        返回 ``(补进来的那句的时刻或 None, 这一批的批次)``。``continuing`` 是
        她上一个气泡所在的那一批：连着发的几个气泡是同一次回复，体检才不会把
        后面那几个算成主动开口。

        平时重启用不上：停机期间她本来就没说话，游标之后只有他的消息。
        **从备份恢复之后才用得上**：备份之后那段时间她其实都回过了，
        那些回复还留在他手机上。只补他那一半的话，他的话全变成未读，
        她会把几小时前、早就回过的话再回一遍，而上下文里又没有自己当时说了什么。

        她这句之前、还挂在未读里的他的话，就是她当时在回的那一批：
        标成已读（read_at 取她这句的时刻），她这句的 reply_batch 也记成它，
        体检才分得清哪句是回复、哪句是主动开口。
        """
        at = message.created_at.astimezone(self.persona.tz)
        answered = [
            m.id
            for m in await self.memory.unread_messages(CONVERSATION_ID)
            if m.created_at <= at
        ]
        # 连着发的几个气泡才算同一次回复。隔了几个小时她自己又开口，那是主动开口。
        if continuing is not None and at - continuing > self.BUBBLE_GAP:
            continuing = None
        batch = at if answered else continuing
        stored = await self.memory.add_bot_message(
            CONVERSATION_ID,
            message.content,
            at,
            discord_message_id=message.id,
            # 纯图片那条也要记上"有张图"，不然上下文里是一行空白的"我："
            attachments=[
                {"url": getattr(a, "url", ""), "filename": getattr(a, "filename", "")}
                for a in getattr(message, "attachments", []) or []
            ],
            reply_batch=batch,
        )
        # **只有真的新补进来的那句才能把之前的未读算作"她回过了"。**
        # 库里已经有的那句拿来标已读的话，他在她打字那几秒里插的一句就被吞了。
        if not stored:
            return None, continuing
        if answered:
            await self.memory.mark_read(answered, at)
        return at, batch

    def _should_handle(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if self.client and message.author.id == getattr(self.client.user, "id", None):
            return False
        if message.author.id not in self.settings.all_allowed_user_ids:
            return False
        if isinstance(message.channel, discord.DMChannel):
            return True
        return bool(
            self.settings.proactive_channel_id
            and message.channel.id == self.settings.proactive_channel_id
        )

    async def on_user_message(self, message: discord.Message) -> None:
        if not self._should_handle(message):
            return

        # !np 开头的是给程序看的，不入库也不进模型
        if owner_cmds.is_command(message.content) and message.author.id == self.settings.owner_user_id:
            reply = await owner_cmds.handle(
                message.content,
                owner_cmds.OwnerContext(
                    memory=self.memory,
                    rhythm=self.rhythm,
                    scheduler=self.scheduler,
                    life=self.life,
                    conversation_id=CONVERSATION_ID,
                    max_calls_per_day=self.settings.max_calls_per_day,
                    now=self.clock.now(),
                ),
            )
            sent = await message.channel.send(reply)
            await self._remember_command_reply(getattr(sent, "id", None))
            return

        now = self.clock.now()
        attachments = await self._download_images(message)
        stored_id = await self.memory.add_user_message(
            IncomingMessage(
                conversation_id=CONVERSATION_ID,
                discord_message_id=message.id,
                author_id=message.author.id,
                author_name=message.author.display_name,
                content=message.content,
                attachments=attachments,
                created_at=now,
            )
        )
        if not stored_id and await self.memory.pending_jobs("reply", CONVERSATION_ID):
            return  # 网关重发，而且这条已经排着回复了，不用再动
        if not stored_id:
            # 重发，但**没有任何排着的回复**——说明上一次在"已入库"和"已排期"
            # 之间断了（`_schedule_reply` 那一步要再写一次 jobs 表，
            # 备份正在跑的时候 SQLite 可能 `database is locked`），
            # 而上层把异常吞成一行日志。原来这里无条件 return，
            # 网关重发这道天然的补救就被挡在门外：那条消息进了库没人回，
            # 又因为它还未读，她连主动消息都发不出来——整个人哑掉，
            # 一直到下次重连补抓才救得回来。
            log.warning("[inbox] 重发的这条没有排着回复，补排一次")

        log.info(
            "[inbox] %d 字%s", len(message.content), " 带图" if attachments else ""
        )
        await self._remember_inbound(getattr(message.channel, "id", None))
        await self._schedule_reply(now, channel_id=message.channel.id)
        try:
            await self.life.maybe_schedule_sign_off(CONVERSATION_ID)
        except Exception:  # noqa: BLE001 - 睡前那一句排不上无所谓，不能连累回复
            log.warning("[life] 睡前那一句没排上", exc_info=True)

    async def _schedule_reply(self, now: datetime, channel_id: int | None = None) -> None:
        if channel_id is None:
            # **不知道回哪儿的时候，回他最后说话的地方，不是默认频道。**
            # `resolve_channel(None)` 返回的是主动消息的去处——配了
            # PROACTIVE_CHANNEL_ID 就是那个公开频道。补抓的补救路径
            # （上一轮半途炸了、这一轮全是重复）走的正是这条，
            # 于是他在私聊里说的话会被当着别人的面回出去。
            # 消息表里不存频道，所以这个值得单独记一份。
            channel_id = await self._last_inbound_id()
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        unread = await self.memory.unread_messages(CONVERSATION_ID)
        if not unread:
            return

        # 热度看的是这批消息**到来之前**对话有多热。
        # 用会话表上的 last_user_message_at 是错的：那个字段已经被刚收到的
        # 这条消息更新过了，间隔永远是 0，于是永远判成热聊，她就永远秒回。
        # **要这批里最小的 id，不是第一条的 id。** 未读现在按时间排，
        # 而补抓是一个频道一整段写库的，时间序和 id 序不再一致：
        # 私聊先翻（id 大、时间晚）、公开频道后翻（id 小、时间早）时，
        # `unread[0].id` 是个大的，`last_exchange_before` 就把同一批里的
        # 另一条也捞了进来——那条比 unread[0] 还晚，`heat_of` 把它滤掉，
        # 于是什么都没有，一律判成 cold。他两分钟前还在说话，她等十五分钟才回。
        prior = await self.memory.last_exchange_before(
            CONVERSATION_ID, min(m.id for m in unread)
        )
        heat = heat_of(
            unread[0].created_at,
            prior,
            None,
            self.persona.timing.hot_seconds,
            self.persona.timing.warm_seconds,
        )

        # 消息本身已经放了很久（补抓、长时间停机）的话，这段对话现在就不热了。
        # heat 问的是"这批消息到来时对话有多热"，那是对的；但她三小时后才看到，
        # 不该再用"手机就在手上"的速度回——那正好跟进程重启对上，很显眼。
        staleness = (now - unread[-1].created_at).total_seconds()
        if staleness > self.persona.timing.warm_seconds:
            heat = "cold"
        elif staleness > self.persona.timing.hot_seconds and heat == "hot":
            heat = "warm"

        # 特征要按**整批未读**重新算，合并那条路上尤其重要。
        features = extract_features(
            [m.content for m in unread],
            self.persona,
            has_image=any(m.attachments for m in unread),
        )

        pending = await self.memory.pending_jobs("reply", CONVERSATION_ID)
        if pending:
            # 他还在连着发，就等他说完再一起回，但不会无限等下去
            job = pending[0]
            new_at = self.attention.merge_pending(job.run_at, now, heat, self.rng)
            # 并进已排的那条时，payload 要跟着整批未读走：
            #
            # - **目的地**：不更新的话任务记的还是旧频道，他在私聊说的话会被回到公开频道去。
            # - **is_question / mode**：原来只更新频道，这两个留着第一条消息算出来的。
            #   "刚到家"后面接一句"你明天有空吗"，任务里还记着 is_question=False、
            #   mode=None——于是那个问题按闲聊处理，该换的口吻也没换。
            #   他连发的时候后一条往往才是正事，这一条恰好最常发生。
            payload = dict(job.payload)
            fresh = {"mode": features.mode, "is_question": features.is_question}
            if channel_id is not None:
                fresh["channel_id"] = channel_id
            if any(payload.get(k) != v for k, v in fresh.items()):
                payload.update(fresh)
                await self.scheduler.reschedule(job.id or 0, new_at, payload)
            else:
                await self.scheduler.reschedule(job.id or 0, new_at)
            log.info("[timing] 他还在打字，回复推到 %s", new_at.strftime("%H:%M"))
            return

        # **交给 plan_reply 的是这一次真正用的值，不是 conv 上读出来的旧值。**
        # 原来库里清成了 None，这一次却还拿着上一段热聊开始的时刻：
        # 隔了一夜他发的第一句，疲劳倍率顶满 ×8，还被提示"可以自然收尾了"。
        session = conv.hot_session_started_at
        if heat == "hot" and session is None:
            session = now
            await self.memory.update_conversation(CONVERSATION_ID, hot_session_started_at=now)
        elif heat == "cold":
            session = None
            await self.memory.update_conversation(CONVERSATION_ID, hot_session_started_at=None)

        decision = self.attention.plan_reply(
            now,
            heat,
            features,
            unread[-1].created_at,
            self.rng,
            session_started_at=session,
        )
        log.info(
            "[timing] heat=%s 看到 %s 回 %s　%s",
            heat,
            decision.notice_at.strftime("%m-%d %H:%M"),
            decision.reply_at.strftime("%m-%d %H:%M"),
            decision.reason,
        )
        await self.scheduler.schedule(
            "reply",
            decision.reply_at,
            conversation_id=CONVERSATION_ID,
            payload={
                "hints": decision.hints,
                "mode": features.mode,
                "is_question": features.is_question,
                # 回到他说话的那个地方，不是默认频道
                "channel_id": channel_id,
            },
            reason=decision.reason[:180],
        )

    async def _last_inbound_id(self) -> int | None:
        remembered = await self.memory.kv_get(LAST_INBOUND)
        return int(remembered) if (remembered or "").isdigit() else None

    async def _pull_reply_earlier(
        self,
        reply: Job,
        now: datetime,
        seconds: tuple[float, float],
        extra: dict[str, Any] | None = None,
        cap: datetime | None = None,
    ) -> datetime:
        """把排着的回复往前拉到现在附近。只往前拉，不往后推。

        **处境提示按新时刻重算，不在旧的后面追加。** 被拉回来的往往是排到了
        明早的那条，它带着"他这条是九个小时前发的，别说在睡觉""你其实早看到了"——
        而她马上就要回、他几分钟前刚说过话。
        """
        utc = lambda t: t.astimezone(UTC)  # noqa: E731
        run_at = later(now, timedelta(seconds=self.rng.uniform(*seconds)))
        if cap is not None:
            run_at = min(run_at, cap, key=utc)
        run_at = min(run_at, max(reply.run_at, now, key=utc), key=utc)
        payload = None
        if not reply.progress.get("plan"):
            unread = await self.memory.unread_messages(CONVERSATION_ID)
            if unread:
                said = unread[-1].created_at
            else:
                conv = await self.memory.get_conversation(CONVERSATION_ID)
                said = conv.last_user_message_at or now
            payload = {
                **reply.payload,
                "hints": self.attention.hints_at(run_at, now, said),
                **(extra or {}),
            }
        await self.scheduler.reschedule(reply.id or 0, run_at, payload)
        return run_at

    # -- 睡前那一句 ---------------------------------------------------------

    async def _signed_off_until(self, now: datetime) -> datetime | None:
        """她今晚已经说过要睡了的话，返回那晚的入睡时刻；过了就是 None。"""
        raw = await self.memory.kv_get(SIGNED_OFF_UNTIL)
        try:
            until = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            return None
        if until is None or now.astimezone(UTC) >= until.astimezone(UTC):
            return None
        return until

    async def _mark_signed_off(self, bedtime: datetime) -> None:
        """记下"今晚说过了"，排着的睡前那句作废：一晚只说一次。"""
        await self.memory.kv_set(SIGNED_OFF_UNTIL, bedtime.isoformat())
        for job in await self.memory.pending_jobs("sign_off", CONVERSATION_ID):
            await self.scheduler.cancel(job.id or 0)

    async def _reply_hints(self, job: Job, now: datetime) -> tuple[list[str], datetime | None]:
        """这次回复的处境提示，以及要不要顺便说睡了（要的话是那晚的入睡时刻）。

        睡前那句在**生成的那一刻**决定，不写死在任务里：回复被推迟过了睡点
        （额度用完、暂停、重试退避），醒来那条回复不该还带着"你准备睡了"。
        """
        hints = list(job.payload.get("hints", []))
        cfg = self.persona.proactive.sign_off
        goodnight: datetime | None = None
        if cfg.enabled and not self.rhythm.is_sleeping(now):
            # attention 那句"可以顺口说一句"是软的，模型说了、到点 sign_off 又说一遍；
            # 开着睡前那一句的时候换成这里的判断，说了就记下
            hints = [h for h in hints if h != SLEEP_SOON_HINT]
            bedtime = self.rhythm.next_sleep_after(now)
            left = bedtime.astimezone(UTC) - now.astimezone(UTC)
            asked = job.payload.get("sign_off_before")
            if asked and now.astimezone(UTC) < datetime.fromisoformat(asked).astimezone(UTC):
                bedtime, left = datetime.fromisoformat(asked), timedelta(0)
            if await self._signed_off_until(now) is not None:
                hints.append(cfg.said_note)
            elif left <= timedelta(minutes=cfg.reply_within_minutes):
                hints.append(cfg.reply_note)
                goodnight = bedtime
        riding = job.payload.get("riding_follow_up")
        if riding:
            rider = await self.memory.get_job(int(riding))
            if rider is not None and rider.status == "pending":
                hints.append(f"你之前答应过他的事，这次顺便说：{rider.payload.get('note', '')}")
        return hints, goodnight

    async def _download_images(self, message: discord.Message) -> list:
        """把他发的图片存下来，之后要真的给她看。"""
        from .models import Attachment

        out: list[Attachment] = []
        for att in message.attachments:
            item = Attachment(
                url=att.url,
                filename=att.filename,
                content_type=att.content_type,
                size=att.size,
            )
            if (att.content_type or "").startswith("image/") and att.size <= MAX_IMAGE_BYTES:
                # filename 是外部输入，可能带路径分隔符，只取最后一段
                safe = Path(att.filename).name or "image"
                target = self.settings.downloads_dir / f"{message.id}-{safe}"
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # **必须有超时。** aiohttp 默认等 300 秒，而这段代码
                    # 在补抓的循环里：一千条消息里有几张图卡住，补抓就停在那儿，
                    # 她一条都不会回，而日志里什么都看不出来。
                    # 下不下来就当没这张图——她照样回，只是看不见图。
                    async with asyncio.timeout(IMAGE_DOWNLOAD_TIMEOUT):
                        await att.save(target)
                    item.local_path = str(target)
                except Exception as exc:  # noqa: BLE001 - 下不下来就当没这张图
                    log.warning("[inbox] 图片没存下来：%s", exc)
            out.append(item)
        return out

    # -- 上下文 -------------------------------------------------------------

    async def _build_situation(self, now: datetime) -> str:
        day = self.rhythm.local_date(now)
        daily = self.rhythm.for_day(day)
        plan = await self.memory.get_day_plan(day)
        notes = [note for _, note in await self.memory.diary_notes(day)]

        mood = list(daily.mood_notes)
        if await owner_cmds.away_state(self.memory, day):
            # 备注**不写进来**。`!np away 出差 5` 说的是他出门，原来却成了
            # "你最近出差"——一个在读研究生被告知自己在出差，会在回复里说出来。
            # 写成"他在出差"也不行：那是他没跟她说过的事，她不该知道。
            mood.append("你这几天没什么心思聊天。")
        if heads_up := self.life.trip_heads_up(day):
            mood.append(heads_up)

        # 作息只知道有没有课，日程才知道她此刻具体在干什么。
        # 不接上的话会出现"你现在有空"和"19:00-22:00 在图书馆"同时摆在她面前。
        state_line = self.life.state_line(now)
        if event := self.life.current_event(plan, now):
            state_line = (
                state_line.removesuffix("你现在有空。") + f"你现在：{event.title}。{event.detail}"
            )
        elif recent := self.life.recent_event(plan, now):
            state_line += f"你刚忙完：{recent.title}。"

        return build_situation(
            persona=self.persona,
            now=self.rhythm.local_time(now),
            state_line=state_line,
            mood_notes=mood,
            day_plan=plan,
            diary_notes=notes,
        )

    async def _on_phone(self) -> None:
        """她真的在用手机：要打字发出去了。在线状态只跟着这个亮，**而且要先亮再打字**。"""
        if self.on_phone is not None:
            with contextlib.suppress(Exception):
                await self.on_phone()

    def _local_stamps(self, messages: list, now: datetime) -> list:
        """聊天记录的时间戳换成她此刻所在的时区，跟"现在是几点"对得上。"""
        tz = self.rhythm.local_time(now).tzinfo
        return [m.model_copy(update={"created_at": m.created_at.astimezone(tz)}) for m in messages]

    async def _photo_shortlist(self, now: datetime, tags: list[str] | None = None) -> list:
        """``tags`` 是这一种主动要的照片标签（window_photo 要窗外、夜景）。不给就不限。"""
        used = await self.memory.recently_used_photo_ids(now)
        # 时段按她人在的地方算：她在苏州的半夜，纽约是中午
        here = self.rhythm.local_time(now)
        found = self.media.library.available(used, time_of_day_at(here))
        return _tagged(found, tags)[:PHOTO_SHORTLIST]

    async def _recall(self, now: datetime) -> tuple[list[str], list[str]]:
        cfg = self.persona.memory
        owner_facts = await self.memory.recall_facts(
            "owner", now, cfg.fact_half_life_days, cfg.fact_recall_threshold
        )
        self_facts = await self.memory.recall_facts(
            "self", now, cfg.fact_half_life_days, cfg.fact_recall_threshold
        )
        return owner_facts, self_facts

    async def _resolve_photo(self, request: PhotoRequest | None, now: datetime):
        if request is None:
            return None
        used = await self.memory.recently_used_photo_ids(now)
        return await self.media.resolve(
            request, used, self.rng, time_of_day_at(self.rhythm.local_time(now))
        )

    # -- 回复 ---------------------------------------------------------------

    async def handle_reply_job(self, job: Job) -> None:
        if await owner_cmds.is_paused(self.memory):
            log.info("[job] 暂停中，回复先不发")
            await self.scheduler.defer(job.id or 0, later(self.clock.now(), timedelta(minutes=10)))
            return

        now = self.clock.now()
        from .models import ReplyPlan

        saved = job.progress.get("plan")
        if saved is not None:
            # 上次发到一半（崩了或者投递失败），接着发，不重新调模型
            start_index = int(job.progress.get("sent_parts", 0))
            covers = job.covers_upto_message_id
            try:
                reply_plan = ReplyPlan.model_validate(saved)
            except Exception:  # noqa: BLE001 - 存坏了就重新生成，别卡死在这
                log.warning("[job] 存下来的回复读不出来，重新想一遍")
                await self.memory.save_job_progress(job.id or 0, {}, covers)
                # 这批消息在存 progress 之前就标成已读了。不放回去的话
                # 下面取到的未读是空的，整批消息就再也不会有人回。
                if covers:
                    restored = await self.memory.restore_unread(CONVERSATION_ID, covers)
                    log.info("[job] 把 %d 条消息放回未读", restored)
                saved = None
            else:
                log.info("[job] 接着上次没发完的，从第 %d 条开始", start_index)
                # 只取**那一批**。reply_to_index 是按那一批的下标算的；原来取的是
                # "最近四十行里 id 不超过 covers 的全部"，早就回过的旧话也在里面，
                # 下标一错位，她就引用几个小时前的一句去回。
                unread = await self.memory.batch_messages(CONVERSATION_ID, covers)
                raw = job.progress.get("goodnight")
                await self._deliver_reply(
                    job, reply_plan, unread, now, start_index, covers,
                    goodnight=datetime.fromisoformat(raw) if raw else None,
                )
                return

        unread = await self.memory.unread_messages(CONVERSATION_ID)
        if not unread:
            return
        covers = max(m.id for m in unread)

        hints, goodnight = await self._reply_hints(job, now)
        reply_plan = await self._generate_reply(job, unread, now, hints)
        if reply_plan is None:
            # 模型没给出结果。**这里绝不能提前把消息标成已读**，
            # 否则重试的时候未读是空的，这批消息就永远回不出去了。
            if await self.brain.over_budget(self.rhythm.local_date(now)):
                # 今天的调用额度用完了。这不是故障，别拿三次重试把它烧掉，
                # 顺延到明天她醒来，表现出来就是今天话少。
                await self._defer_to_tomorrow(job, now, "今天的模型额度用完了")
                return
            raise RuntimeError("模型这次没给出回复")

        # 生成成功了才算她真的处理过这批消息
        await self.memory.mark_read([m.id for m in unread], now)
        start_index = 0
        progress: dict[str, Any] = {"plan": reply_plan.model_dump(mode="json"), "sent_parts": 0}
        if goodnight is not None:
            progress["goodnight"] = goodnight.isoformat()
        await self.memory.save_job_progress(job.id or 0, progress, covers)

        if not reply_plan.parts and not reply_plan.reaction:
            log.info("[brain] 这条她不打算回")
            await self._after_reply(reply_plan, now, sent_any=False, said_at=_said_at(unread))
            return

        await self._deliver_reply(
            job, reply_plan, unread, now, start_index, covers, goodnight=goodnight
        )

    async def _defer_to_tomorrow(self, job: Job, now: datetime, why: str) -> None:
        """把任务推到明天她醒来。用于不是故障、只是今天做不了的情况。"""
        wake = self.rhythm.next_wake_after(now)
        run_at = self.rhythm.first_glance_after_waking(wake, self.rng)
        log.warning("[job] %s，推到 %s", why, run_at.strftime("%m-%d %H:%M"))
        await self.scheduler.defer(job.id or 0, run_at)

    async def _generate_reply(self, job: Job, unread: list, now: datetime, hints: list[str]):
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        owner_facts, self_facts = await self._recall(now)
        recent = await self.memory.recent_messages(
            CONVERSATION_ID, self.persona.memory.recent_messages
        )
        recent = self._local_stamps([m for m in recent if m.id not in {u.id for u in unread}], now)

        mode_name = job.payload.get("mode")
        mode = next((m for m in self.persona.modes if m.name == mode_name), None)
        # 他问了问题、或者聊到交易这类他上心的事，这种不能不回
        must_reply = bool(job.payload.get("is_question") or mode_name)
        ledger = await self.memory.ledger(mode.ledger_kind) if mode and mode.include_ledger else []

        images = []
        for msg in unread:
            for att in msg.attachments:
                if att.local_path and Path(att.local_path).exists():
                    images.append(
                        (att.content_type or "image/png", Path(att.local_path).read_bytes())
                    )

        return await self.brain.generate_reply(
            ReplyRequest(
                situation=await self._build_situation(now),
                summary=conv.summary,
                owner_facts=owner_facts,
                self_facts=self_facts,
                ledger=ledger,
                ledger_topic=mode.ledger_topic if mode else "",
                # 跟话题模式无关，永远带着：他回"跑完了"的时候那句话里
                # 通常一个触发词都没有，模式匹配不上，她就没办法把这件事记成翻篇。
                open_questions=await self.memory.open_questions(now),
                not_yet=await self.memory.pending_timed(now),
                # 偶尔手滑打错一个字，发出去几秒后改回来。多久一次看人设。
                typo=self.rng.random() < self.persona.style.typo_probability,
                mode_instruction=mode.instruction if mode else "",
                recent=recent,
                unread=self._local_stamps(unread, now),
                hints=hints,
                photos=await self._photo_shortlist(now),
                images=images,
                must_reply=must_reply,
            ),
            self.rhythm.local_date(now),
        )

    async def _deliver_reply(
        self,
        job: Job,
        plan,
        unread: list,
        now: datetime,
        start_index: int,
        covers: int,
        goodnight: datetime | None = None,
    ) -> None:
        """``goodnight`` 不为空表示这次回复里顺便说了要睡，值是那晚的入睡时刻。"""
        channel_id = job.payload.get("channel_id")
        # 这一批的身份：一批一个 read_at（mark_read 一批一条 UPDATE）。
        #
        # **从库里按 covers 直接取**，不从 `unread` 里凑。续发那条路上
        # `unread` 是 `recent_messages(40)` 再按 id 过滤出来的，那一批只要被
        # 后来的消息挤出最近四十行，列表就是空的——她回两三个气泡的话，
        # 四十行只够十来个来回。那时候退回 `now` 写进去的是**重试那一刻**，
        # 对不上任何一批，于是这一批在体检里永远算"没接住"。
        #
        # 头一次发的时候这些消息刚被 `mark_read` 标上，库里读回来就是对的；
        # 万一还没写下（理论上不会），退回 `now`——那正是 mark_read 用的值。
        batch = await self.memory.batch_of(covers) or now
        channel = await self.resolve_channel(channel_id)
        photo = await self._resolve_photo(plan.photo_request, now)
        react_to = (
            await self._fetch_message(unread[-1].discord_message_id, channel) if unread else None
        )
        reply_to = None
        if plan.reply_to_index is not None and 0 <= plan.reply_to_index < len(unread):
            reply_to = await self._fetch_message(
                unread[plan.reply_to_index].discord_message_id, channel
            )

        async def interrupted() -> bool:
            return await self.memory.has_newer_user_message(CONVERSATION_ID, covers)

        await self._on_phone()

        async def on_progress(index: int) -> None:
            progress: dict[str, Any] = {"plan": plan.model_dump(mode="json"), "sent_parts": index + 1}
            if goodnight is not None:
                progress["goodnight"] = goodnight.isoformat()
            await self.memory.save_job_progress(job.id or 0, progress, covers)

        try:
            result = await self.deliverer.deliver_reply(
                channel,
                plan,
                photo,
                react_to=react_to,
                interrupted=interrupted,
                reply_to=reply_to,
                on_progress=on_progress,
                start_index=start_index,
            )
        except DeliveryBlocked as blocked:
            log.error("[delivery] 发不出去：%s", blocked.hint)
            await self.memory.update_conversation(CONVERSATION_ID, deliverable=False)
            await self._record_sent(blocked.result, now, batch)
            return
        except (Exception, asyncio.CancelledError) as exc:
            # 网络断在中间、或者停机取消落在两条之间时，前几条其实已经到对方手机上了。
            # 不记下来的话她自己的历史里就少一截，重试续发会重复或者前后矛盾。
            await self._record_partial(exc, now, batch)
            raise

        await self._record_sent(result, now, batch)
        if goodnight is not None and result.sent_texts and not result.interrupted:
            await self._mark_signed_off(goodnight)
        riding = job.payload.get("riding_follow_up")
        if riding and result.sent_texts and not result.interrupted:
            # 答应他的那件事跟着这次回复说出口了
            rider = await self.memory.get_job(int(riding))
            if rider is not None and rider.status == "pending":
                await self.scheduler.cancel(int(riding))
        if result.sent_texts or result.photo_sent:
            # 发得出去就把"发不出去"这个判断收回来。
            # 不收的话，一次 403（你临时退了共同服务器、关了私信）之后
            # 她就永远只回话、再也不主动了——而回复照常，你根本不会发现。
            await self._mark_deliverable(True)
        await self._after_reply(
            plan, now, sent_any=bool(result.sent_texts), said_at=_said_at(unread)
        )

        if result.interrupted:
            log.info("[delivery] 他又发了，剩下的不发了，重新排一次")
            await self._schedule_reply(self.clock.now(), channel_id=channel_id)
        elif await self.memory.unread_messages(CONVERSATION_ID):
            # 顺路的时候这里是空的：`mark_read` 已经把这一批清掉了。
            # 剩下的只有一种情形——他在"投递失败、等着重试"那个窗口里又说了话，
            # 被 `merge_pending` 并进了这条**已经带着旧 plan** 的任务。
            # 续发走的是老 plan，而发到最后一条时 `interrupted()` 根本不问，
            # 任务就此判 done：那句新话没人排回复，她会一直沉默到下次重连补抓。
            log.info("[delivery] 发完之后还有未读（重试窗口里进来的），补排一条")
            await self._schedule_reply(self.clock.now(), channel_id=channel_id)

    async def _fetch_message(self, message_id: int | None, channel: Any = None):
        """取回一条消息，用来加表情反应或者引用回复。

        取不到就算了：加不上反应不该让整条回复发不出去。
        """
        if not message_id or self.client is None:
            return None
        try:
            target = channel or await self.resolve_channel()
            return await target.fetch_message(message_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("[delivery] 取不到消息 %s：%r", message_id, exc)
            return None

    async def _record_partial(
        self, exc: BaseException, now: datetime, reply_batch: datetime | None = None
    ) -> None:
        """投递半途出事时，把已经发出去的那几条记下来。

        停机取消（CancelledError）也走这里：用 shield 护着写库，
        不然第二次取消会把这一步也打断，已经到他手机上的话就不在她的库里。
        """
        if partial := getattr(exc, "delivery_result", None):
            await asyncio.shield(self._record_sent(partial, now, reply_batch))

    async def _record_sent(
        self, result, now: datetime, reply_batch: datetime | None = None
    ) -> None:
        """把她真发出去的每一条记下来。

        ``reply_batch`` 是她在回的那一批未读的 ``read_at``；主动开口传 None。
        体检靠它分"回复"和"主动开口"——**不能靠时间去猜**。
        """
        for i, text in enumerate(result.sent_texts):
            await self.memory.add_bot_message(
                CONVERSATION_ID,
                text,
                now,
                discord_message_id=result.sent_message_ids[i]
                if i < len(result.sent_message_ids)
                else None,
                reply_batch=reply_batch,
            )
        if result.photo_sent and result.photo_sent.photo_id:
            await self.memory.mark_photo_used(
                result.photo_sent.photo_id, CONVERSATION_ID, now, result.photo_sent.is_fresh
            )

    async def _after_reply(
        self, plan, now: datetime, sent_any: bool, said_at: datetime | None = None
    ) -> None:
        """``said_at`` 是他这一批里最晚那句的时刻，"明早九点"就是相对它说的。"""
        day = self.rhythm.local_date(now)
        if plan.inner_note:
            await self.memory.add_diary_note(day, plan.inner_note, now)
        if plan.ledger_entries:
            anchor = said_at or now
            timings = [self.life.resolve_when_there(e.when_there, anchor) for e in plan.ledger_entries]
            await self.memory.add_ledger_entries(plan.ledger_entries, now, timings)
        if plan.resolved_ledger_ids:
            closed = await self.memory.resolve_ledger(plan.resolved_ledger_ids)
            if closed:
                log.info("[ledger] 他给了下文，%d 条翻篇了", closed)
        if plan.follow_up:
            await self.life.schedule_follow_up(
                CONVERSATION_ID, plan.follow_up.delay_minutes, plan.follow_up.note
            )
        if sent_any:
            await self.memory.update_conversation(CONVERSATION_ID, unanswered_initiations=0)
        await self._maybe_summarize()

    async def _maybe_summarize(self) -> None:
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        pending = await self.memory.count_messages_after(
            CONVERSATION_ID, conv.summary_upto_message_id
        )
        if pending < self.persona.memory.summarize_after:
            return
        if await self.memory.pending_jobs("memory_update", CONVERSATION_ID):
            return
        # **上一次整理失败之后要退避。** 这道闸原来只看 pending：
        # 重试用尽变成 failed 之后它就看不见了，而游标因为失败没推进、
        # 落后条数永远压着阈值——于是每回一条消息就新建一个任务、再烧三次调用。
        # 实测 45 轮来回烧掉 93 次模型调用（该是 48），撞上日限额之后
        # 她在当天中途毫无征兆地不说话了：频道里看不出任何原因。
        failed_at = await self.memory.last_failed_at("memory_update", CONVERSATION_ID)
        if failed_at is not None and self.clock.now() - failed_at < MEMORY_RETRY_BACKOFF:
            return
        await self.scheduler.schedule(
            "memory_update",
            later(self.clock.now(), timedelta(seconds=30)),
            conversation_id=CONVERSATION_ID,
            reason="对话攒够了，整理一下",
        )

    # -- 主动消息 -----------------------------------------------------------

    async def _mark_deliverable(self, ok: bool) -> None:
        """记下"现在发不发得出去"。只在状态真的变了时写库。"""
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        if conv.deliverable != ok:
            await self.memory.update_conversation(CONVERSATION_ID, deliverable=ok)
            log.info("[delivery] 发送通道恢复了" if ok else "[delivery] 发不出去了")

    async def _skip(self, job: Job, why: str) -> None:
        """这次什么都不说。**记成作废，不是做完。**

        体检按做完的任务数她主动开口的花样和"办事型"占比。什么都没发的任务
        记成做完的话，一个空照片库的 window_photo、三个撞上未读的 follow_up，
        就能把"她主动说的话里 75% 在办事"凭空印出来——而她实际上只说了一句闲话。
        """
        log.info("[proactive] %s", why)
        await self.scheduler.cancel(job.id or 0)

    async def _later(self, job: Job, now: datetime, minutes: tuple[float, float], why: str) -> None:
        """过一会儿再说。挪到她醒着的时候。"""
        run_at = self.life._awake_at_or_after(
            later(now, timedelta(minutes=self.rng.uniform(*minutes)))
        )
        log.info("[proactive] %s，%s 再说", why, run_at.strftime("%m-%d %H:%M"))
        await self.scheduler.defer(job.id or 0, run_at)

    async def handle_proactive_job(self, job: Job) -> None:
        now = self.clock.now()
        kind = job.payload.get("kind", "own_life")
        # 她自己答应过的事（"我查完告诉你"）不能因为时机不巧就没了：
        # 别的主动可有可无，这种丢了就是说话不算数。
        promised = kind == "follow_up"
        if await owner_cmds.is_paused(self.memory):
            if promised:
                await self._later(job, now, (30, 90), "暂停中")
                return
            await self._skip(job, "暂停中")
            return
        if self.rhythm.is_sleeping(now):
            if promised:
                await self._later(job, now, (0, 1), "这会儿在睡觉")
                return
            await self._skip(job, "这会儿在睡觉，算了")
            return
        if (bedtime := await self._signed_off_until(now)) is not None:
            # 她刚说完"我睡了"，接着又冒出一句，那句"我睡了"就是假的
            if promised:
                run_at = self.life._awake_at_or_after(later(bedtime, timedelta(minutes=1)))
                log.info("[proactive] 她已经说过要睡了，答应他的事 %s 再说",
                         run_at.strftime("%m-%d %H:%M"))
                await self.scheduler.defer(job.id or 0, run_at)
                return
            await self._skip(job, "她已经说过要睡了")
            return

        if promised and job.created_at:
            # 排的时候已经挡过一次，这里再挡一次：带时间的计划可能是**下一条回复**
            # 才记下的（他先说"明早九点做"，隔一句才说时间），排的时候还看不见。
            # **必须在"有未读就并进回复"之前**：并进去之后这道闸就走不到了，
            # 那句"跑完没"会跟着回复在他凌晨发出去。
            due = job.payload.get("due")
            hold = await self.life.follow_up_hold(
                now, job.created_at, datetime.fromisoformat(due) if due else job.created_at
            )
            if hold is not None:
                log.info(
                    "[proactive] 这个 follow_up 比他说的时间还早，推到 %s",
                    hold.strftime("%m-%d %H:%M"),
                )
                await self.scheduler.defer(job.id or 0, hold)
                return

        conv = await self.memory.get_conversation(CONVERSATION_ID)
        if not conv.deliverable:
            await self._skip(job, "发不出去")
            return

        # 有未读就不另起话头了，那是回复该做的事
        unread = await self.memory.unread_messages(CONVERSATION_ID)
        pending = await self.memory.pending_jobs("reply", CONVERSATION_ID)
        if unread or pending:
            if pending:
                reply = pending[0]
                riding = promised and not reply.progress.get("plan")
                # 并进这次回复：她正要回他，顺便把答应的事说了（那句提示在生成时才加）。
                # 这个 follow_up 先不作废，挪后一阵留着：回复真的说出口了
                # （_deliver_reply 发出了文字）才作废它。回复没发出来——
                # 他撤回了、模型觉得这句不用回——它到点照样说。
                await self._pull_reply_earlier(
                    reply, now, (20, 90), {"riding_follow_up": job.id} if riding else None
                )
                if riding:
                    await self._later(job, now, (60, 120), "有未读，答应他的事并进这次回复里说")
                    return
            if promised:
                await self._later(job, now, (15, 40), "有未读，答应他的事等这轮回完")
                return
            await self._skip(job, "有未读，本来想说的话并进回复里")
            return

        heat = heat_of(
            now,
            conv.last_user_message_at,
            conv.last_bot_message_at,
            self.persona.timing.hot_seconds,
            self.persona.timing.warm_seconds,
        )
        if heat == "hot":
            if promised:
                await self._later(job, now, (10, 30), "正聊着，答应他的事等这阵聊完")
                return
            await self._skip(job, "正聊着呢，不用另起话头")
            return

        day = self.rhythm.local_date(now)
        if not await self.life.can_initiate_today(CONVERSATION_ID, day):
            await self._skip(job, "今天已经主动过而且他没回，不追了")
            return
        if away := await owner_cmds.away_state(self.memory, day):
            if kind not in ("callback", "follow_up"):
                await self._skip(job, f"请假中（{away}），这类主动跳过")
                return

        photos = await self._photo_shortlist(now, job.payload.get("photo_tags"))
        if job.payload.get("requires_photo") and not photos:
            await self._skip(job, "想发照片但库里没有，跳过")
            return

        owner_facts, self_facts = await self._recall(now)
        last = max(
            [t for t in (conv.last_user_message_at, conv.last_bot_message_at) if t],
            default=None,
        )

        # 回访类的主动消息必须**带着他原话**去问。
        # 原来 trading_check 只有一句"问一句他之前说要做的事"，而 ProactiveRequest
        # 里根本没有台账——她被要求追问一件自己看不见的事，只能问得很空。
        # 到期条目在发送那一刻才取，不在排期时取：中间隔着几个小时，他可能已经做了。
        note = job.payload.get("note", "")
        ledger_ref: tuple[int, str, LedgerEntry] | None = None
        if job.payload.get("kind") == LEDGER_CHECK:
            ledger_ref = await self.life.due_ledger_entry(now)
            if ledger_ref is None:
                await self._skip(job, "本来要问一句，但已经没有到期的承诺了")
                return
            _entry_id, _kind, entry = ledger_ref
            note = f"{note}\n他当时说的是：{entry.claim}"
            if entry.reason:
                note += f"\n他给的理由：{entry.reason}"
            if entry.committed_to:
                note += f"\n他答应要做的：{entry.committed_to}"
            if entry.when_there:
                # 不带上的话，claim 里那句"明天早上"她会当成还没到。
                note += f"\n他说的时间：他那边 {entry.when_there}（已经过了）"

        # 这件事记下之后他说过话没有。说过的话，那条记下的"要问他的事"可能已经过时了——
        # 他也许早就答了。只在 follow_up 和回访这两种"问他事情"的主动上提醒她。
        he_spoke_since = False
        if job.payload.get("kind") in ("follow_up", LEDGER_CHECK) and job.created_at:
            last_his = conv.last_user_message_at
            he_spoke_since = bool(last_his and last_his > job.created_at)

        plan = await self.brain.generate_proactive(
            ProactiveRequest(
                situation=await self._build_situation(now),
                trigger_note=note,
                summary=conv.summary,
                owner_facts=owner_facts,
                self_facts=self_facts,
                recent=self._local_stamps(
                    await self.memory.recent_messages(CONVERSATION_ID, 20), now
                ),
                hours_since_last_exchange=(now - last).total_seconds() / 3600 if last else None,
                unanswered_initiations=conv.unanswered_initiations,
                photos=photos,
                he_spoke_since_noted=he_spoke_since,
                not_yet=await self.memory.pending_timed(now),
            ),
            day,
        )
        if plan is None:
            if await self.brain.over_budget(day):
                # 额度用完不是故障。原来这里照样 raise，三次空转之后判死，
                # 在体检和 !np status 里跟接口真坏了长得一样。
                if promised:
                    await self._defer_to_tomorrow(job, now, "今天的模型额度用完了")
                    return
                await self._skip(job, "今天的模型额度用完了，这句不说了")
                return
            raise RuntimeError("主动消息没生成出来")
        if not plan.send or (not plan.parts and not plan.photo_request):
            await self._skip(job, "她想了想，没什么要说的")
            return

        photo = await self._resolve_photo(plan.photo_request, now)
        if job.payload.get("requires_photo") and photo is None:
            await self._skip(job, "要的那张照片没取到")
            return

        await self._on_phone()
        try:
            result = await self.deliverer.deliver_proactive(
                await self.resolve_channel(), plan, photo
            )
        except DeliveryBlocked as blocked:
            log.error("[delivery] 发不出去：%s", blocked.hint)
            await self.memory.update_conversation(CONVERSATION_ID, deliverable=False)
            await self._record_sent(blocked.result, now)
            return
        except (Exception, asyncio.CancelledError) as exc:
            # 跟回复那条路一样：网络断在中间时前几条已经到他手机上了。
            # 不记下来的话她自己的历史里就少一截，而重试会拿到同一条台账条目
            # （asked_at 还没写），于是同一件事被问两遍，措辞还不一样——
            # 因为第一次说的话根本不在她的 recent 里。
            await self._record_partial(exc, now)
            raise

        await self._record_sent(result, now)
        # 回访必须真的发出了**文字**才算问过。只发一张没配字的照片
        # 不构成"问了一句"，却会把那条承诺沉到队尾，白白跳过一个周期。
        asked = bool(result.sent_texts) if ledger_ref is not None else True
        if result.sent_texts or result.photo_sent:
            if ledger_ref is not None and asked:
                await self.memory.mark_ledger_asked(ledger_ref[0], now)
            await self.life.mark_proactive_sent(kind, day, CONVERSATION_ID)
            await self.memory.add_diary_note(
                day, plan.inner_note or f"主动说了句（{kind}）", now
            )

    async def handle_sign_off_job(self, job: Job) -> None:
        """睡前说一声再走。

        到点时再看一眼：他还在跟她聊吗？在聊，她说一句"我睡了"；
        他刚说的话还没回，就回完顺便说；聊天早就停了，她直接睡。
        不算"主动开口"：不占当天的名额，他不回也不会让她以后更少开口。
        """
        now = self.clock.now()
        if await owner_cmds.is_paused(self.memory):
            await self._skip(job, "暂停中，睡前那句不说了")
            return
        bedtime_raw = job.payload.get("bedtime")
        bedtime = (
            datetime.fromisoformat(bedtime_raw) if bedtime_raw
            else self.rhythm.next_sleep_after(now)
        )
        if self.rhythm.is_sleeping(now) or now.astimezone(UTC) >= bedtime.astimezone(UTC):
            await self._skip(job, "已经睡了，睡前那句来不及了")
            return
        if await self._signed_off_until(now) is not None:
            await self._skip(job, "她刚才已经说过要睡了")
            return
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        if not conv.deliverable:
            await self._skip(job, "发不出去")
            return
        cfg = self.persona.proactive.sign_off
        # 只看**他**：她主动开口他没回，那不是在聊，睡前再补一句就成了追发
        his = conv.last_user_message_at
        if his is None or now - his > timedelta(minutes=cfg.active_within_minutes):
            await self._skip(job, "他早就不说话了，她直接睡")
            return

        pending = await self.memory.pending_jobs("reply", CONVERSATION_ID)
        if pending:
            reply = pending[0]
            if reply.progress.get("plan"):
                # 正在发的那条（续发、重试）。等它发完再看：
                # 它要是顺便说了，到时候这里会看到"已经说过了"
                await self.scheduler.defer(
                    job.id or 0, later(now, timedelta(minutes=self.rng.uniform(1, 3)))
                )
                return
            # 他刚说的还没回：睡前回完，顺便说一声。不然他那句要等到明天。
            await self._pull_reply_earlier(
                reply, now, (20, 60), {"sign_off_before": bedtime.isoformat()},
                cap=later(now, (bedtime.astimezone(UTC) - now.astimezone(UTC)) / 2),
            )
            await self._skip(job, "睡前把他那句回了，顺便说一声")
            return
        if await self.memory.unread_messages(CONVERSATION_ID):
            await self._skip(job, "有未读却没排回复，睡前那句先不说")
            return

        day = self.rhythm.local_date(now)
        owner_facts, self_facts = await self._recall(now)
        last = max([t for t in (his, conv.last_bot_message_at) if t])
        plan = await self.brain.generate_proactive(
            ProactiveRequest(
                situation=await self._build_situation(now),
                trigger_note=job.payload.get("note", ""),
                summary=conv.summary,
                owner_facts=owner_facts,
                self_facts=self_facts,
                recent=self._local_stamps(
                    await self.memory.recent_messages(CONVERSATION_ID, 20), now
                ),
                hours_since_last_exchange=(now - last).total_seconds() / 3600,
                unanswered_initiations=conv.unanswered_initiations,
                photos=[],
                # 睡前正是他刚说完"明早九点起来做完"的时候，道晚安时最容易顺口问一句
                not_yet=await self.memory.pending_timed(now),
            ),
            day,
        )
        if plan is None:
            if await self.brain.over_budget(day):
                await self._skip(job, "今天的模型额度用完了，睡前那句不说了")
                return
            raise RuntimeError("睡前那句没生成出来")
        if not plan.send or not plan.parts:
            await self._skip(job, "她刚才已经说过要睡了")
            return
        plan.photo_request = None
        # 这是收尾，不是开口：挂在他最后那一批上，体检不会把它算成主动找他。
        recent = await self.memory.recent_messages(CONVERSATION_ID, 10)
        last_his = [m for m in recent if m.author_kind == "user"]
        batch = await self.memory.batch_of(last_his[-1].id) if last_his else None
        await self._on_phone()
        try:
            # 回到他最后说话的地方。不带参数是主动消息的去处，配了公开频道的话
            # 私聊聊到一半的"我睡了"会当着别人的面说出去
            result = await self.deliverer.deliver_proactive(
                await self.resolve_channel(await self._last_inbound_id()), plan, None
            )
        except DeliveryBlocked as blocked:
            log.error("[delivery] 发不出去：%s", blocked.hint)
            await self.memory.update_conversation(CONVERSATION_ID, deliverable=False)
            await self._record_sent(blocked.result, now, batch)
            return
        except (Exception, asyncio.CancelledError) as exc:
            await self._record_partial(exc, now, batch)
            raise
        await self._record_sent(result, now, batch)
        if result.sent_texts:
            await self._mark_signed_off(bedtime)
            await self.memory.add_diary_note(day, plan.inner_note or "睡前跟他说了一声", now)

    # -- 记忆整理 -----------------------------------------------------------

    async def handle_memory_update_job(self, job: Job) -> None:
        now = self.clock.now()
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        # **从最老的一批开始。** 原来用的是 `recent_messages`，那取的是
        # **最新的** 200 条，而游标却推到了全库最大 id——等于宣称中间那些
        # 也整理过了。它们同时早就掉出"最近 40 条"的窗口，于是那一段
        # 从她的记忆里彻底消失，落后计数归零，以后再也没有哪一次整理
        # 会回头看它们。而这不会有任何症状：facts 有、摘要有、体检印 OK。
        #
        # 够得着这条路的场景很常规：她几天没能回话（长时间停机、私聊被挡、
        # 请假），重连时补抓一次最多灌进来一千条。
        messages = await self.memory.messages_after(
            CONVERSATION_ID, conv.summary_upto_message_id, 200
        )
        if not messages:
            return

        # "别重复"的名单只列她**现在还记得**的，最清楚的在前。
        # 原来列的是全量、按 id 从老到新截四十条：已经淡忘的还挂在"别重复"里，
        # 他再提一次模型也被要求别记，于是每件事满九十天必忘、之后也学不回来；
        # 最新记下的几条反而被截掉，换个说法又记一遍。
        # 现在淡忘的不在名单上，他再提起就会重新记下——记不清就问，问完记回来。
        owner_facts, self_facts = await self._recall(now)
        update = await self.brain.update_memory(
            MemoryUpdateRequest(
                previous_summary=conv.summary,
                messages=messages,
                existing_owner_facts=owner_facts,
                existing_self_facts=self_facts,
            ),
            self.rhythm.local_date(now),
        )
        if update is None:
            if await self.brain.over_budget(self.rhythm.local_date(now)):
                # 和回复那条路对齐：额度用完不是故障，别拿三次重试把它烧掉。
                # 原来一律 raise，于是额度一紧就三次全废，任务变 failed——
                # 而下面那道闸看不见 failed，每回一条消息就再排一个、再烧三次。
                await self._defer_to_tomorrow(job, now, "今天的模型额度用完了")
                return
            raise RuntimeError("记忆整理失败")

        await self.memory.update_conversation(
            CONVERSATION_ID,
            summary=update.summary,
            summary_upto_message_id=messages[-1].id,
        )
        await self.memory.add_facts("owner", update.owner_facts, now)
        await self.memory.add_facts("self", update.self_facts, now)
        log.info(
            "[memory] 摘要更新了，新记住 %d 条关于他的",
            len(update.owner_facts),
        )
        # 积压超过一趟（200 条）时接着排下一趟，别等他下次说话才想起来。
        await self._maybe_summarize()


class PresenceManager:
    """在线状态。

    **跟着行为走，不跟着时钟走。** 每天准点上线下线是最容易看出是程序的地方。
    睡觉时离线；醒着默认 idle（手机在口袋里）；只有真的在看手机的那几分钟才 online。
    自定义状态一天最多换一次，而且大多数时候不换。
    """

    def __init__(
        self,
        client: discord.Client,
        persona: Persona,
        rhythm: Rhythm,
        memory: Memory,
        clock: Clock,
        rng: random.Random,
    ) -> None:
        self.client = client
        self.persona = persona
        self.rhythm = rhythm
        self.memory = memory
        self.clock = clock
        self.rng = rng
        self._current: tuple[str, str | None] | None = None
        self._online_until: datetime | None = None

    async def go_online(self) -> None:
        """她拿起手机要发消息了：马上亮，别等下一轮循环。

        循环一分钟才跑一次，只改标志的话，她的气泡几乎总是先到、
        绿灯零到六十秒后才亮——又是一条反着来的固定规律。
        """
        self.note_activity()
        await self.apply_once()

    def note_activity(self, minutes: float | None = None) -> None:
        """她刚看了手机。接下来几分钟显示在线。"""
        span = minutes if minutes is not None else self.rng.uniform(1, 8)
        self._online_until = later(self.clock.now(), timedelta(minutes=span))

    async def apply_once(self) -> None:
        now = self.clock.now()
        snapshot = self.rhythm.state_at(now)

        if snapshot.state == "sleeping":
            status, text = "invisible", None
        elif self._online_until and now.astimezone(UTC) < self._online_until.astimezone(UTC):
            status, text = "online", await self._status_text(now)
        elif snapshot.state == "busy":
            status, text = self._busy_status(snapshot), await self._status_text(now)
        else:
            status, text = "idle", await self._status_text(now)

        if (status, text) == self._current:
            return
        self._current = (status, text)
        with contextlib.suppress(Exception):
            await self.client.change_presence(
                status=discord.Status(status),
                activity=discord.CustomActivity(name=text) if text else None,
            )

    def forget_last_applied(self) -> None:
        """忘掉"上次设的是什么"，下一轮循环会重新设一次。

        网关重连之后 Discord 那边的状态被重置成在线，而我们这边的缓存
        还记着 invisible，于是永远不去纠正。见 on_ready 里的说明。
        """
        self._current = None

    def _busy_status(self, snapshot: RhythmSnapshot) -> str:
        """在忙的时候是"勿扰"还是"闲置"——**同一段课里必须一直是同一个**。

        原来这里每次 apply_once 都重新掷一次骰子，而那个循环一分钟跑一次。
        她周二周四晚上 18:00-21:00 是一整块 busy，于是头像在勿扰和闲置之间
        平均每三分钟跳一次，一节课跳五十多回。没有人的客户端是那样的，
        而且这种高频 change_presence 正是会被 Discord 限流、进而断线重连的东西。

        改成按"这一段是什么时候开始的"抽签：整段课里结果不变，
        换一段课又是独立的一次抽签。
        """
        seed = f"{self.persona.seed}:busy:{snapshot.until.isoformat()}:{snapshot.block_title or ''}"
        roll = random.Random(seed).random()
        return "dnd" if roll < 0.2 else "idle"

    async def _status_text(self, now: datetime) -> str | None:
        """自定义状态一天最多换一次，而且多数时候不换。

        真人不会每两小时改一次签名，跟着日程自动轮换是明显的破绽。

        **按她醒着的那一天算，等当天日程出来了再定。** 原来按日历日、第一次调用就锁：
        第一次调用要么在午夜（她通常还醒着），要么在刚起床那一分钟，两个时刻
        当天的日程都还没生成，mood 是空的，当天就锁成"没有状态"——
        说好的三成日子有状态，实际一天都没有；偶尔有，也在 00:00 整准点消失。
        """
        day = self.rhythm.logical_day(now)
        if await self.memory.kv_get("status_text_day") == day.isoformat():
            return await self.memory.kv_get("status_text") or None
        plan = await self.memory.get_day_plan(day)
        if plan is None:
            return None  # 刚醒、日程还没出来：先空着，出来了再定
        await self.memory.kv_set("status_text_day", day.isoformat())
        # 按天抽签，重启不会重抽
        if random.Random(f"{self.persona.seed}:status:{day.isoformat()}").random() > 0.3:
            await self.memory.kv_set("status_text", "")
            return None
        text = plan.mood[:60]
        await self.memory.kv_set("status_text", text)
        return text or None

    async def run_forever(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.apply_once()
            await asyncio.sleep(60)


class NewPersonClient(discord.Client):
    def __init__(self, app: App) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.app = app
        self.presence: PresenceManager | None = None

    async def close(self) -> None:
        """收工。

        容器停机、机器重启都会走这里。数据库每次写都 commit，
        所以丢不了东西，但把连接干净地关掉能免掉一堆 WAL 残留。
        """
        log.info("[app] 收工，正在关掉手上的东西")
        self.app.scheduler.stop()
        with contextlib.suppress(Exception):
            await self.app.memory.close()
        await super().close()

    async def on_ready(self) -> None:
        """网关就绪。

        跟 ``App.start`` 一样，这里会被反复触发：Discord 隔一阵子就会让会话失效，
        RESUME 不上就重新 IDENTIFY，长跑的机器人一天可能好几次。
        所以在线状态那条循环也只能起一次——每次重连都新起一条的话，
        它们会一起写在线状态，撞上 Discord 的频率限制，
        而被限流又会导致断线重连，正反馈，越滚越糟。
        """
        log.info("[discord] 以 %s 的身份连上了", self.user)
        await self.app.start(self)
        if self.presence is not None:
            # **重连之后必须重发一次在线状态。**
            # 重新 IDENTIFY 时 discord.py 只在 ConnectionState 自带 status 的情况下
            # 把状态塞进 IDENTIFY 里，而 change_presence 从不回写那个字段——
            # 所以新会话默认是"在线"（绿灯）。而 apply_once 有个 _current 缓存，
            # 它记得自己上次设的是 invisible，于是判定"没变化"直接返回，
            # 永远不去纠正。结果就是她半夜三点亮着绿灯，一直到进程重启为止。
            # 清掉缓存，下一轮循环会重新设一次。
            self.presence.forget_last_applied()
            await self.app.catch_up()
            return
        self.presence = PresenceManager(
            self, self.app.persona, self.app.rhythm, self.app.memory, self.app.clock, self.app.rng
        )
        self.app.on_phone = self.presence.go_online
        self.app.spawn(self.presence.run_forever(), "presence")
        # 放在最后：补抓要用到已经建好的频道和数据库。
        # 每次重连都跑一遍，绝大多数时候什么都找不到，代价就是一次 history 调用。
        await self.app.catch_up()

    async def on_message(self, message: discord.Message) -> None:
        # 这里**不**标"在看手机"。原来他一发消息她就变绿：30 秒内亮灯、
        # 几分钟后变黄、几十分钟后在黄灯下回他，上课时也亮——一条百分之百的规律。
        # 在线只跟着她自己的动作走，见 App._on_phone。
        try:
            await self.app.on_user_message(message)
        except Exception:  # noqa: BLE001 - 一条消息处理失败不能把网关拖垮
            log.exception("[discord] 处理消息时出错")

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        content = (payload.data or {}).get("content")
        if content is not None:
            await self.app.memory.edit_message(payload.message_id, content, self.app.clock.now())

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.app.memory.delete_message(payload.message_id)


def build_app(
    settings: Settings,
    persona: Persona,
    *,
    llm_client: Any = None,
    seed: int | None = None,
) -> App:
    """把所有零件装起来。"""
    rng = random.Random(seed)
    clock = RealClock(persona.tz)
    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(
        persona.rhythm, persona.tz, persona.seed, calendar, force_awake=settings.force_awake
    )
    attention = AttentionPolicy(persona, rhythm, settings.delay_scale)
    memory = Memory(settings.db_path)
    scheduler = Scheduler(memory, clock, settings.delay_scale, rng)
    brain = Brain(llm_client or build_client(settings), settings, persona, memory, clock)

    library = PhotoLibrary(settings.photos_index)
    library.load()
    generator = (
        CommandImageGenerator(settings.image_gen_command)
        if settings.image_gen_command
        else NullImageGenerator()
    )
    media = MediaService(library, generator, settings.generated_dir)
    life = LifeEngine(persona, rhythm, calendar, memory, scheduler, brain, clock, rng)
    deliverer = Deliverer(clock, attention, rng, lambda path: discord.File(path))

    return App(
        settings=settings,
        persona=persona,
        clock=clock,
        rhythm=rhythm,
        calendar=calendar,
        attention=attention,
        memory=memory,
        scheduler=scheduler,
        brain=brain,
        media=media,
        life=life,
        deliverer=deliverer,
        rng=rng,
    )


def run(settings: Settings, persona: Persona) -> None:
    """启动机器人，直到被中断。

    容器里她是 1 号进程，`docker stop` 发的是 SIGTERM。
    不接这个信号的话进程会被直接砍掉，十秒后强杀，
    连关数据库的机会都没有。转成 KeyboardInterrupt 走正常的收工流程。
    """
    app = build_app(settings, persona)
    client = NewPersonClient(app)

    def _stop(signum: int, _frame: object) -> None:
        log.info("[app] 收到信号 %s，准备收工", signum)
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError):  # 非主线程时装不上，忽略
        signal.signal(signal.SIGTERM, _stop)

    client.run(settings.discord_bot_token, log_handler=None)
