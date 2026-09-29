"""作息抽签的测试。重点是"没有规律"这件事本身要成立。"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest

from newperson.calendar import AcademicCalendar
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
    fresh = Rhythm(persona.rhythm, persona.tz, persona.seed, AcademicCalendar(persona.academic, persona.seed))
    assert fresh.for_day(day) == first


def test_different_seed_gives_a_different_life(persona: Persona) -> None:
    a = Rhythm(persona.rhythm, persona.tz, persona.seed, AcademicCalendar(persona.academic, persona.seed))
    b = Rhythm(persona.rhythm, persona.tz, persona.seed + 1, AcademicCalendar(persona.academic, persona.seed + 1))
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


def test_no_deadline_crunch_during_a_break(rhythm: Rhythm, persona: Persona) -> None:
    """放假的时候不该有"赶 due"这种阶段。"""
    for d in days(persona, 300):
        daily = rhythm.for_day(d)
        if daily.period_kind in ("break", "summer"):
            assert daily.phase != "赶due"


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


def test_daylight_saving_does_not_break_anything(rhythm: Rhythm, persona: Persona) -> None:
    """美国 11 月回拨（一点出现两次）、三月前拨（两点不存在）。

    这两天正好可能落在春假附近，作息算错会让她凭空多睡或者少睡一小时。
    """
    for start in (date(2026, 10, 31), date(2027, 3, 13)):
        for offset in range(3):
            d = start + timedelta(days=offset)
            daily = rhythm.for_day(d)
            morning = rhythm.for_day(d + timedelta(days=1)).wake
            hours = (morning - daily.sleep_start).total_seconds() / 3600
            assert persona.rhythm.sleep.min_hours - 0.01 <= hours <= persona.rhythm.sleep.max_hours + 0.01


def test_a_whole_year_never_raises(rhythm: Rhythm, persona: Persona, rng: random.Random) -> None:
    """跑一整年，每隔六小时查一次，不能有任何一处炸掉。"""
    t = datetime(2026, 9, 1, 12, tzinfo=persona.tz)
    for _ in range(365 * 4):
        snapshot = rhythm.state_at(t)
        assert snapshot.until > snapshot.at
        assert rhythm.next_glance_after(t, rng) > t
        t += timedelta(hours=6)


def test_times_are_shown_in_the_timezone_she_is_in(rhythm: Rhythm, persona: Persona) -> None:
    """出门在外的那几天，起床入睡要按当地时间表示，否则日志读起来自相矛盾。"""
    checked = 0
    for i in range(400):
        d = date(2026, 9, 1) + timedelta(days=i)
        daily = rhythm.for_day(d)
        if daily.trip is None:
            continue
        checked += 1
        expected = str(rhythm.tz_for(d))
        assert str(daily.wake.tzinfo) == expected
        assert str(daily.sleep_start.tzinfo) == expected
    assert checked > 0, "一整年都没出过门"


def test_engaging_varies_within_a_single_day(rhythm: Rhythm, persona: Persona) -> None:
    """"当场回的概率"不能是每天一个写死的数字。

    上课时偷瞄一眼、刚醒还躺着、快睡着了，这些时候看到了更容易先放着。
    """
    day = date(2026, 10, 13)
    values = set()
    for hour in range(24):
        t = datetime.combine(day, datetime.min.time(), persona.tz).replace(hour=hour)
        if rhythm.is_sleeping(t):
            continue
        values.add(round(rhythm.engage_probability_at(t), 2))
    assert len(values) >= 3, f"一整天只有 {values} 这几个值，等于写死了"


def test_being_busy_makes_her_more_likely_to_leave_it(
    rhythm: Rhythm, persona: Persona
) -> None:
    """上课的时候看到了更可能先放着，晚上有空就当场回了。"""
    for offset in range(30):
        day = date(2026, 10, 13) + timedelta(days=offset)
        daily = rhythm.for_day(day)
        if not daily.classes:
            continue
        in_class = daily.classes[0].start + timedelta(minutes=20)
        evening = datetime.combine(day, datetime.min.time(), persona.tz).replace(hour=21)
        if rhythm.is_sleeping(evening) or rhythm.class_containing(evening):
            continue
        assert rhythm.engage_probability_at(in_class) < rhythm.engage_probability_at(evening)
        return
    pytest.fail("三十天里没找到一天既有课又有空闲的晚上")


def test_a_bad_mood_lowers_it_all_day(rhythm: Rhythm, persona: Persona) -> None:
    """心情差那天，任何时刻的概率都比普通日子低。"""
    bad = good = None
    for offset in range(300):
        day = date(2026, 10, 13) + timedelta(days=offset)
        daily = rhythm.for_day(day)
        noon = datetime.combine(day, datetime.min.time(), persona.tz).replace(hour=21)
        if rhythm.is_sleeping(noon):
            continue
        if daily.variant == "心情差" and bad is None:
            bad = rhythm.engage_probability_at(noon)
        elif daily.variant == "普通" and good is None:
            good = rhythm.engage_probability_at(noon)
    assert bad is not None and good is not None
    assert bad < good


def test_force_awake_is_only_for_debugging(persona: Persona, calendar) -> None:
    """第一次跑起来常常是半夜，她正在睡觉，发什么都要等到早上，看不到效果。

    这个开关让她当作醒着，但只影响作息判定，不改她说话的方式。
    """
    normal = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    debug = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar, force_awake=True)

    night = next(
        t
        for t in (
            datetime(2026, 10, 13, 4, 0, tzinfo=persona.tz) + timedelta(days=d) for d in range(14)
        )
        if normal.is_sleeping(t)
    )
    assert not debug.is_sleeping(night)
    assert debug.activity_at(night) > 0.5
    assert normal.activity_at(night) == 0.0

    # 白天两者应该一致，开关不该改变正常时段的行为
    noon = datetime(2026, 10, 13, 21, 0, tzinfo=persona.tz)
    if not normal.is_sleeping(noon):
        assert normal.for_day(noon.date()).variant == debug.for_day(noon.date()).variant


def test_force_awake_also_changes_what_presence_sees(persona: Persona, calendar) -> None:
    """调试开关只改时机不改状态显示的话，presence 会拿着"睡着"把她设成隐身。

    看起来就是"我一发消息她头像就灰了"，但她其实在正常排队回复。
    """
    debug = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar, force_awake=True)
    normal = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)

    night = next(
        t
        for t in (
            datetime(2026, 10, 13, 4, 0, tzinfo=persona.tz) + timedelta(days=d) for d in range(14)
        )
        if normal.is_sleeping(t)
    )
    assert normal.state_at(night).state == "sleeping"
    assert debug.state_at(night).state != "sleeping"
    assert debug.state_at(night).activity > 0


def test_vacation_does_not_flatten_the_phase_mix(persona: Persona) -> None:
    """放假抽到「赶due」要在剩下的阶段里按权重重抽，不是退回第一个。

    退回第一个（也就是权重最大的「平常」）会把「赶due」让出来的那份概率
    整个送给平常，「松」一天也涨不到：她的假期于是和上课期一样平淡。
    这个偏差是静默的，去调 yaml 里的权重也纠正不过来。

    占比不是按权重算的，是按「权重 × 平均段长」：
    平常 55×9.5、赶due 28×6.5、松 17×5，所以上课期的松本来就只占一成。
    假期把赶due 那两成按 55:17 分掉，松该涨到一成六。
    一个种子的两年里只有二三十段，单看方差极大（实测 5%~27%），
    所以这里跨种子合起来数。
    """
    start = date(2026, 9, 2)
    counts: Counter[str] = Counter()
    for seed in range(1, 25):
        calendar = AcademicCalendar(persona.academic, seed)
        rhythm = Rhythm(persona.rhythm, persona.tz, seed, calendar)
        for offset in range(730):
            day = start + timedelta(days=offset)
            # 期末周没有正课，但不是放假，见下一条
            if calendar.period_for(day).kind not in ("in_session", "finals"):
                counts[rhythm.phase_for(day).name] += 1

    total = sum(counts.values())
    assert total > 2000, "样本太少，这条测试说明不了什么"

    for phase in persona.rhythm.phases:
        if phase.only_in_session:
            assert phase.name not in counts, f"假期里不该出现「{phase.name}」"

    easy = counts["松"] / total
    # 退回 phases[0] 的话这个数会停在上课期的水平（约一成）。
    assert easy > 0.13, f"假期里「松」只占 {easy:.0%}，赶due 让出来的概率没分到它头上"
    assert easy < 0.21, f"假期里「松」占到 {easy:.0%}，重抽的权重不对"


# -- 夏令时结束那一夜 ----------------------------------------------------------
#
# 纽约 2026-11-01 06:00Z 把钟拨回一小时，01:00–02:00 过两遍。同一个 tzinfo 的
# 两个时刻比大小时 Python 只看墙钟、不看 fold，于是第二个 01:10 被当成早于
# 第一个 01:30 的入睡时刻——睡着半小时的她在代码里又醒了，几十秒就回消息。

FALL_BACK = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def _minutes(start: datetime, end: datetime, tz):
    t = start
    while t < end:
        yield t.astimezone(tz)
        t += timedelta(minutes=1)


def test_she_stays_asleep_through_the_repeated_hour(persona: Persona) -> None:
    """那一觉从入睡到起床，每一分钟都在睡——包括过第二遍的那一小时。

    2026-10-31 是周六，周末晚睡会把那晚的入睡推到回拨之后，前提就不成立了。
    这条守的是拨钟，不是周末，所以把周末偏移关掉。
    """
    from newperson.calendar import AcademicCalendar
    from newperson.persona import WeekendConfig

    config = persona.rhythm.model_copy(update={"weekend": WeekendConfig()})
    rhythm = Rhythm(config, persona.tz, persona.seed, AcademicCalendar(persona.academic, persona.seed))
    night = rhythm.for_day(date(2026, 10, 31))
    wake = rhythm.for_day(date(2026, 11, 1)).wake
    assert night.sleep_start.astimezone(UTC) < FALL_BACK + timedelta(hours=1) < wake.astimezone(UTC), (
        "前提不成立：这个种子那一觉没跨过重复的那一小时"
    )
    awake = [
        t for t in _minutes(night.sleep_start.astimezone(UTC), wake.astimezone(UTC), persona.tz)
        if not rhythm.is_sleeping(t)
    ]
    assert not awake, f"睡着的时候被判成醒着：{awake[0].isoformat()} 起共 {len(awake)} 分钟"


def test_she_never_replies_while_asleep_through_the_repeated_hour(
    rhythm: Rhythm, persona: Persona
) -> None:
    """重复的那一小时里他发的消息，一条都不能在她睡着的时候回出去。"""
    from newperson.attention import AttentionPolicy, extract_features

    policy = AttentionPolicy(persona, rhythm, 1.0)
    features = extract_features(["在吗"], persona)
    # 真相按 UTC 算，不借 is_sleeping——被测的正是它。
    asleep_from = rhythm.for_day(date(2026, 10, 31)).sleep_start.astimezone(UTC)
    asleep_until = rhythm.for_day(date(2026, 11, 1)).wake.astimezone(UTC)
    for now in _minutes(FALL_BACK, FALL_BACK + timedelta(minutes=31), persona.tz):
        for seed in range(5):
            decision = policy.plan_reply(now, "cold", features, now, random.Random(seed))
            reply = decision.reply_at.astimezone(UTC)
            assert reply > now.astimezone(UTC), f"{now.isoformat()} 的消息排到了过去"
            assert not asleep_from <= reply < asleep_until, (
                f"{now.isoformat()} 的消息在她睡着时回了：{decision.reply_at.isoformat()}"
            )


def test_glances_move_forward_in_real_time(rhythm: Rhythm, persona: Persona) -> None:
    """下一次看手机永远在真实时间上往后，不会跨过拨钟倒退一小时。"""
    for start in _minutes(FALL_BACK, FALL_BACK + timedelta(minutes=30), persona.tz):
        for seed in range(10):
            nxt = rhythm.next_glance_after(start, random.Random(seed))
            assert nxt.astimezone(UTC) > start.astimezone(UTC), (
                f"从 {start.isoformat()} 起，下一次看手机倒回了 {nxt.isoformat()}"
            )


def test_the_fake_clock_walks_through_the_repeated_hour(persona: Persona) -> None:
    """测试用的钟也得按真实时间走，不然永远测不到线上真会经过的那一小时。"""
    from newperson.clock import FakeClock

    clock = FakeClock(datetime(2026, 11, 1, 5, 59, tzinfo=UTC).astimezone(persona.tz))
    clock.advance(120)
    assert clock.now().astimezone(UTC) == datetime(2026, 11, 1, 6, 1, tzinfo=UTC)
    clock.set(datetime(2026, 11, 1, 6, 5, tzinfo=UTC))
    clock.advance(60)
    assert clock.now().astimezone(UTC) == datetime(2026, 11, 1, 6, 6, tzinfo=UTC)


def test_the_local_time_follows_her_abroad(rhythm: Rhythm, persona: Persona) -> None:
    """她出国那几天，"现在几点"要按她人在的地方算。"""
    day = date(2026, 10, 1)
    while str(rhythm.tz_for(day)) == str(persona.tz) and day < date(2027, 9, 1):
        day += timedelta(days=1)
    assert day < date(2027, 9, 1), "前提不成立：一年里没有跨时区的出行"
    noon_there = datetime.combine(day, datetime.min.time(), tzinfo=rhythm.tz_for(day)) + timedelta(hours=12)
    shown = rhythm.local_time(noon_there.astimezone(persona.tz))
    assert shown.strftime("%H:%M") == "12:00"
    assert str(shown.tzinfo) == str(rhythm.tz_for(day))


def test_finals_week_is_not_a_vacation(persona: Persona) -> None:
    """期末周没有正课，但人最紧。「赶due」要抽得到，不能被当成放假排除掉。

    原来阶段过滤用的是 in_session（今天有没有正课），期末周返回 False：
    一整段期末周里赶due 0%、松过半，提示里同时写着"期末周，人很紧"和"你最近没什么事"。
    """
    start = date(2026, 9, 2)
    counts: Counter[str] = Counter()
    for seed in range(1, 25):
        calendar = AcademicCalendar(persona.academic, seed)
        rhythm = Rhythm(persona.rhythm, persona.tz, seed, calendar)
        for offset in range(730):
            day = start + timedelta(days=offset)
            if calendar.period_for(day).kind == "finals":
                counts[rhythm.phase_for(day).name] += 1
    total = sum(counts.values())
    assert total > 200, "样本太少"
    assert counts["赶due"] / total > 0.1, counts


def test_she_sleeps_in_on_weekends(persona: Persona) -> None:
    """周五周六晚上睡得晚，周末早上起得晚。原来周六跟周二一模一样，看起来像按日抽签的机器。"""
    import statistics

    calendar = AcademicCalendar(persona.academic, persona.seed)
    rhythm = Rhythm(persona.rhythm, persona.tz, persona.seed, calendar)
    wake: dict[bool, list[float]] = {True: [], False: []}
    sleep: dict[bool, list[float]] = {True: [], False: []}
    for offset in range(364):
        day = date(2026, 9, 2) + timedelta(days=offset)
        if calendar.trip_for(day) or calendar.trip_for(day - timedelta(days=1)):
            continue
        daily = rhythm.for_day(day)
        w = daily.wake.astimezone(persona.tz)
        s = daily.sleep_start.astimezone(persona.tz)
        wake[day.weekday() in (5, 6)].append(w.hour * 60 + w.minute)
        minutes = (s - datetime.combine(day, datetime.min.time(), tzinfo=persona.tz)).total_seconds() / 60
        sleep[day.weekday() in (4, 5)].append(minutes)
    assert statistics.mean(wake[True]) - statistics.mean(wake[False]) > 20
    assert statistics.mean(sleep[True]) - statistics.mean(sleep[False]) > 20


def test_a_long_flight_is_not_a_teleport(persona: Persona) -> None:
    """飞回国、飞东京那一晚：从入睡到起床至少是航程，这段时间里她一直不在线。

    原来出发那晚只"睡"六个小时就在上海起床，比十五个小时的航程还短。
    """
    checked = 0
    for seed in range(1, 40):
        calendar = AcademicCalendar(persona.academic, seed)
        rhythm = Rhythm(persona.rhythm, persona.tz, seed, calendar)
        for offset in range(365):
            day = date(2026, 9, 2) + timedelta(days=offset)
            trip, before = calendar.trip_for(day), calendar.trip_for(day - timedelta(days=1))
            flying = (trip and trip != before and trip.transit_hours) or (
                trip is None and before is not None and before.transit_hours
            )
            if not flying:
                continue
            hours = trip.transit_hours if trip else before.transit_hours
            start = rhythm.for_day(day - timedelta(days=1)).sleep_start
            window = rhythm.sleep_window_containing(start + timedelta(minutes=1))
            assert window is not None
            gap = window[1] - window[0]
            assert gap >= timedelta(hours=hours), f"{day} 路上 {hours} 小时，只空了 {gap}"
            for step in range(1, 20):
                t = window[0] + gap * step / 20
                assert rhythm.is_sleeping(t) and rhythm.activity_at(t) == 0, t
            end = window[1]
            assert not rhythm.is_sleeping(end + timedelta(minutes=1)), "落地起床之后还是睡着"
            assert rhythm.next_wake_after(start + timedelta(hours=1)) == end
            checked += 1
    assert checked >= 10


def test_finals_week_never_feels_like_nothing_to_do(persona: Persona) -> None:
    """期末周不会抽到"最近没什么事"：日历已经写着"人很紧"了。"""
    for seed in range(1, 25):
        calendar = AcademicCalendar(persona.academic, seed)
        rhythm = Rhythm(persona.rhythm, persona.tz, seed, calendar)
        for offset in range(365):
            day = date(2026, 9, 2) + timedelta(days=offset)
            if calendar.period_for(day).kind == "finals":
                assert rhythm.phase_for(day).name != "松", day
