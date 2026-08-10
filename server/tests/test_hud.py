"""The HUD renders from integrations and cache, never through the model — a
dashboard refreshing through the agent would spend a full turn every minute."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from jarvis import cache, hud
from jarvis.quotes import QUOTES, quote_of_the_day


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


async def test_one_broken_panel_does_not_blank_the_others():
    """An iCloud outage must not take the markets down with it."""
    async def works():
        return {"value": 1}

    async def fails():
        raise RuntimeError("iCloud unavailable")

    good = await hud._panel("markets", works)
    bad = await hud._panel("calendar", fails)

    assert good["status"] == "ok"
    assert bad["status"] == "down"
    assert "iCloud" in bad["error"]


async def test_a_failed_refresh_keeps_the_last_value_and_says_how_old():
    """A blank panel and a stale one look identical while meaning opposite
    things, and invented data is worse than either."""
    async def works():
        return {"events": [{"summary": "Dentist"}]}

    async def fails():
        raise RuntimeError("network down")

    await hud._panel("calendar", works)
    cache.invalidate("hud_calendar")          # the fresh entry expires
    result = await hud._panel("calendar", fails)

    assert result["status"] == "stale"
    assert result["events"] == [{"summary": "Dentist"}]
    assert result["age"] is not None


async def test_a_long_dead_panel_stops_pretending():
    async def fails():
        raise RuntimeError("still down")

    cache.put("hud_inbox_last", "current", {"messages": [], "_at": time.time() - 99_999})
    result = await hud._panel("inbox", fails)

    assert result["status"] == "down", "an hour-old inbox is not worth showing"


async def test_a_cached_panel_does_no_work_at_all():
    calls = []

    async def build():
        calls.append(1)
        return {"value": 1}

    await hud._panel("markets", build)
    await hud._panel("markets", build)

    assert len(calls) == 1, "the second glance must not hit the network"


def test_the_quote_is_stable_for_a_whole_day():
    """A quote that reshuffles on every refresh is decoration, not a thought."""
    day = dt.date(2026, 8, 10)
    assert quote_of_the_day(day) == quote_of_the_day(day)
    assert quote_of_the_day(day) != quote_of_the_day(day + dt.timedelta(days=1))


def test_every_quote_carries_a_source():
    """The brief said well-attested only. An unsourced line is how a fabricated
    one gets in."""
    for quote in QUOTES:
        assert quote["text"].strip()
        assert quote["source"].strip(), quote["text"]


def test_no_quote_is_attributed_to_a_figure_with_no_written_record():
    """Genghis Khan and Hannibal have no reliably attested quotations; what
    circulates under their names is invention."""
    joined = " ".join(q["source"].lower() for q in QUOTES)
    assert "genghis" not in joined
    assert "hannibal" not in joined
