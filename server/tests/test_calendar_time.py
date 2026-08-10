"""The three ways booking went wrong in real use: events landing in the past,
bare hours silently read as morning, and a follow-up correction having nothing
to refer back to."""

from __future__ import annotations

import datetime as dt

import pytest

from jarvis.tools import recent_events
from jarvis.tools.base import ToolContext
from jarvis.utils.meridiem import resolve_meridiem
from jarvis.utils.timeparse import meridiem_is_ambiguous


@pytest.fixture(autouse=True)
def clean_recent():
    recent_events.reset()
    yield
    recent_events.reset()


# --------------------------------------------------------------- ambiguity

@pytest.mark.parametrize(
    "text",
    ["2pm", "2 PM", "2 p.m.", "14:00", "2026-08-09T14:00:00", "08:30", "tomorrow"],
)
def test_stated_times_are_not_treated_as_ambiguous(text):
    assert meridiem_is_ambiguous(text) is None


@pytest.mark.parametrize(
    ("text", "hour"),
    [("2", 2), ("at 2", 2), ("2:30", 2), ("tomorrow at 9", 9), ("friday at 11", 11)],
)
def test_bare_hours_are_flagged(text, hour):
    assert meridiem_is_ambiguous(text) == hour


def test_a_date_is_not_mistaken_for_a_clock_hour():
    """'june 3rd' is a date. Reading the 3 as an hour would invent a time the
    user never gave."""
    assert meridiem_is_ambiguous("june 3rd") is None


# -------------------------------------------------------------- resolution

@pytest.mark.parametrize("hour", [1, 2, 3, 4, 5])
def test_small_hours_resolve_to_afternoon_whatever_the_activity(hour):
    """Nobody books anything at 3 AM, so the afternoon reading is safe even for
    the activities that are otherwise never guessed."""
    for title in ("haircut", "meeting", ""):
        resolved, _ = resolve_meridiem(hour, title)
        assert resolved == hour + 12


def test_a_haircut_at_nine_is_the_morning():
    resolved, reason = resolve_meridiem(9, "Haircut at the barber")
    assert resolved == 9
    assert reason


def test_dinner_is_the_evening():
    assert resolve_meridiem(7, "Dinner with Sara")[0] == 19


def test_breakfast_is_the_morning():
    assert resolve_meridiem(8, "Breakfast with Tom")[0] == 8


@pytest.mark.parametrize("hour", [7, 8, 9, 10, 11])
def test_a_meeting_at_an_ordinary_hour_is_never_guessed(hour):
    """The user's own instruction: infer for a haircut, ask for a meeting. Both
    readings of 'meeting at 8' are real and the wrong one is a missed meeting."""
    assert resolve_meridiem(hour, "Meeting with the board")[0] is None


def test_an_unknown_activity_at_an_ambiguous_hour_is_asked_about():
    assert resolve_meridiem(10, "Thing")[0] is None


def test_twelve_is_midday():
    assert resolve_meridiem(12, "Lunch")[0] == 12


# ------------------------------------------------------- recent event memory

def _event(uid="abc@jarvis", title="Haircut", start="2026-08-08T14:00:00"):
    return {"uid": uid, "summary": title, "start": start, "calendar": "Home"}


def test_a_just_created_event_is_offered_as_the_antecedent_for_it():
    recent_events.remember(7, _event(), "created")
    block = recent_events.preamble(7)
    assert "abc@jarvis" in block
    assert "Haircut" in block


def test_conversations_do_not_see_each_others_events():
    recent_events.remember(7, _event(), "created")
    assert recent_events.preamble(8) == ""


def test_a_deleted_event_stops_being_offered():
    recent_events.remember(7, _event(), "created")
    recent_events.forget(7, "abc@jarvis")
    assert recent_events.preamble(7) == ""


def test_the_newest_event_is_listed_first():
    recent_events.remember(7, _event(uid="old@jarvis", title="Dentist"), "created")
    recent_events.remember(7, _event(uid="new@jarvis", title="Haircut"), "created")
    block = recent_events.preamble(7)
    assert block.index("new@jarvis") < block.index("old@jarvis")


def test_re_touching_an_event_does_not_duplicate_it():
    recent_events.remember(7, _event(), "created")
    recent_events.remember(7, _event(start="2026-08-08T16:00:00"), "updated")
    assert recent_events.preamble(7).count("abc@jarvis") == 1


def test_stale_events_are_not_offered_as_it():
    """Two hours on, "move it" means something the user is looking at now, not
    whatever was booked this morning."""
    recent_events.remember(7, _event(), "created")
    stale = dt.datetime.now(dt.UTC) - recent_events.TTL - dt.timedelta(minutes=1)
    recent_events._recent[7][0]["at"] = stale
    assert recent_events.preamble(7) == ""


def test_events_with_no_conversation_are_ignored():
    """Tool calls outside a conversation (autonomy, tests) must not leak into
    every prompt."""
    recent_events.remember(None, _event(), "created")
    assert recent_events.preamble(None) == ""


# ------------------------------------------------- the create/update guards

class FakeCalendar:
    """Records what it was asked to write, so the guards can be tested without
    talking to iCloud."""

    def __init__(self):
        self.created = []
        self.updated = []

    async def create_event(self, summary, start, end, **kw):
        self.created.append((summary, start, end))
        return _Event(summary, start, end)

    async def update_event(self, uid, **kw):
        self.updated.append((uid, kw))
        start = kw.get("start") or dt.datetime.now(dt.UTC)
        return _Event(kw.get("summary") or "Haircut", start, start, uid=uid)


class _Event:
    def __init__(self, summary, start, end, uid="new@jarvis"):
        self.uid, self.summary, self.start, self.end = uid, summary, start, end

    def to_dict(self):
        return {
            "uid": self.uid, "summary": self.summary,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
            "calendar": "Home",
        }


@pytest.fixture
def fake_calendar(monkeypatch):
    cal = FakeCalendar()
    monkeypatch.setattr("jarvis.tools.calendar_tool.get_calendar", lambda: cal)
    return cal


async def test_an_event_in_the_past_is_refused_rather_than_written(fake_calendar, registry):
    from jarvis.tools.calendar_tool import calendar_create

    result = await calendar_create(
        title="Dentist", start="2020-01-01T10:00:00", ctx=ToolContext(timezone="America/New_York")
    )

    assert not result.ok
    assert "past" in result.error.lower()
    assert fake_calendar.created == [], "nothing should have been written"


async def test_the_past_can_still_be_recorded_when_confirmed(fake_calendar, registry):
    """Logging something that already happened is legitimate — the guard is
    against doing it silently, not against doing it."""
    from jarvis.tools.calendar_tool import calendar_create

    result = await calendar_create(
        title="Dentist", start="2020-01-01T10:00:00", allow_past=True,
        ctx=ToolContext(timezone="America/New_York"),
    )

    assert result.ok
    assert len(fake_calendar.created) == 1


async def test_an_ambiguous_hour_is_asked_about_not_guessed(fake_calendar, registry):
    from jarvis.tools.calendar_tool import calendar_create

    result = await calendar_create(
        title="Meeting with the board", start="tomorrow at 8",
        ctx=ToolContext(timezone="America/New_York"),
    )

    assert not result.ok
    assert "AM or PM" in result.error
    assert fake_calendar.created == []


async def test_an_obvious_hour_is_inferred_and_stated(fake_calendar, registry):
    """'Haircut at 2' books at 2 PM, and says so — a silent correct guess is
    indistinguishable from a silent wrong one."""
    from jarvis.tools.calendar_tool import calendar_create

    result = await calendar_create(
        title="Haircut", start="tomorrow at 2",
        ctx=ToolContext(timezone="America/New_York", conversation_id=3),
    )

    assert result.ok
    assert fake_calendar.created[0][1].hour == 14
    assert "2 PM" in result.data["interpretation"]


async def test_creating_an_event_makes_it_referrable_next_turn(fake_calendar, registry):
    from jarvis.tools.calendar_tool import calendar_create

    await calendar_create(
        title="Haircut", start="tomorrow at 2",
        ctx=ToolContext(timezone="America/New_York", conversation_id=3),
    )

    assert "new@jarvis" in recent_events.preamble(3)


async def test_update_moves_the_event_rather_than_replacing_it(fake_calendar, registry):
    """Delete-and-recreate loses invitees and alarms, and shows up on the user's
    phone as a cancellation followed by an unrelated new event."""
    from jarvis.tools.calendar_tool import calendar_update

    result = await calendar_update(
        uid="abc@jarvis", start="tomorrow at 4", title="Haircut",
        ctx=ToolContext(timezone="America/New_York", conversation_id=3),
    )

    assert result.ok
    uid, kwargs = fake_calendar.updated[0]
    assert uid == "abc@jarvis"
    assert kwargs["start"].hour == 16


async def test_a_single_day_question_does_not_search_a_week(fake_calendar, registry):
    """Defaulting to seven days meant "what's on today" made several round-trips
    to Apple and filtered a week of events to answer about one day."""
    from jarvis.tools.calendar_tool import calendar_list

    seen = {}

    async def record(start, end, calendar=""):
        seen["span"] = (end - start).days
        return []

    fake_calendar.events_between = record
    await calendar_list(start="today", ctx=ToolContext(timezone="America/New_York"))

    assert seen["span"] == 1


async def test_an_explicit_range_is_still_honoured(fake_calendar, registry):
    from jarvis.tools.calendar_tool import calendar_list

    seen = {}

    async def record(start, end, calendar=""):
        seen["span"] = (end - start).days
        return []

    fake_calendar.events_between = record
    await calendar_list(start="today", end="next week", ctx=ToolContext(timezone="America/New_York"))

    assert seen["span"] >= 6
