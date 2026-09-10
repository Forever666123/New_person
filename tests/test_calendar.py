"""学期日历与出行的测试。"""

from __future__ import annotations

from datetime import date, timedelta

from newperson.calendar import AcademicCalendar
from newperson.persona import Persona
from newperson.rhythm import Rhythm


def test_semester_dates_match_the_real_calendar(calendar: AcademicCalendar) -> None:
    assert calendar.period_for(date(2026, 9, 15)).kind == "in_session"
    assert calendar.period_for(date(2026, 12, 15)).kind == "finals"
    assert calendar.period_for(date(2027, 1, 2)).kind == "break"
    assert calendar.period_for(date(2027, 2, 10)).kind == "in_session"
    assert calendar.period_for(date(2027, 6, 1)).kind == "summer"


def test_thanksgiving_wins_over_the_semester_around_it(calendar: AcademicCalendar) -> None:
    """感恩节嵌在秋季学期里，重叠时假期说了算。"""
    period = calendar.period_for(date(2026, 11, 26))
    assert period.kind == "break"
    assert period.name == "感恩节"


def test_no_classes_outside_the_semester(persona: Persona, rhythm: Rhythm) -> None:
    for day in (date(2026, 12, 15), date(2027, 1, 5), date(2027, 3, 10), date(2027, 6, 8)):
        assert rhythm.for_day(day).classes == [], f"{day} 不该有课"


def test_classes_happen_during_the_semester(persona: Persona, rhythm: Rhythm) -> None:
    """学期里的上课日要真的排到课，否则 busy 状态永远不会出现。"""
    start = date(2026, 9, 7)
    got = [d for d in (start + timedelta(days=i) for i in range(14)) if rhythm.for_day(d).classes]
    assert len(got) >= 4


def test_a_long_break_usually_means_a_trip(calendar: AcademicCalendar) -> None:
    winter = [date(2026, 12, 21) + timedelta(days=i) for i in range(23)]
    assert any(calendar.trip_for(d) for d in winter), "整个寒假都没出过门"


def test_a_trip_is_stable_across_restarts(persona: Persona) -> None:
    """她"上次去了哪"不能每次重启都变。"""
    a = AcademicCalendar(persona.academic, persona.seed)
    b = AcademicCalendar(persona.academic, persona.seed)
    days = [date(2026, 12, 21) + timedelta(days=i) for i in range(23)]
    assert [str(a.trip_for(d)) for d in days] == [str(b.trip_for(d)) for d in days]


def test_trips_stay_inside_their_break(calendar: AcademicCalendar) -> None:
    for i in range(400):
        day = date(2026, 9, 1) + timedelta(days=i)
        trip = calendar.trip_for(day)
        if trip:
            period = calendar.period_for(day)
            assert period.start <= trip.start and trip.end <= period.end


def test_travelling_changes_her_timezone(persona: Persona, calendar: AcademicCalendar) -> None:
    """飞去别的时区，作息就跟着那边走，回消息的节奏自然变了。"""
    zones = set()
    for i in range(400):
        zones.add(calendar.timezone_for(date(2026, 9, 1) + timedelta(days=i)))
    assert len(zones) > 1, "跑了一整年都没离开过波士顿"


def test_no_trips_during_the_semester(calendar: AcademicCalendar) -> None:
    assert calendar.trip_for(date(2026, 10, 15)) is None
    assert calendar.trip_for(date(2027, 2, 20)) is None


def test_unknown_years_still_get_a_calendar(calendar: AcademicCalendar) -> None:
    """配置只覆盖到 2027 年，人物跑久了不能突然没有学期。"""
    for day in (date(2029, 10, 1), date(2029, 7, 4), date(2030, 1, 20), date(2029, 12, 28)):
        period = calendar.period_for(day)
        assert period.start <= day <= period.end
        assert period.name


def test_finals_week_is_quieter_than_a_break(persona: Persona, rhythm: Rhythm) -> None:
    finals = rhythm.for_day(date(2026, 12, 15))
    holiday = rhythm.for_day(date(2027, 1, 5))
    assert finals.activity_multiplier < holiday.activity_multiplier


def test_she_sleeps_more_on_holiday(persona: Persona, rhythm: Rhythm) -> None:
    """假期没什么事，不可能还是只睡五六个小时。"""

    def mean_sleep(start: date, days: int) -> float:
        total = 0.0
        for i in range(days):
            d = start + timedelta(days=i)
            total += (rhythm.for_day(d + timedelta(days=1)).wake - rhythm.for_day(d).sleep_start).total_seconds()
        return total / days / 3600

    assert mean_sleep(date(2026, 12, 22), 20) > mean_sleep(date(2026, 10, 1), 20)


def test_she_checks_her_phone_less_while_travelling(persona: Persona, rhythm: Rhythm) -> None:
    """在外面玩的时候手机看得少，哪怕那段时间本来是假期。"""
    from datetime import date as _date

    away = home = None
    for i in range(120):
        d = _date(2026, 12, 21) + timedelta(days=i)
        daily = rhythm.for_day(d)
        if daily.period_kind not in ("break", "summer"):
            continue
        if daily.trip and away is None:
            away = daily.activity_multiplier / max(daily.trip.activity_multiplier, 0.01)
        if not daily.trip and home is None:
            home = daily.activity_multiplier
    assert away is not None and home is not None
    assert persona.academic.travel.long_activity_multiplier < 1.0
