"""提示词模板。

分两层：
- ``build_system(persona)``：**稳定**部分（人物设定、说话规范、输出规范）。这一段每次请求都完全相同，
  用来做 prompt caching，所以里面**不能**出现时间、日期、随机内容。
- ``build_*_user(...)``：**易变**部分，作为 user 消息传入：当前时间、作息状态、今日日程与日记、
  记忆摘要、事实、最近消息、未读消息、可用照片标签等。

语言：中文（persona.language 可切换措辞，但本项目先只做中文）。
"""

from __future__ import annotations

from datetime import datetime

from .models import DayPlan, PlanEvent, RhythmSnapshot, StoredMessage
from .persona import Persona


def build_system(persona: Persona) -> str:
    """稳定 system prompt。内容要求见 DESIGN.md 2.8：像真人发 IM、短句、可不回、可只用表情反应、
    {photo} 占位规则、永不承认是 AI（若 rules.never_admit_ai）、不用 markdown。"""
    raise NotImplementedError


def format_time(dt: datetime) -> str:
    """'2026-09-10 周四 14:32'"""
    raise NotImplementedError


def format_messages(messages: list[StoredMessage], persona_name: str, owner_nickname: str) -> str:
    """把消息列表渲染成 '[14:32] 对方：xxx' / '[14:35] 我：xxx' 的文本；附件写成 [图片]。"""
    raise NotImplementedError


def build_reply_user(
    *,
    persona: Persona,
    now: datetime,
    snapshot: RhythmSnapshot,
    day_plan: DayPlan | None,
    current_event: PlanEvent | None,
    diary_notes: list[str],
    summary: str,
    user_facts: list[str],
    self_facts: list[str],
    recent: list[StoredMessage],
    unread: list[StoredMessage],
    waited_minutes: float,
    quick_before_sleep: bool,
    available_photo_tags: list[str],
) -> str:
    """回复任务的 user 消息。要告诉模型：现在几点、你在干嘛、距离对方发消息过去了多久（解释为什么现在才回）、
    对话摘要与事实、最近对话、这次要回的未读消息、有哪些照片标签可用、输出 ReplyPlan。"""
    raise NotImplementedError


def build_proactive_user(
    *,
    persona: Persona,
    now: datetime,
    snapshot: RhythmSnapshot,
    day_plan: DayPlan | None,
    trigger_kind: str,
    trigger_note: str,
    diary_notes: list[str],
    summary: str,
    user_facts: list[str],
    self_facts: list[str],
    recent: list[StoredMessage],
    hours_since_last_exchange: float | None,
    available_photo_tags: list[str],
) -> str:
    """主动消息的 user 消息。trigger_kind ∈ {event_share, random_chat, reach_out, follow_up}。
    强调：可以选择不发（send=false）；不要重复已经说过的事；别显得黏人。"""
    raise NotImplementedError


def build_day_plan_user(*, persona: Persona, day: datetime, weekday_schedule_text: str,
                        yesterday_plan: DayPlan | None, summary: str, user_facts: list[str],
                        self_facts: list[str]) -> str:
    """生成今日日程的 user 消息：给出今天日期/星期、固定作息、昨天的日程、和对方的近况，要求输出 DayPlan。
    事件时间要落在清醒时段内，busy 时段的事件与 persona 作息一致。"""
    raise NotImplementedError


def build_memory_update_user(*, persona: Persona, previous_summary: str, messages: list[StoredMessage],
                             existing_user_facts: list[str], existing_self_facts: list[str]) -> str:
    """滚动摘要 + 事实抽取。只输出新的事实，不要重复已有。"""
    raise NotImplementedError
