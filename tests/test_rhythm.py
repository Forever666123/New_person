"""作息抽签的测试。重点是"没有规律"这件事本身要成立。"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from datetime import datetime, timedelta

import pytest

from newperson.persona import Persona
from newperson.rhythm import Rhythm

DAYS = 60


def days(persona: Persona, n: int = DAYS):
    start = datetime(2026, 9, 14, tzinfo=persona.tz).date()
    return [start + timedelta(days=i) for i in range(n)]


def test_same_day_always_samples_the_same(rhythm: Rhythm, persona: Persona) -> None:
    """同一天反复查必须一致，否则重启后人物的作息会变。"""
    day = days(persona)[3]
    first = rhythm.for_day(day)
    fresh = Rhythm(persona.rhythm, persona.tz, persona.seed)
    assert fresh.for_day(day) == first


def test_different_seed_gives_a_different_life(persona: Persona) -> None:
    a = Rhythm(persona.rhythm, persona.tz, persona.seed)
    b = Rhythm(persona.rhythm, persona.tz, persona.seed + 1)
    day = days(persona)[0]
    assert a.for_day(day).sleep_start != b.for_day(day).sleep_start


def test_wake_times_are_spread_out(rhythm: Rhythm, persona: Persona) -> None:
    """起床时间必须分散。全都落在同一个小时就说明又变成时刻表了。"""
    hours = {rhythm.wake_of(d).hour for d in days(persona)}
    assert len(hours) >= 4, f"起床时间只落在 {sorted(hours)}，太规律"


def test_sleep_duration_stays_sane(rhythm: Rhythm, persona: Persona) -> None:
    for d in days(persona):
        night = rhythm.for_day(d).sleep_start
        morning = rhythm.for_day(d + timedelta(days=1)).wake
        hours = (morning - night).total_seconds() / 3600
        assert persona.rhythm.sleep.min_hours - 0.01 <= hours <= persona.rhythm.sleep.max_hours + 0.01


def test_late_night_means_late_morning(rhythm: Rhythm, persona: Persona) -> None:
    """睡得晚就起得晚。独立采样会抽出"四点睡七点起"，那不像人。"""
    pairs = []
    for d in days(persona, 120):
        night = rhythm.for_day(d).sleep_start
        morning = rhythm.for_day(d + timedelta(days=1)).wake
        pairs.append((night.timestamp(), morning.timestamp()))
    slept = [a for a, _ in pairs]
    woke = [b for _, b in pairs]
    assert statistics.correlation(slept, woke) > 0.5


def test_a_phase_lasts_several_days(rhythm: Rhythm, persona: Persona) -> None:
    """阶段要成片，不能一天一换，否则"忙"就没有意义了。"""
    names = [rhythm.for_day(d).phase for d in days(persona, 120)]
    runs, current = [], 1
    for a, b in zip(names, names[1:], strict=False):
        if a == b:
            current += 1
        else:
            runs.append(current)
            current = 1
    runs.append(current)
    assert statistics.fmean(runs) >= 4, f"阶段平均只持续 {statistics.fmean(runs):.1f} 天"


def test_every_phase_shows_up(rhythm: Rhythm, persona: Persona) -> None:
    seen = {rhythm.for_day(d).phase for d in days(persona, 400)}
    assert seen == {p.name for p in persona.rhythm.phases}


def test_a_busy_phase_lowers_activity(rhythm: Rhythm, persona: Persona) -> None:
    """赶 due 的那几天，看手机的活跃度整体要低下来。"""
    busy = [rhythm.for_day(d) for d in days(persona, 300) if rhythm.for_day(d).phase == "赶due"]
    calm = [rhythm.for_day(d) for d in days(persona, 300) if rhythm.for_day(d).phase == "平常"]
    assert busy and calm
    assert statistics.fmean(d.activity_multiplier for d in busy) < statistics.fmean(
        d.activity_multiplier for d in calm
    )


def test_no_single_day_shuts_her_off_entirely(rhythm: Rhythm, persona: Persona) -> None:
    """没有"今天完全不理人"这种开关。最差的日子也还是会看手机。"""
    for d in days(persona, 300):
        daily = rhythm.for_day(d)
        assert daily.activity_multiplier > 0.1
        assert daily.engage_probability > 0.1


def test_all_variants_show_up(rhythm: Rhythm, persona: Persona) -> None:
    seen = {rhythm.for_day(d).variant for d in days(persona, 300)}
    configured = {v.name for v in persona.rhythm.variants}
    assert seen == configured, f"没抽到的变体：{configured - seen}"


def test_asleep_at_night_awake_in_the_evening(rhythm: Rhythm, persona: Persona) -> None:
    """凌晨四点基本在睡，晚上八点基本醒着。"""
    asleep = sum(
        rhythm.is_sleeping(datetime.combine(d, datetime.min.time(), persona.tz).replace(hour=4))
        for d in days(persona)
    )
    awake = sum(
        not rhythm.is_sleeping(datetime.combine(d, datetime.min.time(), persona.tz).replace(hour=20))
        for d in days(persona)
    )
    assert asleep >= DAYS * 0.9
    assert awake >= DAYS * 0.9


def test_sleep_windows_do_not_overlap(rhythm: Rhythm, persona: Persona) -> None:
    """相邻两觉不能重叠，否则 next_wake 之类会算错。"""
    ds = days(persona)
    for a, b in zip(ds, ds[1:], strict=False):
        assert rhythm.for_day(a).sleep_start < rhythm.for_day(b).wake
        assert rhythm.for_day(b).wake < rhythm.for_day(b).sleep_start


def test_activity_is_zero_while_asleep(rhythm: Rhythm, persona: Persona) -> None:
    t = datetime(2026, 9, 14, 12, tzinfo=persona.tz)
    for _ in range(24 * 14):
        if rhythm.is_sleeping(t):
            assert rhythm.activity_at(t) == 0.0
        t += timedelta(hours=1)


def test_evening_is_more_active_than_class(rhythm: Rhythm, persona: Persona) -> None:
    """晚上最闲，上课时最不看手机。"""
    monday = datetime(2026, 9, 14, tzinfo=persona.tz)
    in_class = monday.replace(hour=14)
    evening = monday.replace(hour=20)
    if rhythm.class_containing(in_class):
        assert rhythm.activity_at(in_class) < rhythm.activity_at(evening)


def test_glances_never_land_in_sleep(rhythm: Rhythm, persona: Persona, rng: random.Random) -> None:
    t = datetime(2026, 9, 14, 12, tzinfo=persona.tz)
    end = t + timedelta(days=10)
    for moment in rhythm.glances_between(t, end, rng):
        assert not rhythm.is_sleeping(moment), f"{moment} 在睡觉时还看手机"


def test_glance_gaps_are_irregular(rhythm: Rhythm, persona: Persona, rng: random.Random) -> None:
    """看手机的间隔要散开，不能有一个"标准间隔"反复出现。"""
    t = datetime(2026, 9, 14, 12, tzinfo=persona.tz)
    moments = rhythm.glances_between(t, t + timedelta(days=5), rng)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(moments, moments[1:], strict=False)]
    assert len(gaps) > 50

    rounded = Counter(round(g) for g in gaps)
    most_common_share = rounded.most_common(1)[0][1] / len(gaps)
    assert most_common_share < 0.2, f"间隔 {rounded.most_common(1)} 出现得太频繁，像是固定周期"

    mean = statistics.fmean(gaps)
    assert statistics.stdev(gaps) / mean > 0.4, "间隔的离散度太小，看起来像定时任务"


def test_night_message_waits_until_morning(rhythm: Rhythm, persona: Persona, rng: random.Random) -> None:
    """凌晨三点发的消息，要等到她醒了才可能被看到。"""
    for offset in range(20):
        t = datetime(2026, 9, 15, 3, 30, tzinfo=persona.tz) + timedelta(days=offset)
        if not rhythm.is_sleeping(t):
            continue
        glance = rhythm.next_glance_after(t, rng)
        assert glance >= rhythm.next_wake_after(t)


@pytest.mark.parametrize("hour", [4, 5])
def test_state_at_reports_sleeping_at_night(rhythm: Rhythm, persona: Persona, hour: int) -> None:
    hits = 0
    for d in days(persona, 20):
        snap = rhythm.state_at(datetime.combine(d, datetime.min.time(), persona.tz).replace(hour=hour))
        if snap.state == "sleeping":
            hits += 1
            assert snap.until > snap.at
            assert snap.activity == 0.0
    assert hits >= 15
