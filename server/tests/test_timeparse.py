"""Relative dates, which dateutil's fuzzy parser gets silently and badly wrong.

Every case here was produced by handing a phrase straight to dateutil: it skips
words it doesn't recognise instead of objecting, so "tomorrow" was dropped and
the hour was read as a day of the month. The result was a valid datetime at a
wrong time — no exception, no warning, just an appointment in the past.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from jarvis.utils.timeparse import meridiem_is_ambiguous, parse_when

TZ = "America/New_York"


@pytest.fixture
def now():
    return dt.datetime.now(ZoneInfo(TZ))


def at(text: str) -> dt.datetime:
    return parse_when(text, TZ)


def test_tomorrow_at_four_is_tomorrow_not_the_fourth(now):
    """The original bug: 'tomorrow at 4' resolved to the 4th of the month."""
    result = at("tomorrow at 4")
    assert result.date() == (now + dt.timedelta(days=1)).date()


def test_a_stated_meridiem_does_not_lose_the_day(now):
    """'tomorrow at 4pm' resolved to *today* at 4pm — the time was read and the
    day thrown away."""
    result = at("tomorrow at 4pm")
    assert result.date() == (now + dt.timedelta(days=1)).date()
    assert (result.hour, result.minute) == (16, 0)


def test_a_weekday_resolves_to_that_weekday(now):
    """'friday at 11' resolved to the 11th, whatever day that was."""
    result = at("friday at 11")
    assert result.weekday() == 4
    assert result.hour == 11


def test_a_weekday_is_always_in_the_future(now):
    """Resolving a weekday to today would usually mean a time already past."""
    for name in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        assert at(f"{name} at 10am") > now, name


def test_next_monday_is_a_monday():
    assert at("next monday at 9").weekday() == 0


def test_tonight_at_eight_is_the_evening():
    """'tonight' says which half of the day it is; only the digits were bare."""
    assert at("tonight at 8").hour == 20


def test_tonight_alone_gets_an_evening_hour():
    assert at("tonight").hour == 19


def test_this_morning_keeps_the_morning():
    assert at("this morning at 9").hour == 9


def test_day_after_tomorrow(now):
    result = at("day after tomorrow at 10am")
    assert result.date() == (now + dt.timedelta(days=2)).date()
    assert result.hour == 10


@pytest.mark.parametrize("text", ["next week", "this week", "next month"])
def test_coarse_ranges_are_understood(text):
    """calendar_list is asked for these constantly; dateutil rejects them."""
    assert isinstance(at(text), dt.datetime)


def test_next_week_is_a_week_out(now):
    assert (at("next week").date() - now.date()).days == 7


def test_iso_is_taken_literally():
    result = at("2026-08-09T14:00")
    assert (result.year, result.month, result.day, result.hour) == (2026, 8, 9, 14)


def test_an_explicit_date_still_works():
    result = at("9 august at 2pm")
    assert (result.month, result.day, result.hour) == (8, 9, 14)


def test_midnight_and_noon():
    assert at("midnight").hour == 0
    assert at("noon tomorrow").hour == 12


def test_a_relative_duration_is_not_a_clock_time(now):
    result = at("in 2 hours")
    assert abs((result - now).total_seconds() - 7200) < 60
    assert meridiem_is_ambiguous("in 2 hours") is None, "a duration has no am/pm to ask about"


def test_twenty_four_hour_times_are_left_alone():
    result = at("16:30")
    assert (result.hour, result.minute) == (16, 30)
