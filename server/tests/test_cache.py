"""The cache exists for the token budget as much as for latency: an avoided
tool call is also an avoided model round-trip carrying the whole system prompt."""

from __future__ import annotations

import time

import pytest

from jarvis import cache


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def test_a_value_survives_until_its_ttl():
    cache.put("stocks", "NVDA", {"price": 1})
    assert cache.get("stocks", "NVDA") == {"price": 1}


def test_a_stale_value_is_a_miss(monkeypatch):
    cache.put("stocks", "NVDA", {"price": 1})
    # Epoch, not monotonic: they are different clocks, and mixing them makes the
    # entry look like it was written in the future rather than long ago.
    later = time.time() + 10_000
    monkeypatch.setattr(time, "time", lambda: later)

    assert cache.get("stocks", "NVDA") is None


def test_kinds_do_not_collide():
    cache.put("stocks", "today", 1)
    cache.put("calendar", "today", 2)
    assert cache.get("stocks", "today") == 1
    assert cache.get("calendar", "today") == 2


def test_invalidating_one_kind_leaves_the_others():
    """A booking clears the calendar. It has no business clearing quotes."""
    cache.put("calendar", "today", [1])
    cache.put("stocks", "NVDA", [2])

    cache.invalidate("calendar")

    assert cache.get("calendar", "today") is None
    assert cache.get("stocks", "NVDA") == [2]


def test_a_missing_entry_is_a_miss_not_an_error():
    assert cache.get("stocks", "never-stored") is None


def test_a_corrupt_entry_is_a_miss_not_a_crash():
    """A half-written file read by the next request must not take the tool down."""
    cache.put("stocks", "NVDA", {"price": 1})
    next(iter(cache._dir().glob("stocks-*.json"))).write_text("{not json")

    assert cache.get("stocks", "NVDA") is None


def test_keys_containing_user_text_do_not_become_filenames():
    """Keys carry search queries and email addresses; those have no business on
    disk as names."""
    cache.put("search", "michael@icloud.com / ../../etc/passwd", {"ok": True})

    names = [p.name for p in cache._dir().glob("*.json")]
    assert all("icloud" not in n and ".." not in n for n in names)
    assert cache.get("search", "michael@icloud.com / ../../etc/passwd") == {"ok": True}
