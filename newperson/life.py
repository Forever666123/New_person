"""生活引擎：每日日程、主动消息候选、follow-up。见 DESIGN.md 2.5。"""

from __future__ import annotations

import random
from datetime import date, datetime

from .brain import Brain
from .clock import Clock
from .memory import Memory
from .models import DayPlan, Job, PlanEvent
from .persona import Persona
from .rhythm import Rhythm
from .scheduler import Scheduler


class LifeEngine:
    def __init__(
        self,
        persona: Persona,
        rhythm: Rhythm,
        memory: Memory,
        scheduler: Scheduler,
        brain: Brain,
        clock: Clock,
        rng: random.Random,
    ) -> None:
        self.persona = persona
        self.rhythm = rhythm
        self.memory = memory
        self.scheduler = scheduler
        self.brain = brain
        self.clock = clock
        self.rng = rng

    async def ensure_today_plan(self, conversation_id: str) -> DayPlan | None:
        """今天没有日程就生成并保存，然后安排主动消息候选（只安排一次，用 kv 记录 'proactive_scheduled:<date>'）。
        生成失败返回 None（不阻塞其他功能）。"""
        raise NotImplementedError

    def schedule_next_day_plan(self) -> None:
        """在下一次起床时间 + 少许随机安排一个 day_plan 任务（幂等：已有 pending 的 day_plan 就不再加）。"""
        raise NotImplementedError

    def current_event(self, plan: DayPlan | None, now: datetime) -> PlanEvent | None:
        """now 落在哪个事件里；没有返回 None。"""
        raise NotImplementedError

    def recent_event(self, plan: DayPlan | None, now: datetime) -> PlanEvent | None:
        """当前事件，或最近刚结束（2 小时内）的事件；用于自定义状态和"我刚……"。"""
        raise NotImplementedError

    def candidate_moments(self, plan: DayPlan, day: date, silent_days: int) -> list[tuple[datetime, str, str, list[str]]]:
        """生成候选 [(run_at, trigger_kind, note, photo_tags)]：
        - 每个 shareable 事件在 [start,end] 内随机一刻（event_share）
        - random_chat_slots 个在 free 时段随机一刻（random_chat）
        - silent_days >= reach_out_after_silent_days 时一个 reach_out
        然后按 base_probability 抽样、按 max_per_day 截断（保留最早的？不，随机保留），过滤掉睡眠时段与已过去的时刻。"""
        raise NotImplementedError

    async def schedule_proactive_candidates(self, plan: DayPlan, conversation_id: str) -> int:
        """把候选写成 proactive 任务，返回条数。"""
        raise NotImplementedError

    async def schedule_follow_up(self, conversation_id: str, delay_minutes: int, note: str) -> int:
        raise NotImplementedError

    async def handle_day_plan_job(self, job: Job) -> None:
        raise NotImplementedError
