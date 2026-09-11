"""注意力：她什么时候看到消息，什么时候回。

这是整个项目里最影响"像不像人"的一块。核心想法是**人不是收到消息后等一个随机时间再回，
而是隔一阵看一眼手机，看到了才处理**。所以延迟不是凭空掷出来的，而是：

1. 下一次看手机是什么时候（由 :mod:`rhythm` 的活跃度决定）
2. 看到了当场处理吗，还是先放着（``engage_probability``）
3. 决定回之后，从看到到发出去还要多久（打开对话、想一下、打字）

**没有秒回。** 就算正在聊，中位数也是一分多钟，三五分钟才回一句很常见。
手机拿起来放下、被别的事岔开，都是这一段时间。
"""

from __future__ import annotations

import math
import random
import re
from datetime import datetime, timedelta

from .models import Heat, MessageFeatures, RhythmSnapshot, TimingDecision
from .persona import Persona
from .rhythm import Rhythm

_QUESTION = re.compile(r"[?？]|吗[\s?？]*$|(在不在|在吗|怎么办|为什么|多少|哪个|要不要|是不是)")
_URGENT = re.compile(r"(急|快点|救命|出事|紧急|马上|!!!|？？？)")

MIN_DELAY_SECONDS = 8.0
"""再急也不会比这更快。"""


def heat_of(
    now: datetime,
    last_user_at: datetime | None,
    last_bot_at: datetime | None,
    hot_seconds: float,
    warm_seconds: float,
) -> Heat:
    """按双方最后一次交流距今多久，判断这段对话还热不热。

    只看**已经发生**的交流。排在未来的回复不算数：它还没发出去，
    算进来会得到负数间隔，反而被判成正在热聊。
    """
    stamps = [t for t in (last_user_at, last_bot_at) if t is not None and t <= now]
    if not stamps:
        return "cold"
    gap = (now - max(stamps)).total_seconds()
    if gap <= hot_seconds:
        return "hot"
    if gap <= warm_seconds:
        return "warm"
    return "cold"


def extract_features(texts: list[str], persona: Persona, has_image: bool = False) -> MessageFeatures:
    """从一批未读消息里提取影响回复时机的特征。"""
    joined = "\n".join(texts)
    mode = persona.mode_for(joined)
    return MessageFeatures(
        is_question=bool(_QUESTION.search(joined)),
        is_urgent=bool(_URGENT.search(joined)),
        length=len(joined),
        has_image=has_image,
        mode=mode.name if mode else None,
    )


class AttentionPolicy:
    def __init__(self, persona: Persona, rhythm: Rhythm, delay_scale: float = 1.0) -> None:
        self.persona = persona
        self.rhythm = rhythm
        self.delay_scale = delay_scale

    # -- 采样 ---------------------------------------------------------------

    def _lognormal(self, median_seconds: float, sigma: float, rng: random.Random) -> float:
        """中位数为 median 的对数正态。用它是因为真人的延迟是右偏的：
        多数时候几分钟，偶尔拖很久，但不会出现负数。"""
        return median_seconds * math.exp(rng.gauss(0, sigma))

    def _mode_multiplier(self, features: MessageFeatures) -> float:
        if not features.mode:
            return 1.0
        for mode in self.persona.modes:
            if mode.name == features.mode:
                return mode.delay_multiplier
        return 1.0

    def _reading_seconds(self, features: MessageFeatures) -> float:
        """长消息要先读完。"""
        return max(0.0, (features.length - 120) / 6.0)

    def _fatigue_multiplier(self, now: datetime, session_started_at: datetime | None) -> float:
        """连着聊久了会累，回得越来越慢，直到自然收尾。"""
        if session_started_at is None:
            return 1.0
        minutes = (now - session_started_at).total_seconds() / 60
        over = minutes - self.persona.timing.fatigue_after_minutes
        if over <= 0:
            return 1.0
        return min(8.0, 2 ** (over / 15))

    # -- 主流程 -------------------------------------------------------------

    def plan_reply(
        self,
        now: datetime,
        heat: Heat,
        features: MessageFeatures,
        last_user_message_at: datetime,
        rng: random.Random,
        session_started_at: datetime | None = None,
    ) -> TimingDecision:
        """决定她什么时候看到这批消息、什么时候开始回。"""
        timing = self.persona.timing
        snapshot = self.rhythm.state_at(now)
        daily = self.rhythm.daily_for(now)
        steps: list[str] = [f"{snapshot.state}/{heat}"]
        if snapshot.trip_place:
            steps.append(f"人在{snapshot.trip_place}")

        notice, defers = self._plan_notice(now, heat, snapshot, daily, rng, steps)

        # 从"看到"到"开始打字"：点开对话、想一下、切到输入框
        if heat == "hot":
            lag = self._lognormal(timing.hot_reply_median_seconds, timing.hot_reply_sigma, rng)
        elif heat == "warm":
            lag = self._lognormal(45, 0.7, rng)
        else:
            lag = self._lognormal(70, 0.7, rng)

        lag += self._reading_seconds(features)

        multiplier = self._mode_multiplier(features)
        if features.is_urgent or features.is_question:
            multiplier *= timing.urgent_multiplier
            steps.append("是问题或者听着急")
        fatigue = self._fatigue_multiplier(now, session_started_at)
        if fatigue > 1.05:
            steps.append(f"聊久了，慢下来×{fatigue:.1f}")
        lag = max(MIN_DELAY_SECONDS, lag * multiplier * fatigue)

        reply = notice + timedelta(seconds=lag)
        steps.append(f"看到后 {self._pretty(lag)} 开始回")

        # 快睡着的时候最后回一句，是真人会做的事
        quick_before_sleep = False
        if heat == "hot" and snapshot.state in ("winding_down", "free"):
            since_sleep = (now - daily.sleep_start).total_seconds()
            if 0 <= since_sleep <= 20 * 60:
                quick_before_sleep = True
                steps.append("已经躺下了，最后回一句")

        reply = self._push_out_of_sleep(reply, rng, steps)
        reply = self._respect_burst_gap(reply, last_user_message_at, heat, rng, steps)
        reply = self._cap_total_delay(now, reply, notice, steps)
        notice = min(notice, reply)

        # 先按真实时长算处境提示，再缩放。
        # DELAY_SCALE 是调试用的加速器，不该改变她说什么：
        # 压缩之后"隔了六小时"变成"隔了十几秒"，她就不知道自己该不该提这件事了。
        hints = self._context_hints(
            reply, now, last_user_message_at, snapshot, defers, fatigue
        )
        notice, reply = self._scale(now, notice, reply)
        return TimingDecision(
            notice_at=notice,
            reply_at=reply,
            reason="；".join(steps),
            defers=defers,
            quick_before_sleep=quick_before_sleep,
            hints=hints,
        )

    def _plan_notice(
        self,
        now: datetime,
        heat: Heat,
        snapshot: RhythmSnapshot,
        daily,  # DailyRhythm
        rng: random.Random,
        steps: list[str],
    ) -> tuple[datetime, int]:
        """她什么时候看到这条消息。"""
        if heat == "hot":
            steps.append("正在聊，手机就在手上")
            return now, 0

        glance = self.rhythm.next_glance_after(now, rng)
        if heat == "warm":
            # 刚聊完，手机还在旁边，不一定要等到下一次正经看手机
            glance = min(glance, now + timedelta(seconds=self._lognormal(4 * 60, 0.8, rng)))

        # 看到了不一定当场处理。没空、在路上、懒得打字，就先放着，下次再说。
        # 概率跟着看手机那一刻的活跃度走，不是每天一个固定值。
        # 例外：睡着时积压的消息，醒来那一眼基本都会处理。
        # 人睡醒看到攒了一晚上的消息，不会说"待会儿再说"。
        overnight = self.rhythm.is_sleeping(now)
        defers = 0
        max_defers = self.rhythm.config.max_defers
        while defers < max_defers:
            engage = self.rhythm.engage_probability_at(glance)
            if overnight and defers == 0:
                engage = 1 - (1 - engage) * 0.25
            if rng.random() <= engage:
                break
            nxt = self.rhythm.next_glance_after(glance + timedelta(seconds=1), rng)
            if nxt <= glance:
                break
            glance = nxt
            defers += 1

        if defers:
            steps.append(f"看到了先放着，第 {defers + 1} 次拿手机才处理")
        else:
            steps.append(f"下次看手机 {glance.strftime('%m-%d %H:%M')}")
        return glance, defers

    def _push_out_of_sleep(
        self, reply: datetime, rng: random.Random, steps: list[str]
    ) -> datetime:
        """算出来的时间要是落在睡觉里，推到醒来之后。"""
        if not self.rhythm.is_sleeping(reply):
            return reply
        pushed = self.rhythm.first_glance_after_waking(self.rhythm.next_wake_after(reply), rng)
        steps.append(f"那会儿在睡觉，推到 {pushed.strftime('%m-%d %H:%M')}")
        return pushed

    def _respect_burst_gap(
        self,
        reply: datetime,
        last_user_message_at: datetime,
        heat: Heat,
        rng: random.Random,
        steps: list[str],
    ) -> datetime:
        """对方还在连着发，就等他说完。但不能无限等，否则永远回不出去。"""
        gap = self._lognormal(25 if heat == "hot" else 40, 0.4, rng)
        floor = last_user_message_at + timedelta(seconds=gap)
        if floor <= reply:
            return reply
        cap = last_user_message_at + timedelta(seconds=90 if heat == "hot" else 180)
        pushed = min(floor, cap)
        if pushed > reply:
            steps.append("他还在打字，等一下")
        return pushed

    def _cap_total_delay(
        self, now: datetime, reply: datetime, notice: datetime, steps: list[str]
    ) -> datetime:
        """封顶。

        睡觉推迟本身是合理的长延迟，不算在内，但**醒来之后**不能再拖很久：
        人睡醒第一件事就是看手机，积压一晚上的东西不会再压到晚上。
        早先这里写成"睡着时直接不封顶"，于是一次"先放着"叠上早上很低的活跃度，
        能把一条凌晨的消息拖到下午六点。
        """
        if self.rhythm.is_sleeping(now):
            wake = self.rhythm.next_wake_after(now)
            cap = wake + timedelta(hours=self.persona.timing.backlog_after_wake_hours)
            if reply > cap:
                steps.append("睡醒之后不会再拖了")
                return cap
            return reply

        limit = timedelta(hours=self.persona.timing.max_delay_hours)
        if reply - now <= limit or reply - notice <= limit:
            return reply
        steps.append(f"封顶到 {self.persona.timing.max_delay_hours} 小时")
        return notice + limit

    def _scale(self, now: datetime, notice: datetime, reply: datetime) -> tuple[datetime, datetime]:
        """调试用的整体加速。1.0 就是真实节奏。"""
        if self.delay_scale == 1.0:
            return notice, reply
        return (
            now + (notice - now) * self.delay_scale,
            now + (reply - now) * self.delay_scale,
        )

    def _context_hints(
        self,
        reply: datetime,
        now: datetime,
        last_user_message_at: datetime,
        snapshot: RhythmSnapshot,
        defers: int,
        fatigue: float,
    ) -> list[str]:
        """给模型的处境提示。这些会进上下文，不会直接发出去。

        "他等了多久"要从**他发消息那一刻**算起，不是从"现在"算起。
        原来用的是 ``reply - now``，正常收消息时两者几乎一样，
        但补抓停机期间的消息时差着好几个小时：一条三小时前的话，
        提示里会写成"20 分钟前发的"，甚至因为没超过阈值而整条不出现——
        于是那句"别解释这段时间你在干嘛"没了，她就真的会解释。
        """
        hints: list[str] = []
        waited = (reply - min(now, last_user_message_at)).total_seconds() / 60
        if waited > 10:
            hints.append(
                f"他这条消息是 {self._pretty(waited * 60)} 前发的。"
                "**别解释这段时间你在干嘛**：不说在睡觉、不说刚看到、"
                "不说抱歉、不交代去哪了。直接接着他的话说。"
            )
        if defers:
            hints.append("你其实早看到了，只是当时没回。别提这件事。")
        if fatigue > 2:
            hints.append("你们已经聊了一阵了，可以自然收尾去做自己的事。")

        to_sleep = (snapshot.next_sleep - reply).total_seconds() / 60
        if 0 < to_sleep < 25:
            hints.append("你差不多要睡了，可以顺口说一句就下线。")
        if snapshot.state == "busy" and snapshot.block_title:
            hints.append(f"你现在在{snapshot.block_title}，只能偷偷回一句。")
        if snapshot.trip_place:
            hints.append(f"你人在{snapshot.trip_place}，跟他那边的时差和平时不一样。")
        return hints

    # -- 其他 ---------------------------------------------------------------

    def merge_pending(
        self, existing_reply_at: datetime, now: datetime, heat: Heat, rng: random.Random
    ) -> datetime:
        """已经排好队要回了，对方又发一条：往后挪一点，等他说完，但有上限。

        **基准要取"已排的时刻"和"现在"里靠后的那个。**
        原来上限是 ``existing_reply_at + 90 秒``，而那条任务可能已经过期了
        （重启之后、或者调度器还没轮到它）。已排时刻在五分钟前的话，
        上限就是"四分半钟前"——外层的 min 会挑中它，于是回复被排到过去，
        调度器下一拍立刻发出去。那就是实打实的秒回，这个项目最不能出的事。
        """
        base = max(existing_reply_at, now)
        gap = self._lognormal(25 if heat == "hot" else 40, 0.4, rng)
        cap = base + timedelta(seconds=90 if heat == "hot" else 180)
        return min(max(base, now + timedelta(seconds=gap)), cap)

    def typing_duration(self, text: str, rng: random.Random) -> float:
        """打这条话要多久。手机打字比键盘慢。"""
        cps = self.persona.style.typing_chars_per_second
        seconds = 1.0 + len(text) / max(cps, 0.5) + rng.gauss(0, 0.5)
        return max(1.5, min(40.0, seconds)) * self.delay_scale

    @staticmethod
    def _pretty(seconds: float) -> str:
        if seconds < 90:
            return f"{seconds:.0f} 秒"
        if seconds < 90 * 60:
            return f"{seconds / 60:.0f} 分钟"
        return f"{seconds / 3600:.1f} 小时"
