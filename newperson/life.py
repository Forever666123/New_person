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
from datetime import date, datetime, time, timedelta

from .brain import Brain, DayPlanRequest
from .calendar import AcademicCalendar
from .clock import Clock
from .memory import Memory
from .models import DayPlan, Job, PlanEvent
from .persona import Persona, ProactiveKind, parse_hhmm
from .rhythm import Rhythm
from .scheduler import Scheduler

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
            return existing
        if not await self.memory.claim_day_plan(day, now):
            return None  # 别人正在生成

        daily = self.rhythm.for_day(day)
        yesterday = await self.memory.get_day_plan(day - timedelta(days=1))
        conv = await self.memory.get_conversation(conversation_id)

        plan = await self.brain.generate_day_plan(
            DayPlanRequest(
                now=now,
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
        if self.rng.random() > cfg.day_probability * decay * chattiness:
            return []

        now = self.clock.now()
        wake, sleep = self._awake_window(day)
        travelling = self.calendar.trip_for(day) is not None

        shareable = [e for e in plan.events if e.shareable]

        eligible: list[ProactiveKind] = []
        for kind in cfg.kinds:
            if kind.only_while_travelling and not travelling:
                continue
            if await self._days_since_last(kind.name, day) < kind.min_days_since_last:
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

            note = kind.note
            if shareable and kind.name in ("own_life", "travel_note"):
                event = self.rng.choice(shareable)
                note = f"{note}\n今天可以提的是：{event.title}。{event.detail}"
                if event.share_hint:
                    note += f"（{event.share_hint}）"
            candidates.append((moment, kind, note))

        return sorted(candidates, key=lambda c: c[0])

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
        """在她醒着的时间里随机挑一刻，受 kind 的时段限制。"""
        if sleep <= wake:
            return None
        for _ in range(30):
            offset = self.rng.uniform(0, (sleep - wake).total_seconds())
            moment = wake + timedelta(seconds=offset)
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

    async def schedule_follow_up(self, conversation_id: str, delay_minutes: int, note: str) -> int:
        """她说了"我查完告诉你"，就真的要记得回来说。"""
        run_at = self.clock.now() + timedelta(minutes=delay_minutes * self.scheduler.delay_scale)
        if self.rhythm.is_sleeping(run_at):
            run_at = self.rhythm.first_glance_after_waking(
                self.rhythm.next_wake_after(run_at), self.rng
            )
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

    async def handle_day_plan_job(self, job: Job) -> None:
        await self.ensure_today_plan(job.conversation_id or "owner")
        await self.schedule_next_day_plan()
