"""人物设定（persona.yaml）的加载与校验。

设计原则：**不要写死**。
这里几乎所有"行为"字段都是倾向、权重、概率，而不是硬性规则。
真正硬的只有两类：

1. 说话风格里的机械约束（不用 emoji、不打句号之类），由 ``style_guard`` 在发出前做后处理，
   不塞进提示词里当二十条军规，那样模型会写得很拘谨。
2. ``boundaries.never_say`` 里的禁用语，同样在后处理里拦截。

其余（作息、主动消息、话题模式）全部交给概率和上下文提示，让模型自己发挥。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import AcademicPeriod

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> tuple[int, int]:
    """把 ``"HH:MM"`` 解析成 ``(hour, minute)``，格式不对就抛 ``ValueError``。"""
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    return int(m.group(1)), int(m.group(2))


def hhmm_to_minutes(value: str) -> int:
    h, m = parse_hhmm(value)
    return h * 60 + m


# ---------------------------------------------------------------------------
# 作息：倾向 + 每日抽签
# ---------------------------------------------------------------------------


class SleepDistribution(BaseModel):
    """睡觉时间不是一个区间，是一个分布。每天从这里采样一次。"""

    start_median: str = "01:30"
    """入睡时间的中位数（可以在午夜之后）。"""
    start_sigma_minutes: float = 70.0
    wake_median: str = "08:30"
    wake_sigma_minutes: float = 65.0
    min_hours: float = 4.5
    """采样后强制的最短睡眠，防止抽出荒唐的值。"""
    max_hours: float = 11.0

    @field_validator("start_median", "wake_median")
    @classmethod
    def _check(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class ActivityCurve(BaseModel):
    """一天里"会看手机"的活跃度曲线，0 到 1。

    ``points`` 是阶梯函数：key 为该段起点 ``HH:MM``，value 为这一段的活跃度，
    直到下一个 key 为止。必须包含 ``"00:00"``。

    活跃度只影响"多久瞄一眼手机"，不是"回不回"。回不回另有概率。
    """

    points: dict[str, float] = Field(
        default_factory=lambda: {"00:00": 0.5, "02:00": 0.05, "08:00": 0.3, "19:00": 0.8}
    )

    @field_validator("points")
    @classmethod
    def _check(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("activity.points 不能为空")
        for k, val in v.items():
            parse_hhmm(k)
            if not 0.0 <= val <= 1.0:
                raise ValueError(f"activity.points[{k}] 应在 0 到 1 之间，收到 {val}")
        if "00:00" not in v:
            raise ValueError('activity.points 必须包含 "00:00" 这一段')
        return v

    def sorted_points(self) -> list[tuple[int, float]]:
        return sorted(((hhmm_to_minutes(k), v) for k, v in self.points.items()), key=lambda x: x[0])

    def at_minutes(self, minute_of_day: int) -> float:
        """取该时刻所在段的活跃度。"""
        pts = self.sorted_points()
        value = pts[-1][1]  # 默认用最后一段（跨过午夜回绕）
        for start, val in pts:
            if minute_of_day >= start:
                value = val
            else:
                break
        return value


class ClassBlock(BaseModel):
    """课/固定安排。``probability`` 表示"今天真的去了"的概率，允许翘课。"""

    days: list[int] = Field(default_factory=list, description="0=周一 … 6=周日")
    start: str
    end: str
    title: str = "上课"
    probability: float = 1.0
    activity: float | None = None
    """本想做这段时间的活跃度覆盖值。**目前没接上**：上课一律用 class_activity。"""

    @field_validator("start", "end")
    @classmethod
    def _check(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class LifePhase(BaseModel):
    """跨天的阶段。忙是成片的，不是每天独立掷骰子。

    考试周、赶 project、刚放假，这些会连续影响好几天。阶段之上再叠加当日变体，
    所以"她今天一直没回"往往是"这周本来就忙 + 昨晚熬夜 + 他刚好在她睡觉时发的"三件事撞在一起，
    而不是某个开关被打开了。
    """

    name: str
    weight: float = 1.0
    min_days: int = 3
    max_days: int = 8
    activity_multiplier: float = 1.0
    engage_multiplier: float = 1.0
    note: str = ""
    only_in_session: bool = False
    """只在上课期间可能抽到。放假的时候不该有"赶 due"这种阶段。"""
    skip_in_finals: bool = False
    """期末周抽不到。期末周的心态日历已经给了（人很紧），不该再叠一个"最近没什么事"。"""


class WeekendConfig(BaseModel):
    """周末的作息。研究生周五周六晚睡、周末晚起，是人本来就有的规律。"""

    late_nights: list[int] = Field(default_factory=lambda: [4, 5])
    """哪几天晚上睡得晚（0 是周一）。"""
    sleep_shift_hours: float = 0.0
    late_mornings: list[int] = Field(default_factory=lambda: [5, 6])
    """哪几天早上起得晚。"""
    wake_shift_hours: float = 0.0


class DayVariant(BaseModel):
    """当日变体。每天按 ``weight`` 抽一个，用来打破规律。"""

    name: str
    weight: float = 1.0
    sleep_start_shift_hours: float = 0.0
    wake_shift_hours: float = 0.0
    activity_multiplier: float = 1.0
    engage_probability: float | None = None
    """看到消息后当场处理的概率；None 表示用 rhythm.engage_probability。"""
    note: str = ""
    """一句话注入当天的上下文，比如"你今天不太想说话"。不会直接发给对方。"""


class RhythmConfig(BaseModel):
    phases: list[LifePhase] = Field(default_factory=list)
    """跨天的阶段序列。为空则一直是"平常"。"""
    sleep: SleepDistribution = Field(default_factory=SleepDistribution)
    activity: ActivityCurve = Field(default_factory=ActivityCurve)
    class_activity: float = 0.15
    """上课时段的默认活跃度（偷偷回一句的程度）。"""
    classes: list[ClassBlock] = Field(default_factory=list)
    variants: list[DayVariant] = Field(default_factory=list)
    weekend: WeekendConfig = Field(default_factory=WeekendConfig)
    sleep_follow_weight: float = 0.8
    """起床时刻有多跟着昨晚的入睡走。0 是完全按生物钟，1 是完全跟着昨晚。

    真人介于两者之间：熬夜会起得晚，但有课有闹钟，不会一路睡到下午。
    """
    base_glance_minutes: float = 8.0
    """活跃度为 1 时，两次看手机的间隔中位数。实际间隔 = base / activity。"""
    glance_sigma: float = 0.6
    engage_probability: float = 0.85
    """看到消息之后当场处理的概率。

    没中不代表这条消息被丢掉，而是**先放着**，等下一次看手机再说。
    这就是"看到了但当时没空回"，延迟自然被拉长到几小时，而不是石沉大海。
    """
    max_defers: int = 3
    """最多先放着几次。到了上限就必须处理，免得消息永远不被回。"""
    winding_down_minutes: float = 60.0
    """距离入睡还有多久算"准备睡了"。"""
    min_activity_to_glance: float = 0.02
    """低于这个活跃度就完全不看手机（睡着了）。"""


# ---------------------------------------------------------------------------
# 学期日历与出行
# ---------------------------------------------------------------------------


class TravelSpot(BaseModel):
    place: str
    timezone: str = ""
    note: str = ""
    transit_hours: float = 0.0
    """路上要多久（飞机加转机）。这段时间她不在线，去程和回程都算。

    不写的话，出发那晚睡六个小时就在上海起床了——比十五个小时的航程还短，
    而她前一天刚说过"明天回国"。
    """


class TravelConfig(BaseModel):
    """放假出门的设定。假期长短决定走多远。"""

    short_trips: list[TravelSpot] = Field(default_factory=list)
    long_trips: list[TravelSpot] = Field(default_factory=list)
    short_probability: float = 0.5
    long_probability: float = 0.8
    short_min_days: int = 3
    short_max_days: int = 6
    long_min_days: int = 7
    long_max_days: int = 18
    short_activity_multiplier: float = 0.7
    """短途玩得紧凑，手机看得少。"""
    long_activity_multiplier: float = 0.9
    """长途住下来了，跟平时差不多。"""


class FallbackYear(BaseModel):
    """超出显式配置的年份，用这套典型日期推算，免得人物跑过一学年就没日历了。"""

    fall: tuple[str, str] = ("09-02", "12-20")
    spring: tuple[str, str] = ("01-13", "05-01")


class AcademicConfig(BaseModel):
    home_timezone: str = "America/New_York"
    periods: list[AcademicPeriod] = Field(default_factory=list)
    fallback: FallbackYear = Field(default_factory=FallbackYear)
    travel: TravelConfig = Field(default_factory=TravelConfig)


# ---------------------------------------------------------------------------
# 说话风格（机械约束在 style_guard 里执行）
# ---------------------------------------------------------------------------


class StyleConfig(BaseModel):
    """说话的机械约束。

    用**预算**而不是开关：偶尔一个 emoji、偶尔一个感叹号是年轻人的正常说话方式，
    满屏才不正常。把某个预算设成 0 就等于完全禁止。
    """

    max_parts: int = 2
    """一次最多发几条气泡。"""
    extra_part_probability: float = 0.15
    long_sentence_chars: int = 20
    """超过这个字数算"长句"。长句应当罕见，出现时说明是重话。"""
    long_sentence_budget: float = 0.08
    strip_trailing_period: bool = True
    """去掉句尾的句号。"""
    emoji_budget: float = 0.2
    """多大比例的气泡可以带 emoji。0 表示完全不用。"""
    max_emoji_per_part: int = 1
    exclamation_budget: float = 0.15
    allow_reactions: bool = True
    """允许给对方的消息加 Discord 表情反应。"""
    english_sentence_max_words: int = 5
    """纯英文句子最多几个词。短的没问题，整段英文就不像她了。0 表示不限制。"""
    english_words_allowed: list[str] = Field(default_factory=list)
    typing_chars_per_second: float = 2.6
    """打字速度，手机打字比键盘慢。"""
    typo_probability: float = 0.05
    typo_left_probability: float = 0.0
    """手滑之后有多大概率懒得改。每次都在几秒内改回来，本身也是一条规律。"""
    """一次回复里手滑打错一个字、发出去几秒后改回来的概率。

    打错的样子让模型写（拼音选错同音字最像手滑），多久一次由这里定。
    """
    status_text_probability: float = 0.0
    """多大比例的日子挂一个 Discord 自定义状态。0 就是从不挂。

    挂的是当天日程里 mood 那一句的原文。那句是写给她自己看的心情，
    写它的时候并不知道会被公开，所以默认关着。
    """


class Boundaries(BaseModel):
    never_say: list[str] = Field(default_factory=list)
    """出现即判违规的短语，由 style_guard 拦截并要求重写。

    **裸子串匹配，所以只放那些出现在任何位置都不对的话。**
    像"在吗""你好"这种只有开头才是寒暄的词要放 never_open_with，
    不然"现在吗""你好像""加油站"全会被误伤。
    """
    never_open_with: list[str] = Field(default_factory=list)
    """只有**开头**才算违规的短语，主要是寒暄。

    他们认识一年了，任何形式的打招呼都是聊天机器人的味道——但那只在开口那一下成立。
    "我现在吗？在图书馆"里的"在吗"、"你好像把参数记错了"里的"你好"、
    "周末去加油站碰到她"里的"加油"，都是正常说话。
    裸子串匹配会把这些全判违规，每一次误伤都白花一次重写的模型调用，
    而重写出来的句子往往还不如原来那句。
    """
    never_say_anywhere: list[str] = Field(default_factory=list)
    """出现在哪儿都算的套话："抱抱""摸摸头""早点休息"。

    这几句没有歧义，不会是别的词的一部分，照老办法按子串拦——
    按"说完了才算"的话，"抱抱你""早点休息呗"就溜过去了。
    """
    never_start_with: list[str] = Field(default_factory=list)
    """拿来**开头**就算违规的交代行踪："刚看到""还在睡""刚醒"。

    跟寒暄不一样，这种后面往往还接着话（"刚看到你消息"），所以只看前缀，
    不要求到此为止。放在句子中间是正常说话："我室友在睡觉""你说的那个我还没看到"。
    """
    never_start_with_whole: list[str] = Field(default_factory=list)
    """同上，但短语得到此为止："刚起 你呢""我刚起"算，"刚起了个头"不算。"""
    deflect_topics: list[str] = Field(default_factory=list)
    """被问到就岔开或者不答的话题。"""
    deflect_hint: str = ""
    """怎么岔开，给模型的提示。"""
    never_admit_ai: bool = True


# ---------------------------------------------------------------------------
# 话题模式：某些话题会让她换一副样子
# ---------------------------------------------------------------------------


class TopicMode(BaseModel):
    name: str
    triggers: list[str] = Field(default_factory=list)
    """命中任一关键词就进入这个模式（大小写不敏感）。"""
    priority: int = 0
    """命中多个模式时谁说了算，大的赢。

    有些话题压过别的：他一边说仓位一边提了句 deadline，那仍然是一次交易对话。
    """
    instruction: str = ""
    """进入模式后追加到上下文的指令。"""
    delay_multiplier: float = 1.0
    """这个话题下回复快慢的倍率，小于 1 表示更上心。"""
    include_ledger: bool = False
    """是否把对方过往的相关陈述（ledger）带进上下文，用来指出前后矛盾。"""
    ledger_kind: str = ""
    ledger_topic: str = ""
    """台账那一段在提示词里的小标题，比如"在交易上说过的话"。

    写死成交易的话，一条作息承诺会被摆进查账的框里，
    而作息那段人设又明确禁止说教——两层指令互相打架，输出会很怪。
    """
    follow_up_after_days: float = 0.0
    """记下来多久之后才值得追问一句。0 表示这一类不追问。

    **按事情本身的周期走。** 便利店的班次是几天的事，期末考是几周的事；
    都按同一个周期问，她就成了待办清单，不是人。
    """


# ---------------------------------------------------------------------------
# 主动消息
# ---------------------------------------------------------------------------


class ProactiveKind(BaseModel):
    name: str
    weight: float = 1.0
    """在今天要说的几种里被抽中的相对权重。"""
    note: str = ""
    """给模型的触发说明，描述这次主动是出于什么。"""
    hours: list[str] = Field(default_factory=list, description="限定时段，如 ['00:00-04:00']；空表示不限")
    photo_tags: list[str] = Field(default_factory=list)
    """本想按标签挑照片。**目前没接上**：挑照片只看时段和冷却。"""
    requires_photo: bool = False
    """为真时如果挑不到合适的照片，这次主动就取消；照片库空着时也不进当天的候选。"""
    text_optional: bool = False
    """本想控制能不能只发照片不配字。**目前没接上**：配不配字由模型自己定。"""
    min_days_since_last: float = 0.0
    """距离上次同类主动至少隔多少天。"""
    only_while_travelling: bool = False
    """只在出门在外的时候才成立。"""


class OpenerConfig(BaseModel):
    """她第一次上线时先说的那一句。

    **只在数据库还是空的时候发，一辈子一次。** 目的不是打招呼——
    他们已经认识一年了，打招呼才是露馅。目的是：不做的话，第一天是你发消息进去，
    然后按作息可能等三小时才有回音，功能完全正常，但看起来像坏了。
    这一句是"她在"的证据。

    所以最好的开场是**看不出是开场的开场**：内容上跟她第一百天说的话没有区别。
    """

    enabled: bool = True
    kind: str = "own_life"
    """借用哪一种主动消息的口吻。用现成的，别新造一种"开场白"语气。"""
    min_delay_minutes: float = 25.0
    """最早也要等这么久。``docker compose up`` 之后一分钟就冒出一句，那是程序开机的样子。"""
    max_delay_hours: float = 6.0
    """在这个窗口里按活跃度抽一个时刻。"""
    after_waking_minutes: tuple[float, float] = (40.0, 180.0)
    """窗口里她一直在睡（半夜装机器就是这样）时，退到起床之后这个区间里。"""
    fallback_search_hours: float = 30.0
    """往后找"她醒着"最多找这么久。跨一整个夜里也够。"""
    note: str = ""
    """给模型的指示。重点是把"打招呼"那条路堵死。"""


class LedgerTimedConfig(BaseModel):
    """他说了时间的事（"明早 9 点起来做完"），什么时候才能问。

    管的是"**不在他说的时间之前问**"，不是"避开他睡觉"——
    人设里写着她不迁就他的作息。
    """

    grace_hours: tuple[float, float] = (3.0, 10.0)
    """过了他说的时间，再等多久才算"该问了"。每条抽一次。

    一过九点就来问"跑完没"，那是闹钟不是人。
    """
    max_days_ahead: float = 30.0
    """他说的时间比这还远，就当没说，按类别周期走。多半是读错了钟。"""
    follow_up_guard_hours: float = 12.0
    """记下带时间的计划前后这么久里排的 follow_up，最早也要等到那件事该问的时候。

    模型不听话时会把他的计划塞进 follow_up，分钟数又是它心算的，
    普遍偏早——他凌晨一两点说"明早九点做"，她四五点就来问。
    分不清那个 follow_up 是不是在问这件事，所以宁可让她自己答应的事晚一点。
    """
    follow_up_hold_max_hours: float = 48.0
    """最多压多久。一件三周以后的事不该把她今晚答应的"查完告诉你"压上三周——
    同一时间只排一个 follow_up，压着的那个还会把她后来答应的全挤掉。"""


class SignOffConfig(BaseModel):
    """睡前说一声再走。

    聊着聊着到了她睡觉的点，一声不吭就下线、第二天才回，那是程序；
    真人会说一句"困了 我睡了"。但只在你们**还在聊**的时候：
    聊天早就停了的晚上，她直接睡，不会专门跑来道晚安。
    """

    enabled: bool = True
    watch_minutes: float = 90.0
    """他在她入睡前这么久之内说过话，才准备好睡前那一句。"""
    lead_minutes: tuple[float, float] = (2.0, 10.0)
    """在入睡前多久说。每晚抽一次。"""
    active_within_minutes: float = 30.0
    """到点时，**他**最后一次说话在这么久之内，才算"还在聊"。

    她自己说的不算：她主动开口他没回，那不是在聊，睡前再补一句就成了追发。
    """
    reply_within_minutes: float = 25.0
    """回他消息的时候离入睡不到这么久，就在这次回复里顺便说，不再另起一句。"""
    note: str = ""
    """单独说这一句时给模型的指示。"""
    reply_note: str = "你准备睡了。回完他这句，顺便跟他说一声你要睡了，一句就够。"
    """在回复里顺便说时的提示。"""
    said_note: str = "你刚跟他说过你要睡了。他又说了话，要回就回短的，别再道一次晚安。"
    """已经说过、他又说话了时的提示。"""


class Holiday(BaseModel):
    date: date
    name: str
    greeting: str = ""
    """那天怎么说，比如"新年快乐"。不写就是"{name}快乐"。"""
    local: bool = False
    """美国这边也过的节日（圣诞、元旦）。

    不标的就是国内的节日：她人在美国的话，那天照常上课，不放假。
    当初只写了一句"今天是国庆"，她就跟他说自己放一天假。
    """


class HolidayConfig(BaseModel):
    """节日那天她多半会主动说一声，顺口说说自己打算干嘛。

    不走每天"今天开不开口"那一关：节日本来就是会开口的日子。但也不是每个节日都说，
    说法每次不一样——死板的话就成了定时群发。
    """

    probability: float = 0.8
    note: str = "今天{name}。跟他说声{greeting}，再顺口说说你今天打算干嘛。"
    """给模型的指示。{name} 和 {greeting} 会换成那天的。"""
    days: list[Holiday] = Field(default_factory=list)
    """农历节日每年日子不一样，一年一年写上。"""
    home_timezones: list[str] = Field(default_factory=lambda: ["Asia/Shanghai"])
    """国内的时区。她人在这些时区时，国内的节日才是当地的节日。"""
    abroad_note: str = "{name}是国内的节日，你人不在国内，这边不放假，日子照你平常的过。"
    """国内的节日、她人不在国内时补的一句。{name} 会换成那天的。"""

    @field_validator("home_timezones")
    @classmethod
    def _check_tzs(cls, v: list[str]) -> list[str]:
        # 写错一个字的话永远对不上，国内的节日她在苏州也会说"这边不放假"
        for tz in v:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError as e:
                raise ValueError(f"未知时区 {tz!r}") from e
        return v


class ProactiveConfig(BaseModel):
    day_probability: float = 0.4
    """今天她到底会不会主动开口。

    先过这一关再谈说什么。少了这一步，每种主动各自掷骰子，
    合起来就变成几乎每天都要找你说话，很黏人。
    """
    second_message_probability: float = 0.3
    """开了口之后，今天再说第二件事的概率。"""
    max_per_day: int = 2
    unanswered_decay: float = 0.35
    """每有一次主动开场没被回应，下次概率乘这个数。"""
    max_unanswered_per_day: int = 1
    quiet_days_before_callback: float = 3.0
    """多久没说话之后允许提一句旧事。"""
    kinds: list[ProactiveKind] = Field(default_factory=list)
    opener: OpenerConfig = Field(default_factory=lambda: OpenerConfig())
    """第一次上线时的那一句。"""
    ledger_max_follow_ups: int = 2
    """同一条承诺最多追问几次。问过就得放下，不然她成了催办机器人。"""
    ledger_max_age_days: float = 45.0
    """多久以前的承诺就不再提了。三个月前那句话，正常人早就翻篇了。"""
    ledger_timed: LedgerTimedConfig = Field(default_factory=lambda: LedgerTimedConfig())
    """他说了时间的事，那之前不问。"""
    sign_off: SignOffConfig = Field(default_factory=lambda: SignOffConfig())
    """睡前说一声再走。"""
    holiday: HolidayConfig = Field(default_factory=lambda: HolidayConfig())


# ---------------------------------------------------------------------------
# 其余
# ---------------------------------------------------------------------------


class TimingConfig(BaseModel):
    max_delay_hours: float = 14.0
    urgent_multiplier: float = 0.7
    fatigue_after_minutes: float = 30.0
    """连续聊多久之后开始变慢、想收尾。"""
    hot_seconds: float = 180.0
    warm_seconds: float = 2700.0
    hot_reply_median_seconds: float = 75.0
    """正在聊的时候，隔多久回一句。

    没有秒回这回事：手机拿起来放下、打字、被别的事岔开，
    中位数一分多钟，三五分钟才回也很常见。
    """
    hot_reply_sigma: float = 0.8
    class_hot_notice_minutes: float = 5.0
    """聊进了她的课里，隔多久偷看一眼手机（中位数）。

    她嘴上说在上课，就不能还一分半钟回一句，跟没课的晚上一样快。
    """
    fatigue_reset_minutes: float = 15.0
    """两句之间停了这么久，"聊了多久"就从头算。停一会儿再聊，不该一接上就想收尾。"""
    backlog_after_wake_hours: float = 3.0
    """睡着时积压的消息，醒来之后最多拖多久。

    人睡醒第一件事就是看手机，积压一晚上的东西不会再压到晚上。
    没有这个上限的话，偶尔一次"先放着"叠上早上很低的活跃度，
    能把一条凌晨的消息拖到下午。
    """


class MemoryConfig(BaseModel):
    recent_messages: int = 40
    summarize_after: int = 60
    summary_max_chars: int = 800
    """她记着的那段聊天摘要最多多长。

    摘要每次整理都把新的并进去，原来没有上限：一个月下来越滚越长，
    每一次回复都带着它，越来越贵；长到整理时一口气写不完，输出被截断，
    记忆就卡住了。超了就让整理的时候把早的事压短，没聊完的和约好的优先留着。
    """
    fact_half_life_days: float = 45.0
    """一条事实多久淡一半。提起来会重新变清晰。"""
    fact_recall_threshold: float = 0.25
    """淡到这个程度以下就不再带进上下文，等于想不起来了。

    这是故意的。一个什么都记得的人，聊天就没意思了。
    """


class OwnerInfo(BaseModel):
    name: str = ""
    nickname: str = ""
    timezone: str = ""
    """对方所在时区。人物知道有时差，但不迁就。"""
    knows: str = ""
    """人物知道对方的哪些事。"""
    does_not_ask: str = ""
    """人物不知道也不问的事。"""

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        if v:
            try:
                ZoneInfo(v)
            except ZoneInfoNotFoundError as e:
                raise ValueError(f"未知时区 {v!r}") from e
        return v


class Persona(BaseModel):
    name: str
    english_name: str = ""
    age: int | None = None
    timezone: str = "America/New_York"
    language: str = "zh-CN"

    background: str = ""
    voice: str = ""
    """说话方式。用描述而不是规则清单。"""
    relationship: str = ""
    self_boundaries: str = ""
    """她自己的边界，写成描述。"""

    owner: OwnerInfo = Field(default_factory=OwnerInfo)
    academic: AcademicConfig = Field(default_factory=AcademicConfig)
    rhythm: RhythmConfig = Field(default_factory=RhythmConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    boundaries: Boundaries = Field(default_factory=Boundaries)
    modes: list[TopicMode] = Field(default_factory=list)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    seed: int = 0
    """人物的随机种子，影响每日抽签。改这个数会得到一整套不同的作息序列。"""

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"未知时区 {v!r}") from e
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def owner_tz(self) -> ZoneInfo | None:
        return ZoneInfo(self.owner.timezone) if self.owner.timezone else None

    def holiday_on(self, day: date) -> Holiday | None:
        return next((h for h in self.proactive.holiday.days if h.date == day), None)

    def mode_for(self, text: str) -> TopicMode | None:
        """文本命中哪个话题模式。

        先看 ``priority``，同级再看命中了几个触发词，还平就按 yaml 里的顺序。

        原来的规则是"取触发词最长的那个"，**而长度不是跨语种可比的量**：
        交易的触发词是"止损""加仓"这种两三个字的中文，课业里却有
        assignment、tutorial、deadline 这种八到十个字母的英文。
        于是"止损位我加仓了 assignment 还没交"会被判成课业——
        她看不见他在交易上说过的话，"你变硬、不给面子"那段人设整个丢掉，
        回复速度也从 0.6 倍慢回常速。人设里写着交易纪律是"唯一一件你不让步的事"，
        却能被任何一个更长的英文单词顶掉。
        """
        lowered = text.lower()
        best: tuple[int, int, TopicMode] | None = None
        for mode in self.modes:
            hits = sum(1 for t in mode.triggers if t.lower() in lowered)
            if not hits:
                continue
            score = (mode.priority, hits)
            if best is None or score > (best[0], best[1]):
                best = (mode.priority, hits, mode)
        return best[2] if best else None

    def placeholders(self) -> list[str]:
        """返回仍含【待填】占位的字段名，供 ``check`` 命令提示。"""
        out = []
        for name in ("background", "voice", "relationship", "self_boundaries"):
            if "【待填】" in getattr(self, name):
                out.append(name)
        if "【待填】" in self.owner.knows:
            out.append("owner.knows")
        return out


Severity = Literal["error", "warning"]


def validate_persona(persona: Persona) -> list[tuple[Severity, str]]:
    """返回一组问题，供 ``check`` 命令展示。空列表表示没问题。"""
    issues: list[tuple[Severity, str]] = []
    if not persona.rhythm.variants:
        issues.append(("warning", "rhythm.variants 为空：她每天的作息会一模一样，很容易看出是程序"))
    if not persona.proactive.kinds:
        issues.append(("warning", "proactive.kinds 为空：她永远不会主动说话"))
    total = sum(v.weight for v in persona.rhythm.variants)
    if persona.rhythm.variants and total <= 0:
        issues.append(("error", "rhythm.variants 的权重之和必须大于 0"))
    for name in persona.placeholders():
        level: Severity = "error" if name in ("background", "voice") else "warning"
        issues.append((level, f"{name} 还是【待填】占位"))

    # 台账这套东西有两种"配错了但完全没有症状"的方式，而这个项目里
    # "安静"和"正常"看起来一模一样，所以只能在这里当场喊出来。
    if any(m.ledger_kind for m in persona.modes) and not any(
        k.name == "ledger_check" for k in persona.proactive.kinds
    ):
        issues.append(
            ("warning", "有台账但 proactive.kinds 里没有 ledger_check：她永远不会回头问你做了没有")
        )
    for mode in persona.modes:
        if mode.ledger_kind and mode.follow_up_after_days <= 0:
            issues.append(
                ("warning", f"模式 {mode.name} 记台账但没设 follow_up_after_days：这一类只进不出")
            )
        if mode.ledger_kind and not mode.include_ledger:
            issues.append(
                ("warning", f"模式 {mode.name} 有 ledger_kind 但没打开 include_ledger：聊到时看不见旧账")
            )
    timed = persona.proactive.ledger_timed
    lo, hi = timed.grace_hours
    if lo < 1:
        issues.append(
            ("warning", "ledger_timed.grace_hours 下限不到 1 小时：一过他说的点就来问，那是闹钟不是人")
        )
    if hi < lo:
        issues.append(("error", "ledger_timed.grace_hours 要写成 [下限, 上限]"))
    if timed.max_days_ahead + hi / 24 >= persona.proactive.ledger_max_age_days:
        issues.append(
            (
                "warning",
                "ledger_timed.max_days_ahead 加上宽限超过了 ledger_max_age_days："
                "远一点的事还没到时间，就先被当成太老丢掉了",
            )
        )
    seen: dict[str, str] = {}
    for mode in persona.modes:
        for trigger in mode.triggers:
            if trigger in seen and seen[trigger] != mode.name:
                issues.append(
                    ("warning", f"触发词「{trigger}」同时属于 {seen[trigger]} 和 {mode.name}，命中时谁赢要看 priority")
                )
            seen[trigger] = mode.name
    return issues


def load_persona(path: str | Path) -> Persona:
    """从 yaml 文件加载人物设定。文件不存在或格式错误会抛异常。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到人设文件 {p}。请复制 persona/persona.example.yaml 为 {p} 并填写。"
        )
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Persona.model_validate(raw)
