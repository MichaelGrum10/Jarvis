"""Choosing between the Mac and iCloud.

Two sources for the same question is a good thing until they disagree about
whether they answered. The failure this file exists to prevent is a plausible
empty list — the Mac being asleep, or asked about a range it cannot see, and the
tool reporting "nothing on today" instead of asking iCloud.
"""

from __future__ import annotations

import datetime as dt

import pytest

from jarvis.agentlink import AgentUnavailable
from jarvis.config import get_settings
from jarvis.integrations.apple_calendar import CalendarError
from jarvis.integrations.mac_source import MacSource, get_mac
from jarvis.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _no_cached_calendar():
    """The calendar cache is keyed by range, and every test here asks about
    'today' — without this they answer each other's questions."""
    from jarvis import cache

    cache.invalidate("calendar")
    yield
    cache.invalidate("calendar")


@pytest.fixture
def mac(monkeypatch):
    """A Mac that is connected and answers whatever the test sets."""
    source = MacSource()
    monkeypatch.setattr(type(source), "available", property(lambda self: True))
    monkeypatch.setattr("jarvis.integrations.mac_source._source", source)
    return source


@pytest.fixture
def absent_mac(monkeypatch):
    source = MacSource()
    monkeypatch.setattr(type(source), "available", property(lambda self: False))
    monkeypatch.setattr("jarvis.integrations.mac_source._source", source)
    return source


class FakeCalendar:
    def __init__(self):
        self.asked = []

    async def events_between(self, start, end, calendar=""):
        self.asked.append((start, end, calendar))
        return []


@pytest.fixture
def icloud(monkeypatch):
    cal = FakeCalendar()
    monkeypatch.setattr("jarvis.integrations.apple_calendar.get_calendar", lambda: cal)
    return cal


def _reply(events):
    return {"ok": True, "data": {"events": events}}


def _event(start, end, summary="Standup", calendar="Work"):
    return {
        "uid": "u-1", "summary": summary, "calendar": calendar, "location": "",
        "all_day": False, "start": start.isoformat(), "end": end.isoformat(),
    }


# ------------------------------------------------------------ which one answers


async def test_the_mac_answers_when_it_is_awake(mac, icloud, monkeypatch):
    from jarvis.tools.calendar_tool import calendar_list

    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(_reply([_event(soon, soon + dt.timedelta(hours=1))])),
    )

    result = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))

    assert result.ok
    assert result.data["source"] == "Mac"
    assert icloud.asked == [], "iCloud should not have been troubled"


async def test_icloud_answers_when_the_mac_is_shut(absent_mac, icloud):
    from jarvis.tools.calendar_tool import calendar_list

    result = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))

    assert result.ok
    assert result.data["source"] == "iCloud"
    assert len(icloud.asked) == 1


async def test_a_mac_that_drops_mid_call_falls_through_to_icloud(mac, icloud, monkeypatch):
    """The lid closing during a call must not become 'you have nothing on'."""
    def die(self, command, args=None, timeout=None):
        raise AgentUnavailable("The Mac agent dropped mid-call.")

    monkeypatch.setattr("jarvis.agentlink.AgentLink.call", die)
    from jarvis.tools.calendar_tool import calendar_list

    result = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))

    assert result.ok
    assert result.data["source"] == "iCloud"
    assert len(icloud.asked) == 1


async def test_a_permission_refusal_on_the_mac_falls_through_too(mac, icloud, monkeypatch):
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(
            {"ok": False, "error": "macOS has not been granted permission..."}
        ),
    )
    from jarvis.tools.calendar_tool import calendar_list

    result = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))

    assert result.data["source"] == "iCloud"


async def test_both_sources_down_says_both(mac, monkeypatch):
    """Naming only CalDAV sends someone to fix credentials when the fix is
    opening their laptop."""
    from jarvis.integrations import mac_source

    monkeypatch.setattr(get_settings(), "agent_secret", "set")
    monkeypatch.setattr(type(get_mac()), "available", property(lambda self: False))

    class Broken:
        async def events_between(self, *a, **k):
            raise CalendarError("iCloud calendar is not configured.")

    monkeypatch.setattr("jarvis.integrations.apple_calendar.get_calendar", lambda: Broken())

    with pytest.raises(CalendarError) as caught:
        await mac_source.read_events(
            dt.datetime.now(dt.timezone.utc), dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1), ""
        )
    assert "Mac is not connected" in str(caught.value)
    assert "iCloud" in str(caught.value)


async def test_a_refused_permission_with_no_icloud_names_the_permission(mac, monkeypatch):
    """The likeliest real failure: Automation denied on the Mac, nothing behind
    it. Reporting only iCloud's complaint sends someone to set CALDAV_USERNAME
    when the fix is a checkbox in System Settings."""
    from jarvis.integrations import mac_source

    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(
            {"ok": False, "error": "macOS has not been granted permission... > Automation"}
        ),
    )

    class Broken:
        async def events_between(self, *a, **k):
            raise CalendarError("iCloud calendar is not configured.")

    monkeypatch.setattr("jarvis.integrations.apple_calendar.get_calendar", lambda: Broken())

    now = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(CalendarError) as caught:
        await mac_source.read_events(now, now + dt.timedelta(days=1))

    assert "Automation" in str(caught.value)
    assert "iCloud" in str(caught.value)


# ------------------------------------------------------------- the range itself


async def test_a_cached_answer_keeps_the_source_that_produced_it(mac, icloud, monkeypatch):
    """A cached result from the Mac must not come back labelled iCloud."""
    from jarvis.tools.calendar_tool import calendar_list

    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(
            _reply([_event(soon, soon + dt.timedelta(hours=1))])
        ),
    )

    first = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))
    second = await calendar_list(start="today", ctx=ToolContext(timezone="UTC"))

    assert first.data["source"] == second.data["source"] == "Mac"
    assert second.data["count"] == first.data["count"]


def test_a_range_beyond_the_mac_window_is_not_attempted(mac):
    """Enumerating every calendar over AppleScript for a year costs more than
    the answer is worth, and CalDAV filters server-side."""
    now = dt.datetime.now(dt.timezone.utc)

    assert mac.can_serve_range(now, now + dt.timedelta(days=7)) is True
    assert mac.can_serve_range(now, now + dt.timedelta(days=365)) is False


def test_a_range_entirely_in_the_past_is_still_served(mac):
    """The window is offsets either side of now, so backwards is fine — this is
    what 'what did I have on yesterday' needs."""
    now = dt.datetime.now(dt.timezone.utc)

    assert mac.can_serve_range(now - dt.timedelta(days=2), now - dt.timedelta(days=1)) is True


async def test_events_outside_the_asked_range_are_dropped(mac, monkeypatch):
    """The window is widened by a few minutes for clock skew, not to return
    extra events."""
    now = dt.datetime.now(dt.timezone.utc)
    inside = now + dt.timedelta(hours=1)
    outside = now + dt.timedelta(days=3)
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(_reply([
            _event(inside, inside + dt.timedelta(hours=1), "Inside"),
            _event(outside, outside + dt.timedelta(hours=1), "Outside"),
        ])),
    )

    events = await mac.events_between(now, now + dt.timedelta(days=1))

    assert [e.summary for e in events] == ["Inside"]


async def test_the_window_reaches_backwards_for_this_morning(mac, monkeypatch):
    """'What's on today' asked at 2pm must still see the 9am standup — a
    forward-only window would silently drop it."""
    seen = {}

    def capture(self, command, args=None, timeout=None):
        seen.update(args or {})
        return _async(_reply([]))

    monkeypatch.setattr("jarvis.agentlink.AgentLink.call", capture)
    now = dt.datetime.now(dt.timezone.utc)
    await mac.events_between(now - dt.timedelta(hours=5), now + dt.timedelta(hours=5))

    assert seen["start_offset"] < 0
    assert seen["end_offset"] > 0


async def test_an_unparseable_timestamp_drops_that_event_only(mac, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    good = _event(now + dt.timedelta(hours=1), now + dt.timedelta(hours=2), "Good")
    bad = dict(good, summary="Bad", start="not a date")
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async(_reply([bad, good])),
    )

    events = await mac.events_between(now, now + dt.timedelta(days=1))

    assert [e.summary for e in events] == ["Good"]


# -------------------------------------------------------------------- the mail


async def test_mail_comes_from_the_mac_when_it_is_awake(mac, monkeypatch):
    from jarvis.tools.mail_tool import mail_summary

    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async({"ok": True, "data": {"messages": [
            {"uid": "m1", "subject": "Lease renewal", "sender": "Ada",
             "sender_email": "ada@example.com", "date": now.isoformat(), "unread": True},
        ]}}),
    )

    result = await mail_summary()

    assert result.data["source"] == "Mac"
    assert result.data["total"] == 1


async def test_a_mailbox_other_than_the_inbox_goes_to_imap(mac, monkeypatch):
    """Mail.app's AppleScript reaches the unified inbox and nothing else."""
    from jarvis.tools import mail_tool

    called = {}

    class FakeMail:
        async def recent(self, **kwargs):
            called.update(kwargs)
            return []

    monkeypatch.setattr("jarvis.integrations.apple_mail.get_mail", lambda: FakeMail())
    result = await mail_tool.mail_summary(mailbox="Archive")

    assert result.data["source"] == "iCloud"
    assert called["mailbox"] == "Archive"


async def test_mail_search_says_which_haystack_it_searched(mac, monkeypatch):
    """The two sources genuinely search different things, so an empty result
    means different things."""
    from jarvis.tools.mail_tool import mail_search

    monkeypatch.setattr(
        "jarvis.agentlink.AgentLink.call",
        lambda self, c, a=None, timeout=None: _async({"ok": True, "data": {"messages": []}}),
    )

    result = await mail_search(query="rent")

    assert result.data["source"] == "Mac"
    assert result.data["searched"] == "subjects and senders only"


def _async(value):
    async def run():
        return value
    return run()


# ------------------------------------------------------- times, told correctly


async def test_the_hud_compares_instants_not_strings(monkeypatch):
    """Two valid timestamps with different offsets are the same moment and sort
    the wrong way round as text."""
    from jarvis import hud
    from jarvis.integrations.apple_calendar import CalEvent

    monkeypatch.setattr(get_settings(), "timezone", "America/New_York")
    now = dt.datetime.now(dt.timezone.utc)

    # Written with a +00:00 offset while "now" is Eastern: as strings the future
    # event sorts *before* now and would be dropped.
    soon = (now + dt.timedelta(minutes=30)).astimezone(dt.timezone.utc)
    past = (now - dt.timedelta(hours=3)).astimezone(dt.timezone.utc)

    async def events(start, end, calendar=""):
        return [
            CalEvent(uid="p", summary="Already happened", start=past.isoformat(),
                     end=past.isoformat(), all_day=False),
            CalEvent(uid="s", summary="Dinner", start=soon.isoformat(),
                     end=soon.isoformat(), all_day=False),
        ], "Mac"

    monkeypatch.setattr("jarvis.integrations.mac_source.read_events", events)
    panel = await hud._calendar()

    assert [e["summary"] for e in panel["events"]] == ["Dinner"]
    assert panel["events"][0]["imminent"] is True


async def test_an_all_day_event_keeps_its_flag_through_the_hud(monkeypatch):
    """The browser shows 'All day' rather than 00:00 — but only if the flag
    survives the trip."""
    from jarvis import hud
    from jarvis.integrations.apple_calendar import CalEvent

    monkeypatch.setattr(get_settings(), "timezone", "America/New_York")
    tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)

    async def events(start, end, calendar=""):
        return [CalEvent(uid="f", summary="Feast", start=tomorrow.isoformat(),
                         end=tomorrow.isoformat(), all_day=True)], "Mac"

    monkeypatch.setattr("jarvis.integrations.mac_source.read_events", events)
    panel = await hud._calendar()

    assert panel["events"][0]["all_day"] is True
