"""Discord 适配层：收消息、发消息、在线状态。

组成：
- ``NewPersonClient(discord.Client)``：网关事件 → ``App``。
- ``PresenceManager``：每 60s 按作息更新 presence（DESIGN.md 2.6）。
- ``App``：组合根，把 memory / scheduler / brain / life / delivery 串起来，注册各类任务的 handler：
    - reply：读未读消息 → 构造 ReplyContext → brain.generate_reply → media.resolve → deliver →
      存库、mark_read、follow_up、diary、必要时安排 memory_update；被打断则按 hot 规则重新安排。
    - proactive / follow_up：检查 sleeping / hot → brain.generate_proactive → deliver → 存库。
    - day_plan：life.handle_day_plan_job。
    - memory_update：brain.update_memory → 更新 summary / facts。

权限：只处理 DM 里来自 allowed 用户的消息，或频道里 @人物 的消息（同样限 allowed 用户）。忽略机器人。
附件：image/* 且 ≤ 5MB 的下载到 downloads_dir，作为 images 传给 brain。
"""

from __future__ import annotations

import logging
import random

import discord

from .brain import Brain
from .clock import Clock
from .config import Settings
from .delivery import Deliverer
from .life import LifeEngine
from .media import MediaService
from .memory import Memory
from .models import Job
from .persona import Persona
from .rhythm import Rhythm
from .scheduler import Scheduler
from .timing import ReplyTimingPolicy

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024


class App:
    def __init__(
        self,
        settings: Settings,
        persona: Persona,
        clock: Clock,
        rhythm: Rhythm,
        timing: ReplyTimingPolicy,
        memory: Memory,
        scheduler: Scheduler,
        brain: Brain,
        media: MediaService,
        life: LifeEngine,
        deliverer: Deliverer,
        rng: random.Random,
    ) -> None:
        raise NotImplementedError

    async def start(self, client: discord.Client) -> None:
        """on_ready 时调用：打开 memory、reset running、注册 handler、ensure_today_plan、启动 scheduler 与 presence 循环。"""
        raise NotImplementedError

    async def on_user_message(self, message: discord.Message) -> None:
        """过滤 → 存库 → 有 pending reply 就 merge，否则 plan_reply 并 schedule。"""
        raise NotImplementedError

    async def handle_reply_job(self, job: Job) -> None:
        raise NotImplementedError

    async def handle_proactive_job(self, job: Job) -> None:
        raise NotImplementedError

    async def handle_memory_update_job(self, job: Job) -> None:
        raise NotImplementedError


class PresenceManager:
    def __init__(self, client: discord.Client, persona: Persona, rhythm: Rhythm, life: LifeEngine, memory: Memory, clock: Clock) -> None:
        raise NotImplementedError

    async def run_forever(self) -> None:
        raise NotImplementedError

    async def apply_once(self) -> None:
        raise NotImplementedError


class NewPersonClient(discord.Client):
    def __init__(self, app: App) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.app = app

    async def on_ready(self) -> None:
        raise NotImplementedError

    async def on_message(self, message: discord.Message) -> None:
        raise NotImplementedError


def build_app(settings: Settings, persona: Persona, *, client: object | None = None, seed: int | None = None) -> App:
    """组合根：创建所有组件。client 为 AsyncAnthropic（None 时自动创建）。"""
    raise NotImplementedError
