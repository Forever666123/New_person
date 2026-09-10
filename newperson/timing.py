"""回复时机：像真人一样"什么时候看到、什么时候回"。

纯逻辑，所有随机通过传入的 ``random.Random``。规则见 DESIGN.md 2.2。
"""

from __future__ import annotations

import random
from datetime import datetime

from .models import Heat, MessageFeatures, TimingDecision
from .persona import TimingConfig
from .rhythm import Rhythm

HOT_SECONDS = 3 * 60
WARM_SECONDS = 45 * 60
BURST_GAP_HOT = 25.0
BURST_GAP_OTHER = 45.0

# (median_seconds, sigma) 按 state -> heat
DELAY_TABLE: dict[str, dict[Heat, tuple[float, float]]] = {
    "free": {"hot": (20, 0.5), "warm": (4 * 60, 0.8), "cold": (20 * 60, 0.9)},
    "winding_down": {"hot": (30, 0.5), "warm": (6 * 60, 0.8), "cold": (25 * 60, 0.9)},
    "busy": {"hot": (60, 0.6), "warm": (15 * 60, 0.8), "cold": (40 * 60, 0.9)},
}


def heat_of(now: datetime, last_user_at: datetime | None, last_bot_at: datetime | None) -> Heat:
    """按双方最后一次交流距今的时间判定热度。两个都为 None 视为 cold。"""
    raise NotImplementedError


def extract_features(texts: list[str], has_image: bool = False) -> MessageFeatures:
    """从一批未读消息的文本里提取特征（是否问题、是否紧急、总长度）。"""
    raise NotImplementedError


class ReplyTimingPolicy:
    def __init__(self, config: TimingConfig, rhythm: Rhythm, delay_scale: float = 1.0) -> None:
        self.config = config
        self.rhythm = rhythm
        self.delay_scale = delay_scale

    def plan_reply(
        self,
        now: datetime,
        heat: Heat,
        features: MessageFeatures,
        last_user_message_at: datetime,
        rng: random.Random,
        fell_asleep_at: datetime | None = None,
    ) -> TimingDecision:
        """决定 notice_at / reply_at。

        - 用 ``self.rhythm.state_at(now)`` 取作息状态。
        - sleeping：notice = next_wake + U(5,45)min；若 heat==hot 且距入睡不到 20 分钟，
          允许一次快速回复并置 ``quick_before_sleep=True``。
        - busy：采样后上限为 until + U(2,15)min。
        - 问题/紧急乘 urgent_multiplier（最低 8s）；长消息加阅读时间 len/6 秒。
        - reply_at 若落入睡眠 → 推到起床 + U(5,45)min。
        - 防抖：reply_at >= last_user_message_at + burst_gap。
        - 总延迟不超过 max_delay_hours（睡眠推迟除外）。
        - 最后把相对 now 的延迟乘以 delay_scale。
        - reason 写清楚每一步（供日志）。
        """
        raise NotImplementedError

    def merge_pending(self, existing_reply_at: datetime, now: datetime, heat: Heat) -> datetime:
        """已有待回复任务时，新消息到来：返回新的 reply_at = max(existing, now + burst_gap)。"""
        raise NotImplementedError

    def typing_duration(self, text: str, rng: random.Random) -> float:
        """打字时长（秒）= 1.0 + len/cps + N(0,0.5)，限制在 [1.5, 40]，再乘 delay_scale。"""
        raise NotImplementedError

    def sample_lognormal(self, median: float, sigma: float, rng: random.Random) -> float:
        """中位数为 median 的对数正态采样。"""
        raise NotImplementedError
