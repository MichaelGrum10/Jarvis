"""Endpoint rankings: measured, remembered, and used to order the pool."""

from __future__ import annotations

import json
import time

import pytest

from jarvis.llm import health
from jarvis.llm.pool import Endpoint

GROQ = "https://api.groq.com/openai/v1"


@pytest.fixture(autouse=True)
def clean():
    health._path().unlink(missing_ok=True)
    yield
    health._path().unlink(missing_ok=True)


def endpoints(*labels):
    return [Endpoint(model=x, api_key="k", base_url=GROQ, label=x) for x in labels]


def test_without_measurements_the_order_is_left_alone():
    """The configured order is the user's stated preference; nothing may
    silently override it on no evidence."""
    given = endpoints("a", "b", "c")
    assert [e.label for e in health.rank(given)] == ["a", "b", "c"]


def test_working_endpoints_come_before_broken_ones():
    health.record([
        {"label": "slow", "tools_large": True, "large_latency": 2.0},
        {"label": "broken", "tools_large": False, "error": "fails tool calls"},
        {"label": "fast", "tools_large": True, "large_latency": 0.3},
    ])

    order = [e.label for e in health.rank(endpoints("broken", "slow", "fast"))]

    assert order == ["fast", "slow", "broken"]


def test_an_unmeasured_endpoint_is_not_treated_as_broken():
    """"Not yet tested" is not evidence against it. Otherwise anything newly
    added is buried behind everything already known."""
    health.record([
        {"label": "known-good", "tools_large": True, "large_latency": 0.5},
        {"label": "known-bad", "tools_large": False, "error": "no tool calls"},
    ])

    order = [e.label for e in health.rank(endpoints("known-bad", "brand-new", "known-good"))]

    assert order == ["known-good", "brand-new", "known-bad"]


def test_stale_measurements_are_ignored():
    """A provider broken a fortnight ago says nothing about today."""
    health.record([{"label": "a", "tools_large": False, "error": "was broken"}])
    payload = json.loads(health._path().read_text())
    payload["at"] = time.time() - health.MAX_AGE_DAYS * 86_400 - 60
    health._path().write_text(json.dumps(payload))

    assert health.load() == {}
    assert [e.label for e in health.rank(endpoints("a", "b"))] == ["a", "b"]


def test_a_corrupt_file_is_ignored_rather_than_fatal():
    health._path().parent.mkdir(parents=True, exist_ok=True)
    health._path().write_text("{not json")

    assert health.load() == {}
    assert [e.label for e in health.rank(endpoints("a"))] == ["a"]
