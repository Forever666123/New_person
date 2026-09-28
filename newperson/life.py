"""生活引擎：她今天过了什么日子，什么时候会主动开口。

两件事：

1. **每天的日程**。醒来时生成一次，存进日记。之后回消息、主动说话都从这里取材，
   这样"我刚下课"和"我在图书馆"不会自相矛盾。
2. **主动消息的候选时刻**。候选类型写在人设里，不写死在代码里。
   她主动的方式不是问候：是说一句自己的事、提一句他几天前说过的话、
   问他之前说要做的交易做了没有。**没有"最近怎么样"这一类。**

主动消息的分寸靠三条：主动开的话头对方没回，下次概率就衰减；
每天最多一次没被回应的开场；正在聊的时候不会突然另起话头，那是回复该做的事。
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from .brain import Brain, DayPlanRequest
from .calendar import AcademicCalendar
from .clock import Clock
from .memory import Memory
from .models import DayPlan, Job, LedgerEntry, LedgerTiming, PlanEvent
from .persona import OpenerConfig, Persona, ProactiveKind, parse_hhmm
from .rhythm import Rhythm
from .scheduler import Scheduler

LEDGER_CHECK = "ledger_check"
"""回访台账的那种主动消息的名字。代码里要认它，所以不能只写在 yaml 里。"""

_WHEN_THERE = re.compile(r"\s*(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*")
"""模型写的"他那边的时间"：MM-DD HH:MM。别的写法一律不认，退回按周期问。"""

PROACTIVE_PENDING = "proactive_pending:"
"""这一天的日程是外面存进来的（`plan --save`），主动时刻还没排。"""

OPENER_KEY = "opener"
"""排过开场没有。同时用作任务的去重键，所以它天然只会排上一次。"""

log = logging.getLogger(__name__)


class LifeEngine:
    def __init__(
        self,
        persona: Persona,
        rhythm: Rhythm,
        calendar: AcademicCalendar,
        memory: Memory,
        scheduler: Scheduler,
        brain: Brain,
        clock: Clock,
        rng: random.Random,
    ) -> None:
        self.persona = persona
        self.rhythm = rhythm
        self.calendar = calendar
        self.memory = memory
        self.scheduler = scheduler
        self.brain = brain
        self.clock = clock
        self.rng = rng
        self.has_photos: Callable[[], bool] = lambda: True
        """手边有没有能发的照片。App 接上照片库；没接的时候当作有。"""

    # -- 日程 ---------------------------------------------------------------

    def state_line(self, now: datetime) -> str:
        """一句话说清她此刻在哪、在干嘛。回复和日程生成都用它。"""
        snapshot = self.rhythm.state_at(now)
        where = f"你人在{snapshot.trip_place}。" if snapshot.trip_place else ""
        period = f"现在是{snapshot.period}。" if snapshot.period else ""
        state = {
            "sleeping": "你在睡觉。",
            "busy": f"你在{snapshot.block_title or '忙'}。",
            "winding_down": "你准备睡了。",
            "free": "你现在有空。",
        }[snapshot.state]
        return f"{period}{where}{state}"

    async def ensure_today_plan(self, conversation_id: str) -> DayPlan | None:
        """今天还没有日程就生成一个，顺便把主动消息的候选排上。

        用数据库抢占，所以启动检查和起床任务同时触发也只会生成一次。
        """
        now = self.clock.now()
        day = self.rhythm.local_date(now)

        existing = await self.memory.get_day_plan(day)
        if existing is not None:
            if await self.memory.kv_get(f"{PROACTIVE_PENDING}{day}"):
                await self.memory.kv_delete(f"{PROACTIVE_PENDING}{day}")
                count = await self.schedule_proactive_candidates(existing, conversation_id)
                log.info("[life] %s 的日程是手动存的，补排了 %d 个主动时刻", day, count)
            return existing
        if not await self.memory.claim_day_plan(day, now):
            return None  # 别人正在生成

        daily = self.rhythm.for_day(day)
        yesterday = await self.memory.get_day_plan(day - timedelta(days=1))
        conv = await self.memory.get_conversation(conversation_id)

        plan = await self.brain.generate_day_plan(
            DayPlanRequest(
                now=self.rhythm.local_time(now),
                state_line=self.state_line(now),
                mood_notes=daily.mood_notes,
                wake_at=daily.wake,
                sleep_at=daily.sleep_start,
                classes=[
                    f"{c.title} {c.start.strftime('%H:%M')}-{c.end.strftime('%H:%M')}"
                    for c in daily.classes
                ],
                yesterday=yesterday,
                summary=conv.summary,
            ),
            day,
        )
        if plan is None:
            # 生成失败：把抢占放掉，下次再试，不要一整天都没有日程
            await self.memory.release_day_plan(day)
            log.warning("[life] %s 的日程没生成出来，稍后再试", day)
            return None

        await self.memory.save_day_plan(day, plan)
        count = await self.schedule_proactive_candidates(plan, conversation_id)
        log.info("[life] %s 的日程好了，排了 %d 个主动时刻", day, count)
        return plan

    async def ensure_opener(self, conversation_id: str, plan: DayPlan | None) -> datetime | None:
        """第一次上线时排一句开场。一辈子一次。

        不做的话，第一天是你发消息进去，然后按她的作息可能等三小时才有回音——
        功能完全正常，但看起来像坏了。这一句是"她在"的证据。

        它借用普通的主动消息通道，用的也是现成的那种口吻，
        所以内容上跟她第一百天说的话没有区别——**看不出是开场的开场**才是对的开场。
        真到了要发的时候，``handle_proactive_job`` 的那些守卫照样生效：
        你要是抢先说话了，这句会并进回复里，而不是自说自话。
        """
        cfg = self.persona.proactive.opener
        if not cfg.enabled or await self.memory.kv_get(OPENER_KEY):
            return None

        # 已经聊过就不是第一次。老库升级上来不该突然冒出一句开场白。
        if await self.memory.recent_messages(conversation_id, limit=1):
            await self.memory.kv_set(OPENER_KEY, "skipped:已经聊过了")
            log.info("[life] 不是第一次上线，开场跳过")
            return None

        moment = self._opener_moment(self.clock.now(), cfg)
        if moment is None:
            log.warning("[life] 找不到合适的开场时刻，跳过")
            return None

        shareable = [e for e in plan.events if e.shareable] if plan else []
        job_id = await self.scheduler.schedule(
            "proactive",
            moment,
            conversation_id=conversation_id,
            payload={"kind": cfg.kind, "note": self._with_plan_hint(cfg.note, cfg.kind, shareable)},
            dedupe_key=OPENER_KEY,
            reason="opener",
        )
        if not job_id:
            return None
        # 排上了就记账。任务本身已经落库，进程崩了它还在；
        # 这里记的是"这辈子排过了"，重启不该再排一次。
        await self.memory.kv_set(OPENER_KEY, moment.isoformat())
        log.info("[life] 第一次上线，开场排在 %s", moment.strftime("%m-%d %H:%M"))
        return moment

    def _opener_moment(self, now: datetime, cfg: OpenerConfig) -> datetime | None:
        """挑一个像人的时刻。

        两条底线：不能是启动后一分钟（那是程序开机的样子），
        也不能是她正睡着的时候。
        """
        earliest = now + timedelta(minutes=cfg.min_delay_minutes)
        latest = now + timedelta(hours=cfg.max_delay_hours)
        span = max((latest - earliest).total_seconds(), 1.0)

        # 先在窗口里按活跃度抽：她越可能在看手机的时刻，越容易被抽中。
        for _ in range(80):
            moment = earliest + timedelta(seconds=self.rng.uniform(0, span))
            if self.rhythm.is_sleeping(moment):
                continue
            if self.rng.random() <= self.rhythm.engage_probability_at(moment):
                return moment

        # 窗口里没抽到——要么她整段都在睡（半夜装机器就是这样），
        # 要么活跃度那关一直没过。往后找一个她醒着的时刻，
        # 再往后挪一段，别正好卡在起床那一分钟。
        #
        # **加完偏移之后必须再查一次是不是在睡。** 起床前几分钟是"醒着"的，
        # 但 probe + 40~180 分钟完全可能又落回下一段睡眠里去；
        # 而开场只有一次机会：真到了那一刻她在睡，handle_proactive_job 直接返回，
        # 任务标记成 done，kv 里的标记和全局唯一的 dedupe_key 让它再也排不上了。
        # 这一句不发，就是永远不发。
        probe = earliest
        limit = now + timedelta(hours=cfg.fallback_search_hours)
        step = timedelta(minutes=15)
        low, high = cfg.after_waking_minutes
        while probe < limit:
            if not self.rhythm.is_sleeping(probe):
                candidate = probe + timedelta(minutes=self.rng.uniform(low, high))
                if candidate < limit and not self.rhythm.is_sleeping(candidate):
                    return candidate
            probe += step
        return None

    async def schedule_next_day_plan(self) -> int:
        """在下一次起床时安排生成明天的日程。去重键保证只有一个。"""
        now = self.clock.now()
        wake = self.rhythm.next_wake_after(now)
        day = self.rhythm.local_date(wake)
        return await self.scheduler.schedule(
            "day_plan",
            wake + timedelta(minutes=self.rng.uniform(0, 20)),
            dedupe_key=f"day_plan:{day}",
            reason=f"{day} 醒来",
        )

    # -- 日程里的事件 -------------------------------------------------------

    def _event_window(self, plan_day: date, event: PlanEvent) -> tuple[datetime, datetime] | None:
        try:
            sh, sm = parse_hhmm(event.start)
            eh, em = parse_hhmm(event.end)
        except ValueError:
            log.warning("[life] 日程里的时间格式不对：%s-%s", event.start, event.end)
            return None
        tz = self.rhythm.tz_for(plan_day)
        start = datetime.combine(plan_day, time(sh, sm), tzinfo=tz)
        end = datetime.combine(plan_day, time(eh, em), tzinfo=tz)
        if end <= start:  # 跨午夜
            end += timedelta(days=1)
        return start, end

    def current_event(self, plan: DayPlan | None, now: datetime) -> PlanEvent | None:
        if plan is None:
            return None
        day = self.rhythm.local_date(now)
        for event in plan.events:
            window = self._event_window(day, event)
            if window and window[0] <= now < window[1]:
                return event
        return None

    def recent_event(self, plan: DayPlan | None, now: datetime) -> PlanEvent | None:
        """当前的事，或者刚结束不久的事。用来说"我刚……"。"""
        current = self.current_event(plan, now)
        if current is not None:
            return current
        if plan is None:
            return None
        day = self.rhythm.local_date(now)
        best: tuple[datetime, PlanEvent] | None = None
        for event in plan.events:
            window = self._event_window(day, event)
            if window and window[1] <= now < window[1] + timedelta(hours=2):
                if best is None or window[1] > best[0]:
                    best = (window[1], event)
        return best[1] if best else None

    # -- 主动消息 -----------------------------------------------------------

    def _hours_allowed(self, kind: ProactiveKind, moment: datetime) -> bool:
        """有些主动只在特定时段才成立，比如凌晨拍窗外。"""
        if not kind.hours:
            return True
        minute = moment.hour * 60 + moment.minute
        for span in kind.hours:
            try:
                start_s, end_s = span.split("-")
                sh, sm = parse_hhmm(start_s)
                eh, em = parse_hhmm(end_s)
            except ValueError:
                log.warning("[life] 主动消息的时段写错了：%s", span)
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            if start <= end:
                if start <= minute <= end:
                    return True
            elif minute >= start or minute <= end:  # 跨午夜
                return True
        return False

    async def _days_since_last(self, kind_name: str, today: date) -> float:
        raw = await self.memory.kv_get(f"last_proactive:{kind_name}")
        if not raw:
            return 999.0
        try:
            return (today - date.fromisoformat(raw)).days
        except ValueError:
            return 999.0

    def _awake_window(self, day: date) -> tuple[datetime, datetime]:
        daily = self.rhythm.for_day(day)
        return daily.wake, daily.sleep_start

    async def candidate_moments(
        self, plan: DayPlan, day: date, conversation_id: str
    ) -> list[tuple[datetime, ProactiveKind, str]]:
        """今天她可能主动开口的时刻。

        概率会因为"上次主动他没回"而衰减。追着说话是这类东西最容易崩掉的地方。
        """
        cfg = self.persona.proactive
        if not cfg.kinds:
            return []

        conv = await self.memory.get_conversation(conversation_id)
        decay = cfg.unanswered_decay**conv.unanswered_initiations
        try:
            chattiness = float(await self.memory.kv_get("chattiness") or 1.0)
        except ValueError:
            chattiness = 1.0

        # 先问"今天她到底会不会开口"。少了这一步，每种主动各自掷骰子，
        # 合起来就变成几乎每天都要找你说话，那很黏人。
        threshold = cfg.day_probability * decay * chattiness
        if self.rng.random() > threshold:
            log.info(
                '[life] 今天不打算主动开口。命中概率 %.0f%%（基准 %.0f%%，'
                '他没回过 %d 次，chattiness %.1f）',
                threshold * 100,
                cfg.day_probability * 100,
                conv.unanswered_initiations,
                chattiness,
            )
            return []

        now = self.clock.now()
        wake, sleep = self._awake_window(day)
        travelling = self.calendar.trip_for(day) is not None

        shareable = [e for e in plan.events if e.shareable]

        eligible: list[ProactiveKind] = []
        photos = self.has_photos()
        for kind in cfg.kinds:
            if kind.only_while_travelling and not travelling:
                continue
            if kind.requires_photo and not photos:
                continue
            if await self._days_since_last(kind.name, day) < kind.min_days_since_last:
                continue
            if kind.name == LEDGER_CHECK and await self.due_ledger_entry(now) is None:
                # 没有到期的承诺就别安排"问一句做了没有"——
                # 她会为了填这个坑去编一件他没说过的事。
                continue
            eligible.append(kind)
        if not eligible:
            return []

        # 开了口不代表要说一整天。多数日子只说一件事。
        keep = 1
        while keep < cfg.max_per_day and self.rng.random() < cfg.second_message_probability:
            keep += 1

        candidates: list[tuple[datetime, ProactiveKind, str]] = []
        pool = list(eligible)
        for _ in range(min(keep, len(pool))):
            kind = self._weighted_pick(pool)
            pool.remove(kind)

            moment = self._sample_moment(kind, wake, sleep, day)
            if moment is None or moment <= now or self.rhythm.is_sleeping(moment):
                continue

            candidates.append((moment, kind, self._with_plan_hint(kind.note, kind.name, shareable)))

        return sorted(candidates, key=lambda c: c[0])

    async def due_ledger_entry(self, now: datetime) -> tuple[int, str, LedgerEntry] | None:
        """所有台账类别里，此刻最该被追问的那一条。

        每一类有自己的周期（``modes`` 里的 ``follow_up_after_days``），
        谁先到期先问谁。**一次只问一件**：类别多了之后，如果每类各自抽签，
        她会变成一份待办清单——今天问学习、明天问排班、后天问作息。
        """
        best: tuple[int, str, LedgerEntry] | None = None
        best_at: datetime | None = None
        for mode in self.persona.modes:
            if not mode.ledger_kind or mode.follow_up_after_days <= 0:
                continue
            cfg = self.persona.proactive
            found = await self.memory.due_ledger_entry(
                mode.ledger_kind,
                now,
                mode.follow_up_after_days,
                max_follow_ups=cfg.ledger_max_follow_ups,
                max_age_days=cfg.ledger_max_age_days,
            )
            if found is None:
                continue
            entry_id, touched_at, entry = found
            # 最久没被碰过的那条先问。问过一条它就排到队尾，
            # 于是几个类别自然轮着来，而不是某一类一直压着别的。
            if best_at is None or touched_at < best_at:
                best_at, best = touched_at, (entry_id, mode.ledger_kind, entry)
        return best

    def _with_plan_hint(self, note: str, kind_name: str, shareable: list[PlanEvent]) -> str:
        """给"说说自己"这类主动挂一件今天真发生的事。

        少了这一步她只能泛泛地说，而泛泛正是最像机器人的地方。
        """
        if not shareable or kind_name not in ("own_life", "travel_note"):
            return note
        event = self.rng.choice(shareable)
        note = f"{note}\n今天可以提的是：{event.title}。{event.detail}"
        if event.share_hint:
            note += f"（{event.share_hint}）"
        return note

    def _weighted_pick(self, kinds: list[ProactiveKind]) -> ProactiveKind:
        """按权重抽一种。权重决定她更常用哪种方式开口。"""
        total = sum(max(k.weight, 0.0) for k in kinds)
        if total <= 0:
            return self.rng.choice(kinds)
        roll = self.rng.uniform(0, total)
        acc = 0.0
        for kind in kinds:
            acc += max(kind.weight, 0.0)
            if roll <= acc:
                return kind
        return kinds[-1]

    def _sample_moment(
        self, kind: ProactiveKind, wake: datetime, sleep: datetime, day: date
    ) -> datetime | None:
        """在她醒着的时间里挑一刻，**按活跃度加权**，受 kind 的时段限制。

        原来这里是在整个清醒时段里均匀抽。均匀抽的后果是她会在周二晚上
        七点半——也就是她自己那三个小时的课正上到一半的时候——忽然说一句
        "今天雪好大"。而那一刻她的 Discord 状态明明写着在上课。
        状态和行为对不上，是最直白的一种露馅。

        加权之后，她越可能在看手机的时刻越容易被抽中，跟真人一样：
        课间、走路回去的路上、睡前躺着的时候。

        抽不中就退回原来的均匀抽法——像 window_photo 那种把时段写死在
        凌晨的类型，作者已经指定了什么时候发，不该被活跃度否掉。
        """
        if sleep <= wake:
            return None
        span = (sleep - wake).total_seconds()
        for _ in range(40):
            moment = wake + timedelta(seconds=self.rng.uniform(0, span))
            if not self._hours_allowed(kind, moment):
                continue
            if self.rng.random() <= self.rhythm.engage_probability_at(moment):
                return moment
        for _ in range(30):
            moment = wake + timedelta(seconds=self.rng.uniform(0, span))
            if self._hours_allowed(kind, moment):
                return moment
        return None

    async def schedule_proactive_candidates(self, plan: DayPlan, conversation_id: str) -> int:
        count = 0
        day = self.rhythm.local_date(self.clock.now())
        for moment, kind, note in await self.candidate_moments(plan, day, conversation_id):
            job_id = await self.scheduler.schedule(
                "proactive",
                moment,
                conversation_id=conversation_id,
                payload={
                    "kind": kind.name,
                    "note": note,
                    "photo_tags": kind.photo_tags,
                    "requires_photo": kind.requires_photo,
                    "text_optional": kind.text_optional,
                },
                dedupe_key=f"proactive:{day}:{kind.name}",
                reason=kind.name,
            )
            if job_id:
                count += 1
        return count

    def resolve_when_there(self, raw: str, said_at: datetime) -> LedgerTiming | None:
        """把模型写的"他那边 MM-DD HH:MM"换算成真实时刻，再抽一段宽限。

        **分工是模型读钟、代码做算术。** 这次的 bug 就出在让模型心算
        "还有多少分钟"：它要同时换算她的钟、他的钟和"明天早上"，普遍算短，
        于是他凌晨一两点说"明早九点做"，她四五点就来问。

        锚点是**他说这句话的时候**，不是她回复的时候：她睡了一夜才回，
        "09:00"对回复时刻已经过去，对他开口那一刻仍是将来。

        换算不出来（写法不对、没配他的时区、在他开口之前、远得离谱）就返回 None，
        这条退回按类别周期问——一个坏值卡不住整条台账。
        """
        tz = self.persona.owner_tz
        if not raw or not raw.strip() or tz is None:
            return None
        cfg = self.persona.proactive.ledger_timed
        match = _WHEN_THERE.fullmatch(raw)
        if match is None:
            log.warning("[ledger] 他说的时间写法认不出（%s），按周期问", raw[:40])
            return None
        month, day, hour, minute = (int(g) for g in match.groups())
        said = said_at.astimezone(UTC)
        said_there = said_at.astimezone(tz)
        best: datetime | None = None
        # 跨年："12-31 说 01-01"是明年的一月一号。取离他开口最近的那一年。
        for year in (said_there.year - 1, said_there.year, said_there.year + 1):
            try:
                wall = datetime(year, month, day, hour, minute, tzinfo=tz)
            except ValueError:
                continue
            # 夏令时跳过或重复的那一小时，两种理解取**更晚**的：写晚了只是问得晚。
            # 比较一律换到 UTC——同一个 tzinfo 的两个时刻相减时 Python 按墙钟算，
            # 会把重复的那一小时算丢。
            moment = max(
                wall.replace(fold=0).astimezone(UTC), wall.replace(fold=1).astimezone(UTC)
            )
            if best is None or abs(moment - said) < abs(best - said):
                best = moment
        if best is None:
            log.warning("[ledger] 他说的时间不存在（%s），按周期问", raw[:40])
            return None
        if best <= said:
            # 在他开口之前——多半是进展（"我九点就在跑了"），不是计划。
            return None
        if best - said > timedelta(days=cfg.max_days_ahead):
            log.warning("[ledger] 他说的时间远得离谱（%s），按周期问", raw[:40])
            return None
        lo, hi = cfg.grace_hours
        grace = timedelta(hours=self.rng.uniform(lo, max(lo, hi)))
        return LedgerTiming(
            when_there=best.astimezone(tz).strftime("%m-%d %H:%M"),
            due_at=best,
            ask_after=best + grace,
        )

    def _awake_at_or_after(self, moment: datetime) -> datetime:
        """这一刻她要是在睡，就挪到她醒来之后第一次看手机。"""
        if self.rhythm.is_sleeping(moment):
            return self.rhythm.first_glance_after_waking(
                self.rhythm.next_wake_after(moment), self.rng
            )
        return moment

    async def follow_up_hold(self, now: datetime, noted_at: datetime) -> datetime | None:
        """这个 follow_up 最早能在什么时候发。不用等就返回 None。

        ``noted_at`` 前后 ``follow_up_guard_hours`` 里记下的、他说了时间的计划，
        过了宽限之前她都不发 follow_up——分不清它是不是就在问那件事。
        窗口两头都要有边：只有下限的话，被推迟过的 follow_up 会被几天后
        一件不相干的新计划再拖住。
        """
        guard = timedelta(hours=self.persona.proactive.ledger_timed.follow_up_guard_hours)
        hold = await self.memory.latest_timed_ask_after(
            now, since=noted_at - guard, until=noted_at + guard
        )
        if hold is None or hold <= now:
            return None
        # 库里存的是 UTC。排任务之前换回她的钟，跟队列里别的任务写法一致。
        return self._awake_at_or_after(hold.astimezone(now.tzinfo))

    async def schedule_follow_up(self, conversation_id: str, delay_minutes: int, note: str) -> int:
        """她说了"我查完告诉你"，就真的要记得回来说。

        **同一时间最多排一个。** 分钟数是模型自己心算的，而它会把他的计划
        （"明天早上我要做 X"）也当成自己的待办，一条回复排一个，
        于是好几个"做了吗"挂在队列里，每个都算得偏早——他还没起床她就来问。
        他的计划本来有台账那一套管着（按事情本身的周期问），这里不该再叠一层。
        """
        if await self.memory.pending_jobs("follow_up", conversation_id):
            log.info("[life] 已经有一个 follow_up 排着了，这个不排")
            return 0
        now = self.clock.now()
        run_at = now + timedelta(minutes=delay_minutes * self.scheduler.delay_scale)
        # 他刚说了带时间的计划的话，这个 follow_up 不能比那件事该问的时候还早。
        # 靠提示词让模型别把他的计划塞进来是请求，不是约束。
        hold = await self.follow_up_hold(now, now)
        if hold is not None and hold > run_at:
            log.info(
                "[life] 这个 follow_up 比他说的时间还早，推到 %s", hold.strftime("%m-%d %H:%M")
            )
            run_at = hold
        run_at = self._awake_at_or_after(run_at)
        return await self.scheduler.schedule(
            "follow_up",
            run_at,
            conversation_id=conversation_id,
            payload={"kind": "follow_up", "note": note},
            reason="follow_up",
        )

    async def mark_proactive_sent(self, kind_name: str, day: date, conversation_id: str) -> None:
        """记下这次主动，用于同类间隔和"没被回应"的衰减。"""
        await self.memory.kv_set(f"last_proactive:{kind_name}", day.isoformat())
        conv = await self.memory.get_conversation(conversation_id)
        await self.memory.update_conversation(
            conversation_id,
            unanswered_initiations=conv.unanswered_initiations + 1,
            last_initiation_date=day,
        )

    async def can_initiate_today(self, conversation_id: str, day: date) -> bool:
        """今天已经主动过而且他还没回，就别再开新话头了。"""
        conv = await self.memory.get_conversation(conversation_id)
        if conv.last_initiation_date != day:
            return True
        return conv.unanswered_initiations < self.persona.proactive.max_unanswered_per_day

    # -- 任务入口 -----------------------------------------------------------

    DAY_PLAN_RETRIES = 3
    """起床那次日程没生成出来，当天最多再试几次。"""

    async def handle_day_plan_job(self, job: Job) -> None:
        conversation_id = job.conversation_id or "owner"
        await self.ensure_today_plan(conversation_id)
        await self.schedule_next_day_plan()

        # 起床那次失败了（模型抖了一下、输出解析不了），当天得再试。
        # 原来只打一行"稍后再试"，可是没人排那个"稍后"：一整天没有日程，
        # 也就一整天不会主动开口。
        now = self.clock.now()
        day = self.rhythm.local_date(now)
        tries = int(job.payload.get("retry", 0))
        if await self.memory.get_day_plan(day) is not None or tries >= self.DAY_PLAN_RETRIES:
            return
        run_at = now + timedelta(minutes=self.rng.uniform(30, 60))
        if self.rhythm.is_sleeping(run_at) or self.rhythm.local_date(run_at) != day:
            return
        await self.scheduler.schedule(
            "day_plan",
            run_at,
            conversation_id=conversation_id,
            payload={"retry": tries + 1},
            dedupe_key=f"day_plan_retry:{day}:{tries + 1}",
            reason=f"{day} 的日程再试一次",
        )
