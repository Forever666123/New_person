"""回复时机的测试。

这些断言是"像不像人"的底线，改参数的时候它们会先叫。
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime, timedelta

import pytest

from newperson.attention import MIN_DELAY_SECONDS, AttentionPolicy, extract_features, heat_of
from newperson.persona import Persona
from newperson.rhythm import Rhythm


@pytest.fixture
def policy(persona: Persona, rhythm: Rhythm) -> AttentionPolicy:
    return AttentionPolicy(persona, rhythm)


def evening(day_offset: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 12, 19, 0, tzinfo=ZoneInfo("America/New_York")) + timedelta(
        days=day_offset
    )


def delays(policy: AttentionPolicy, persona: Persona, heat: str, samples: int = 300) -> list[float]:
    out = []
    for i in range(samples):
        now = evening(i % 60).replace(hour=[10, 14, 19, 21, 23][i % 5])
        if policy.rhythm.is_sleeping(now):
            continue
        features = extract_features(["在吗"], persona)
        d = policy.plan_reply(now, heat, features, now, random.Random(i))
        out.append((d.reply_at - now).total_seconds())
    return sorted(out)


# -- 热度 -------------------------------------------------------------------


def test_heat_buckets(persona: Persona) -> None:
    now = evening()
    hot_s, warm_s = persona.timing.hot_seconds, persona.timing.warm_seconds
    assert heat_of(now, now - timedelta(seconds=30), None, hot_s, warm_s) == "hot"
    assert heat_of(now, now - timedelta(minutes=20), None, hot_s, warm_s) == "warm"
    assert heat_of(now, now - timedelta(hours=5), None, hot_s, warm_s) == "cold"
    assert heat_of(now, None, None, hot_s, warm_s) == "cold"


# -- 没有秒回 ----------------------------------------------------------------


def test_she_never_replies_instantly(policy: AttentionPolicy, persona: Persona) -> None:
    """就算正在聊，也不会一秒钟之内就回。"""
    fastest = delays(policy, persona, "hot")[0]
    assert fastest >= MIN_DELAY_SECONDS


def test_even_mid_conversation_takes_about_a_minute(
    policy: AttentionPolicy, persona: Persona
) -> None:
    """手机拿起来放下、打字、被岔开，中位数在一分钟上下。"""
    ds = delays(policy, persona, "hot")
    assert 30 <= statistics.median(ds) <= 180


def test_a_few_minutes_is_common(policy: AttentionPolicy, persona: Persona) -> None:
    """三五分钟才回一句，这种情况要经常出现，不能是罕见尾巴。"""
    ds = delays(policy, persona, "warm")
    in_range = [d for d in ds if 120 <= d <= 900]
    assert len(in_range) / len(ds) > 0.25


def test_colder_means_slower(policy: AttentionPolicy, persona: Persona) -> None:
    hot = statistics.median(delays(policy, persona, "hot"))
    warm = statistics.median(delays(policy, persona, "warm"))
    cold = statistics.median(delays(policy, persona, "cold"))
    assert hot < warm < cold


def test_delays_are_spread_not_clustered(policy: AttentionPolicy, persona: Persona) -> None:
    """不能所有回复都挤在同一个区间，那样看得出是程序。"""
    ds = delays(policy, persona, "cold")
    assert ds[-1] / max(ds[0], 1) > 10


# -- 作息 -------------------------------------------------------------------


def test_a_message_at_night_waits_until_morning(
    policy: AttentionPolicy, persona: Persona, rhythm: Rhythm
) -> None:
    checked = 0
    for offset in range(30):
        now = evening(offset).replace(hour=4)
        if not rhythm.is_sleeping(now):
            continue
        checked += 1
        d = policy.plan_reply(now, "cold", extract_features(["睡了吗"], persona), now, random.Random(offset))
        assert d.reply_at >= rhythm.next_wake_after(now)
    assert checked >= 20


def test_never_replies_while_asleep(policy: AttentionPolicy, persona: Persona, rhythm: Rhythm) -> None:
    for i in range(200):
        now = evening(i % 60).replace(hour=[1, 6, 11, 16, 22][i % 5])
        d = policy.plan_reply(now, "cold", extract_features(["在吗"], persona), now, random.Random(i))
        assert not rhythm.is_sleeping(d.reply_at)


def test_class_time_slows_her_down(policy: AttentionPolicy, persona: Persona, rhythm: Rhythm) -> None:
    """上课的时候只能偷偷回，比空闲时慢得多。"""
    in_class = free = None
    for offset in range(40):
        now = evening(offset).replace(hour=19)
        snap = rhythm.state_at(now)
        d = policy.plan_reply(now, "cold", extract_features(["在吗"], persona), now, random.Random(offset))
        gap = (d.reply_at - now).total_seconds()
        if snap.state == "busy" and in_class is None:
            in_class = gap
        elif snap.state == "free" and free is None:
            free = gap
    assert in_class is not None and free is not None
    assert in_class > free


# -- 内容特征 ----------------------------------------------------------------


def test_urgent_messages_get_a_faster_reply(policy: AttentionPolicy, persona: Persona) -> None:
    now = evening()
    calm, rush = [], []
    for i in range(120):
        rng_a, rng_b = random.Random(i), random.Random(i)
        calm.append(
            (policy.plan_reply(now, "hot", extract_features(["刚看完那个"], persona), now, rng_a).reply_at - now).total_seconds()
        )
        rush.append(
            (policy.plan_reply(now, "hot", extract_features(["急 帮我看下"], persona), now, rng_b).reply_at - now).total_seconds()
        )
    assert statistics.median(rush) < statistics.median(calm)


def test_trading_talk_gets_her_attention(policy: AttentionPolicy, persona: Persona) -> None:
    """交易是她唯一较真的事，回得更快。"""
    now = evening()
    small_talk, trading = [], []
    for i in range(120):
        small_talk.append(
            (policy.plan_reply(now, "hot", extract_features(["今天下雨"], persona), now, random.Random(i)).reply_at - now).total_seconds()
        )
        trading.append(
            (policy.plan_reply(now, "hot", extract_features(["我今天加仓了"], persona), now, random.Random(i)).reply_at - now).total_seconds()
        )
    assert statistics.median(trading) < statistics.median(small_talk)


def test_mode_detection(persona: Persona) -> None:
    assert extract_features(["止损没设"], persona).mode == "trading"
    assert extract_features(["今天好烦"], persona).mode == "低气压"
    assert extract_features(["吃了吗"], persona).mode is None


def test_a_long_message_takes_longer_to_read(policy: AttentionPolicy, persona: Persona) -> None:
    now = evening()
    short = policy.plan_reply(now, "hot", extract_features(["嗯"], persona), now, random.Random(3))
    long = policy.plan_reply(now, "hot", extract_features(["字" * 600], persona), now, random.Random(3))
    assert long.reply_at > short.reply_at


# -- 连发与疲劳 --------------------------------------------------------------


def test_rapid_fire_does_not_starve_the_reply(policy: AttentionPolicy, persona: Persona) -> None:
    """对方一直连着发，不能永远等下去，那看起来像在生闷气。"""
    now = evening()
    reply_at = now + timedelta(seconds=30)
    for i in range(50):
        now += timedelta(seconds=10)
        reply_at = policy.merge_pending(reply_at, now, "hot", random.Random(i))
    assert (reply_at - now).total_seconds() < 200


def test_long_sessions_slow_down(policy: AttentionPolicy, persona: Persona) -> None:
    """聊久了会累，回得慢下来，然后自然收尾。"""
    now = evening()
    fresh, tired = [], []
    for i in range(80):
        fresh.append(
            (policy.plan_reply(now, "hot", extract_features(["嗯"], persona), now, random.Random(i)).reply_at - now).total_seconds()
        )
        tired.append(
            (policy.plan_reply(now, "hot", extract_features(["嗯"], persona), now, random.Random(i), session_started_at=now - timedelta(minutes=90)).reply_at - now).total_seconds()
        )
    assert statistics.median(tired) > statistics.median(fresh)


# -- 给模型的提示 ------------------------------------------------------------


def test_a_long_gap_tells_her_not_to_apologise(policy: AttentionPolicy, persona: Persona, rhythm: Rhythm) -> None:
    """隔了很久回来，她直接接着说，不解释去哪了。"""
    now = evening().replace(hour=4)
    assert rhythm.is_sleeping(now)
    d = policy.plan_reply(now, "cold", extract_features(["在吗"], persona), now, random.Random(1))
    assert any("别解释" in h for h in d.hints)


def test_deferred_messages_are_not_mentioned(policy: AttentionPolicy, persona: Persona) -> None:
    """她早看到了只是没回，这件事不该说出来。"""
    now = evening()
    for i in range(200):
        d = policy.plan_reply(now, "cold", extract_features(["在吗"], persona), now, random.Random(i))
        if d.defers:
            assert any("别提这件事" in h for h in d.hints)
            return
    pytest.fail("两百次采样一次都没先放着，engage_probability 是不是设成 1 了")


def test_travel_shows_up_in_the_hints(policy: AttentionPolicy, persona: Persona, rhythm: Rhythm) -> None:
    from zoneinfo import ZoneInfo

    for offset in range(120):
        now = datetime(2026, 12, 21, 20, 0, tzinfo=ZoneInfo("America/New_York")) + timedelta(days=offset)
        if rhythm.is_sleeping(now) or not rhythm.daily_for(now).trip:
            continue
        d = policy.plan_reply(now, "hot", extract_features(["在干嘛"], persona), now, random.Random(offset))
        assert any("人在" in h for h in d.hints)
        return
    pytest.fail("整个寒假都没出过门")


# -- 调试开关 ----------------------------------------------------------------


def test_delay_scale_speeds_everything_up(persona: Persona, rhythm: Rhythm) -> None:
    now = evening()
    real = AttentionPolicy(persona, rhythm, delay_scale=1.0)
    fast = AttentionPolicy(persona, rhythm, delay_scale=0.01)
    features = extract_features(["在吗"], persona)
    slow_gap = (real.plan_reply(now, "cold", features, now, random.Random(9)).reply_at - now).total_seconds()
    fast_gap = (fast.plan_reply(now, "cold", features, now, random.Random(9)).reply_at - now).total_seconds()
    assert fast_gap < slow_gap / 50


def test_typing_takes_time_but_not_forever(policy: AttentionPolicy) -> None:
    rng = random.Random(2)
    assert 1.5 <= policy.typing_duration("嗯", rng) <= 40
    assert policy.typing_duration("字" * 500, rng) == pytest.approx(40, abs=0.1)
    assert policy.typing_duration("今天下雪了", rng) > policy.typing_duration("嗯", rng)


def test_an_overnight_backlog_is_handled_soon_after_waking(
    policy: AttentionPolicy, persona: Persona, rhythm: Rhythm
) -> None:
    """睡着时积压的消息，醒来之后不会再拖到下午。

    人睡醒第一件事就是看手机。早先这里完全不封顶，一次"先放着"
    叠上早上很低的活跃度，能把一条凌晨的消息拖到晚上六点。
    """
    limit = persona.timing.backlog_after_wake_hours
    checked = 0
    for offset in range(40):
        sent = evening(offset).replace(hour=4, minute=47)
        if not rhythm.is_sleeping(sent):
            continue
        checked += 1
        wake = rhythm.next_wake_after(sent)
        for seed in range(12):
            d = policy.plan_reply(
                sent, "cold", extract_features(["在吗"], persona), sent, random.Random(seed)
            )
            gap = (d.reply_at - wake).total_seconds() / 3600
            assert 0 <= gap <= limit + 0.01, f"起床后 {gap:.1f} 小时才回"
    assert checked >= 20


def test_she_usually_deals_with_the_backlog_on_the_first_look(
    policy: AttentionPolicy, persona: Persona, rhythm: Rhythm
) -> None:
    """攒了一晚上的消息，醒来那一眼基本都会处理，不会说"待会儿再说"。"""
    sent = None
    for offset in range(30):
        candidate = evening(offset).replace(hour=4)
        if rhythm.is_sleeping(candidate):
            sent = candidate
            break
    assert sent is not None

    deferred = sum(
        1
        for seed in range(200)
        if policy.plan_reply(
            sent, "cold", extract_features(["在吗"], persona), sent, random.Random(seed)
        ).defers
    )
    assert deferred / 200 < 0.25, "醒来看到积压还老是先放着，不像人"


def test_a_daytime_message_can_still_wait(
    policy: AttentionPolicy, persona: Persona, rhythm: Rhythm
) -> None:
    """白天收到的消息该能拖就拖，睡醒那个上限不该管到白天。"""
    long_waits = 0
    for offset in range(60):
        now = evening(offset).replace(hour=14)
        if rhythm.is_sleeping(now):
            continue
        d = policy.plan_reply(
            now, "cold", extract_features(["在吗"], persona), now, random.Random(offset)
        )
        if (d.reply_at - now).total_seconds() > 3600:
            long_waits += 1
    assert long_waits > 0, "白天一次超过一小时的延迟都没有，反而不像人"


def test_the_hints_ignore_the_debug_speedup(persona: Persona, rhythm: Rhythm) -> None:
    """DELAY_SCALE 是调试用的加速器，不该改变她说什么。

    压缩之后"隔了六小时"会变成"隔了十几秒"，她就不知道自己该不该
    提这段时间的事了，于是冒出"还在睡 没看到"这种解释行踪的话。
    """
    real = AttentionPolicy(persona, rhythm, delay_scale=1.0)
    fast = AttentionPolicy(persona, rhythm, delay_scale=0.01)
    now = evening().replace(hour=4)
    if not rhythm.is_sleeping(now):
        now = next(
            evening(d).replace(hour=4) for d in range(14) if rhythm.is_sleeping(evening(d).replace(hour=4))
        )
    features = extract_features(["在吗"], persona)
    a = real.plan_reply(now, "cold", features, now, random.Random(5))
    b = fast.plan_reply(now, "cold", features, now, random.Random(5))
    assert a.hints == b.hints
    assert any("别解释" in h for h in b.hints)


def test_even_a_short_gap_says_do_not_explain(
    policy: AttentionPolicy, persona: Persona, rhythm: Rhythm
) -> None:
    """隔十几分钟回来也不该解释去哪了，不用等到隔了一个半小时。"""
    found = False
    for offset in range(30):
        now = evening(offset).replace(hour=21)
        if rhythm.is_sleeping(now):
            continue
        d = policy.plan_reply(
            now, "cold", extract_features(["在吗"], persona), now, random.Random(offset)
        )
        if (d.reply_at - now).total_seconds() / 60 > 10:
            assert any("别解释" in h for h in d.hints)
            found = True
    assert found
