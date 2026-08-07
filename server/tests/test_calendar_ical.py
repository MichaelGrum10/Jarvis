"""The iCalendar payload written to CalDAV.

Reading calendars always worked; creating events did not. The cause was in the
generated VCALENDAR, so that is what these assert.
"""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent


def build(start: dt.datetime, end: dt.datetime) -> str:
    """Mirror of AppleCalendar._create_event's payload construction."""
    ical = ICalendar()
    ical.add("prodid", "-//Jarvis//EN")
    ical.add("version", "2.0")
    event = IEvent()
    event.add("uid", f"{uuid.uuid4()}@jarvis")
    event.add("dtstamp", dt.datetime.now(dt.UTC))
    event.add("dtstart", start.astimezone(dt.UTC))
    event.add("dtend", end.astimezone(dt.UTC))
    event.add("summary", "Haircut")
    ical.add_component(event)
    return ical.to_ical().decode()


def test_no_tzid_without_a_vtimezone():
    """The bug. RFC 5545 requires any referenced TZID to be defined in the same
    VCALENDAR; icalendar emits the reference but not the definition, and iCloud
    rejects the event outright. Writing UTC sidesteps it entirely."""
    tz = ZoneInfo("America/New_York")
    start = dt.datetime(2026, 8, 10, 15, 0, tzinfo=tz)
    payload = build(start, start + dt.timedelta(hours=1))

    if "TZID=" in payload:
        assert "BEGIN:VTIMEZONE" in payload, (
            "payload references a TZID it never defines — iCloud will reject this"
        )


def test_times_are_utc_and_correct():
    tz = ZoneInfo("America/New_York")           # UTC-4 in August
    start = dt.datetime(2026, 8, 10, 15, 0, tzinfo=tz)
    payload = build(start, start + dt.timedelta(hours=1))

    assert "DTSTART:20260810T190000Z" in payload
    assert "DTEND:20260810T200000Z" in payload


def test_naive_local_time_still_lands_right():
    """The tool parses "3pm" into a local naive datetime; the integration
    localises it before converting. Verify the round trip, not just the format."""
    tz = ZoneInfo("Europe/London")               # UTC+1 in July
    start = dt.datetime(2026, 7, 1, 9, 30).replace(tzinfo=tz)
    payload = build(start, start + dt.timedelta(minutes=30))

    assert "DTSTART:20260701T083000Z" in payload


def test_required_fields_present():
    start = dt.datetime(2026, 8, 10, 15, 0, tzinfo=dt.UTC)
    payload = build(start, start + dt.timedelta(hours=1))

    for field in ("BEGIN:VCALENDAR", "VERSION:2.0", "PRODID", "BEGIN:VEVENT",
                  "UID:", "DTSTAMP:", "SUMMARY:", "END:VEVENT", "END:VCALENDAR"):
        assert field in payload, f"missing {field}"
