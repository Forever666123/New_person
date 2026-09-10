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
import logging
import random
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from . import owner as owner_cmds
from .attention import AttentionPolicy, extract_features, heat_of
from .brain import Brain, MemoryUpdateRequest, ProactiveRequest, ReplyRequest, build_client
from .calendar import AcademicCalendar
from .clock import Clock, RealClock
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
CONVERSATION_ID = "owner"
PHOTO_SHORTLIST = 20
"""进上下文的照片最多这么多条，免得库大了把提示词撑爆。"""


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
        self._tasks: set[asyncio.Task] = set()
        """留着引用。只 create_task 不保存的话，任务可能被 GC 掉，循环无声无息就停了。"""

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
            await message.channel.send(reply)
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
        if not stored_id:
            return  # 网关重发，已经处理过了

        log.info(
            "[inbox] %d 字%s", len(message.content), " 带图" if attachments else ""
        )
        await self._schedule_reply(now, channel_id=message.channel.id)

    async def _schedule_reply(self, now: datetime, channel_id: int | None = None) -> None:
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        unread = await self.memory.unread_messages(CONVERSATION_ID)
        if not unread:
            return

        # 热度看的是这批消息**到来之前**对话有多热。
        # 用会话表上的 last_user_message_at 是错的：那个字段已经被刚收到的
        # 这条消息更新过了，间隔永远是 0，于是永远判成热聊，她就永远秒回。
        prior = await self.memory.last_exchange_before(CONVERSATION_ID, unread[0].id)
        heat = heat_of(
            unread[0].created_at,
            prior,
            None,
            self.persona.timing.hot_seconds,
            self.persona.timing.warm_seconds,
        )

        pending = await self.memory.pending_jobs("reply", CONVERSATION_ID)
        if pending:
            # 他还在连着发，就等他说完再一起回，但不会无限等下去
            job = pending[0]
            new_at = self.attention.merge_pending(job.run_at, now, heat, self.rng)
            await self.scheduler.reschedule(job.id or 0, new_at)
            log.info("[timing] 他还在打字，回复推到 %s", new_at.strftime("%H:%M"))
            return

        if heat == "hot" and conv.hot_session_started_at is None:
            await self.memory.update_conversation(CONVERSATION_ID, hot_session_started_at=now)
        elif heat == "cold":
            await self.memory.update_conversation(CONVERSATION_ID, hot_session_started_at=None)

        features = extract_features(
            [m.content for m in unread],
            self.persona,
            has_image=any(m.attachments for m in unread),
        )
        decision = self.attention.plan_reply(
            now,
            heat,
            features,
            unread[-1].created_at,
            self.rng,
            session_started_at=conv.hot_session_started_at,
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
        if away := await owner_cmds.away_state(self.memory, day):
            mood.append(f"你最近{away}，没什么心思聊天。")

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
            now=now,
            state_line=state_line,
            mood_notes=mood,
            day_plan=plan,
            diary_notes=notes,
        )

    async def _photo_shortlist(self, now: datetime) -> list:
        used = await self.memory.recently_used_photo_ids(now)
        return self.media.library.available(used, time_of_day_at(now))[:PHOTO_SHORTLIST]

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
        return await self.media.resolve(request, used, self.rng, time_of_day_at(now))

    # -- 回复 ---------------------------------------------------------------

    async def handle_reply_job(self, job: Job) -> None:
        if await owner_cmds.is_paused(self.memory):
            log.info("[job] 暂停中，回复先不发")
            await self.scheduler.reschedule(
                job.id or 0, self.clock.now() + timedelta(minutes=10)
            )
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
                unread = [
                    m
                    for m in await self.memory.recent_messages(CONVERSATION_ID, 40)
                    if m.author_kind == "user" and m.id <= covers
                ]
                await self._deliver_reply(job, reply_plan, unread, now, start_index, covers)
                return

        unread = await self.memory.unread_messages(CONVERSATION_ID)
        if not unread:
            return
        covers = max(m.id for m in unread)

        reply_plan = await self._generate_reply(job, unread, now)
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
        await self.memory.save_job_progress(
            job.id or 0, {"plan": reply_plan.model_dump(mode="json"), "sent_parts": 0}, covers
        )

        if not reply_plan.parts and not reply_plan.reaction:
            log.info("[brain] 这条她不打算回")
            await self._after_reply(reply_plan, now, sent_any=False)
            return

        await self._deliver_reply(job, reply_plan, unread, now, start_index, covers)

    async def _defer_to_tomorrow(self, job: Job, now: datetime, why: str) -> None:
        """把任务推到明天她醒来。用于不是故障、只是今天做不了的情况。"""
        wake = self.rhythm.next_wake_after(now)
        run_at = self.rhythm.first_glance_after_waking(wake, self.rng)
        log.warning("[job] %s，推到 %s", why, run_at.strftime("%m-%d %H:%M"))
        await self.scheduler.reschedule(job.id or 0, run_at)

    async def _generate_reply(self, job: Job, unread: list, now: datetime):
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        owner_facts, self_facts = await self._recall(now)
        recent = await self.memory.recent_messages(
            CONVERSATION_ID, self.persona.memory.recent_messages
        )
        recent = [m for m in recent if m.id not in {u.id for u in unread}]

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
                mode_instruction=mode.instruction if mode else "",
                recent=recent,
                unread=unread,
                hints=list(job.payload.get("hints", [])),
                photos=await self._photo_shortlist(now),
                images=images,
                must_reply=must_reply,
            ),
            self.rhythm.local_date(now),
        )

    async def _deliver_reply(
        self, job: Job, plan, unread: list, now: datetime, start_index: int, covers: int
    ) -> None:
        channel_id = job.payload.get("channel_id")
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

        async def on_progress(index: int) -> None:
            await self.memory.save_job_progress(
                job.id or 0,
                {"plan": plan.model_dump(mode="json"), "sent_parts": index + 1},
                covers,
            )

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
            await self._record_sent(blocked.result, now)
            return
        except Exception as exc:
            # 网络断在中间时，前几条其实已经到对方手机上了。不记下来的话
            # 她自己的历史里就少一截，重试续发会重复或者前后矛盾。
            if partial := getattr(exc, "delivery_result", None):
                await self._record_sent(partial, now)
            raise

        await self._record_sent(result, now)
        if result.sent_texts or result.photo_sent:
            # 发得出去就把"发不出去"这个判断收回来。
            # 不收的话，一次 403（你临时退了共同服务器、关了私信）之后
            # 她就永远只回话、再也不主动了——而回复照常，你根本不会发现。
            await self._mark_deliverable(True)
        await self._after_reply(plan, now, sent_any=bool(result.sent_texts))

        if result.interrupted:
            log.info("[delivery] 他又发了，剩下的不发了，重新排一次")
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

    async def _record_sent(self, result, now: datetime) -> None:
        for i, text in enumerate(result.sent_texts):
            await self.memory.add_bot_message(
                CONVERSATION_ID,
                text,
                now,
                discord_message_id=result.sent_message_ids[i]
                if i < len(result.sent_message_ids)
                else None,
            )
        if result.photo_sent and result.photo_sent.photo_id:
            await self.memory.mark_photo_used(
                result.photo_sent.photo_id, CONVERSATION_ID, now, result.photo_sent.is_fresh
            )

    async def _after_reply(self, plan, now: datetime, sent_any: bool) -> None:
        day = self.rhythm.local_date(now)
        if plan.inner_note:
            await self.memory.add_diary_note(day, plan.inner_note, now)
        if plan.ledger_entries:
            await self.memory.add_ledger_entries(plan.ledger_entries, now)
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
        await self.scheduler.schedule(
            "memory_update",
            self.clock.now() + timedelta(seconds=30),
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

    async def handle_proactive_job(self, job: Job) -> None:
        now = self.clock.now()
        if await owner_cmds.is_paused(self.memory):
            return
        if self.rhythm.is_sleeping(now):
            log.info("[proactive] 这会儿在睡觉，算了")
            return

        conv = await self.memory.get_conversation(CONVERSATION_ID)
        if not conv.deliverable:
            return

        # 有未读就不另起话头了，那是回复该做的事
        unread = await self.memory.unread_messages(CONVERSATION_ID)
        pending = await self.memory.pending_jobs("reply", CONVERSATION_ID)
        if unread or pending:
            log.info("[proactive] 有未读，本来想说的话并进回复里")
            if pending:
                await self.scheduler.reschedule(
                    pending[0].id or 0, now + timedelta(seconds=self.rng.uniform(20, 90))
                )
            return

        heat = heat_of(
            now,
            conv.last_user_message_at,
            conv.last_bot_message_at,
            self.persona.timing.hot_seconds,
            self.persona.timing.warm_seconds,
        )
        if heat == "hot":
            log.info("[proactive] 正聊着呢，不用另起话头")
            return

        day = self.rhythm.local_date(now)
        kind = job.payload.get("kind", "own_life")
        if not await self.life.can_initiate_today(CONVERSATION_ID, day):
            log.info("[proactive] 今天已经主动过而且他没回，不追了")
            return
        if away := await owner_cmds.away_state(self.memory, day):
            if kind not in ("callback", "follow_up"):
                log.info("[proactive] 请假中（%s），这类主动跳过", away)
                return

        photos = await self._photo_shortlist(now)
        if job.payload.get("requires_photo") and not photos:
            log.info("[proactive] 想发照片但库里没有，跳过")
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
                log.info("[proactive] 本来要问一句，但已经没有到期的承诺了")
                return
            _entry_id, _kind, entry = ledger_ref
            note = f"{note}\n他当时说的是：{entry.claim}"
            if entry.reason:
                note += f"\n他给的理由：{entry.reason}"
            if entry.committed_to:
                note += f"\n他答应要做的：{entry.committed_to}"

        plan = await self.brain.generate_proactive(
            ProactiveRequest(
                situation=await self._build_situation(now),
                trigger_note=note,
                summary=conv.summary,
                owner_facts=owner_facts,
                self_facts=self_facts,
                recent=await self.memory.recent_messages(CONVERSATION_ID, 20),
                hours_since_last_exchange=(now - last).total_seconds() / 3600 if last else None,
                unanswered_initiations=conv.unanswered_initiations,
                photos=photos,
            ),
            day,
        )
        if plan is None:
            raise RuntimeError("主动消息没生成出来")
        if not plan.send or (not plan.parts and not plan.photo_request):
            log.info("[proactive] 她想了想，没什么要说的")
            return

        photo = await self._resolve_photo(plan.photo_request, now)
        if job.payload.get("requires_photo") and photo is None:
            return

        try:
            result = await self.deliverer.deliver_proactive(
                await self.resolve_channel(), plan, photo
            )
        except DeliveryBlocked as blocked:
            log.error("[delivery] 发不出去：%s", blocked.hint)
            await self.memory.update_conversation(CONVERSATION_ID, deliverable=False)
            await self._record_sent(blocked.result, now)
            return
        except Exception as exc:
            # 跟回复那条路一样：网络断在中间时前几条已经到他手机上了。
            # 不记下来的话她自己的历史里就少一截，而重试会拿到同一条台账条目
            # （asked_at 还没写），于是同一件事被问两遍，措辞还不一样——
            # 因为第一次说的话根本不在她的 recent 里。
            if partial := getattr(exc, "delivery_result", None):
                await self._record_sent(partial, now)
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

    # -- 记忆整理 -----------------------------------------------------------

    async def handle_memory_update_job(self, job: Job) -> None:
        now = self.clock.now()
        conv = await self.memory.get_conversation(CONVERSATION_ID)
        messages = await self.memory.recent_messages(
            CONVERSATION_ID, 200, after_id=conv.summary_upto_message_id
        )
        if not messages:
            return

        owner_facts = [f for s, f in await self.memory.all_facts("owner")]
        self_facts = [f for s, f in await self.memory.all_facts("self")]
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

    def note_activity(self, minutes: float | None = None) -> None:
        """她刚看了手机。接下来几分钟显示在线。"""
        span = minutes if minutes is not None else self.rng.uniform(1, 8)
        self._online_until = self.clock.now() + timedelta(minutes=span)

    async def apply_once(self) -> None:
        now = self.clock.now()
        snapshot = self.rhythm.state_at(now)

        if snapshot.state == "sleeping":
            status, text = "invisible", None
        elif self._online_until and now < self._online_until:
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
        """
        day = self.rhythm.local_date(now).isoformat()
        if await self.memory.kv_get("status_text_day") == day:
            return await self.memory.kv_get("status_text") or None
        await self.memory.kv_set("status_text_day", day)
        if self.rng.random() > 0.3:
            await self.memory.kv_set("status_text", "")
            return None
        plan = await self.memory.get_day_plan(self.rhythm.local_date(now))
        text = (plan.mood if plan else "")[:60]
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
            return
        self.presence = PresenceManager(
            self, self.app.persona, self.app.rhythm, self.app.memory, self.app.clock, self.app.rng
        )
        self.app.spawn(self.presence.run_forever(), "presence")

    async def on_message(self, message: discord.Message) -> None:
        try:
            if self.presence and message.author.id in self.app.settings.all_allowed_user_ids:
                self.presence.note_activity()
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
    brain = Brain(llm_client or build_client(settings), settings, persona, memory)

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
