"""Telemetry and the continuous improvement loop."""

from __future__ import annotations

import pytest

from jarvis.agent.selfimprove import MIN_OCCURRENCES, SelfImprover
from jarvis.config import Settings
from jarvis.telemetry import (
    describe_issue,
    fingerprint,
    mark_resolved,
    record_failure,
    record_if_refusal,
    top_issues,
)


def settings(**overrides) -> Settings:
    base = {"auth_secret": "x" * 32, "access_password": "y" * 12, "groq_api_key": "gsk_t"}
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------- fingerprinting


def test_same_problem_with_different_numbers_groups_together():
    """"Requested 11081" and "Requested 9004" are one problem, not two. Without
    this, a recurring failure never reaches the threshold to be worked on."""
    a = fingerprint("llm_error", "Limit 6000, Requested 11081 tokens")
    b = fingerprint("llm_error", "Limit 6000, Requested 9004 tokens")
    assert a == b


def test_different_problems_stay_separate():
    a = fingerprint("tool_failure", "calendar write rejected")
    b = fingerprint("tool_failure", "imap login refused")
    assert a != b


def test_opaque_ids_are_normalised_away():
    """Real case: every calendar failure embeds a fresh event UID. Without
    collapsing these, one recurring bug looks like N unrelated ones and never
    reaches the threshold to be worked on."""
    a = fingerprint("x", "No event with uid 8a6ac181-dea8-4d8b-bf98-b6f63ba91333@jarvis")
    b = fingerprint("x", "No event with uid 1f2e3d4c-aaaa-bbbb-cccc-dddddddddddd@jarvis")
    assert a == b

    c = fingerprint("x", "org_01kz6namj6ez5r0dvtqrhv8nf2 rate limited")
    d = fingerprint("x", "org_99aa1bbccdd22ee33ff44gg55 rate limited")
    assert c == d


# ---------------------------------------------------------------- recording


async def test_failures_aggregate_by_frequency():
    for _ in range(4):
        await record_failure("tool_failure", "calendar rejected the event", tool="calendar_create")
    await record_failure("tool_failure", "imap timed out", tool="mail_summary")

    issues = await top_issues(days=7)
    assert issues[0]["count"] == 4
    assert issues[0]["tool"] == "calendar_create"


async def test_telemetry_never_raises():
    """Instrumentation that can break the thing it measures is worse than none."""
    await record_failure("kind", "x" * 10_000, request="y" * 5_000)


@pytest.mark.parametrize(
    "reply",
    [
        "I can't do that — there's no tool for sending faxes.",
        "I don't have access to your Spotify account.",
        "That's not something I can help with yet.",
    ],
)
async def test_refusals_are_recorded_as_missing_capability(reply):
    """Nothing errored, so this is invisible in an error log — yet it is the
    clearest possible signal of a missing feature."""
    await record_if_refusal("send a fax to the office", reply)
    issues = await top_issues(days=7)
    assert any(i["kind"] == "missing_capability" for i in issues)


@pytest.mark.parametrize(
    "reply",
    [
        "You have three meetings today, sir.",
        "AAPL is at 311, up 12% over six months.",
        "I've booked it for Tuesday at 3pm.",
    ],
)
async def test_normal_answers_are_not_flagged(reply):
    """A false positive sends the engine chasing a feature that already exists."""
    before = len([i for i in await top_issues(days=7) if i["kind"] == "missing_capability"])
    await record_if_refusal("anything", reply)
    after = len([i for i in await top_issues(days=7) if i["kind"] == "missing_capability"])
    assert after == before


async def test_resolved_issues_drop_out():
    for _ in range(3):
        await record_failure("tool_failure", "a recurring thing", tool="some_tool")
    issues = await top_issues(days=7)
    target = next(i for i in issues if i["tool"] == "some_tool")

    assert await mark_resolved(target["fingerprint"]) == 3
    assert not any(i["fingerprint"] == target["fingerprint"] for i in await top_issues(days=7))


# ---------------------------------------------------------------- goal picking


async def test_one_off_failures_are_left_alone():
    """A single failure is usually a network blip. Acting on it means rewriting
    working code to chase noise."""
    await record_failure("tool_failure", "a one-time blip", tool="flaky_tool")
    improver = SelfImprover(settings(improve_mode="propose"))
    picked = await improver.pick_goal()
    if picked is not None:
        assert "one-time blip" not in picked[1]


async def test_recurring_failure_becomes_a_goal():
    for _ in range(MIN_OCCURRENCES + 2):
        await record_failure(
            "tool_failure", "calendar_create keeps being rejected", tool="calendar_create"
        )
    improver = SelfImprover(settings(improve_mode="propose"))
    picked = await improver.pick_goal()
    assert picked is not None
    fingerprint_value, goal = picked
    assert "calendar_create" in goal
    # The goal must forbid the lazy fixes, or a model under pressure will take them.
    assert "try/except" in goal and "regression test" in goal


async def test_disabled_mode_does_nothing():
    improver = SelfImprover(settings(improve_mode="off"))
    assert (await improver.run_once())["status"] == "disabled"


async def test_idle_when_nothing_recurs():
    improver = SelfImprover(settings(improve_mode="propose"))
    result = await improver.run_once()
    assert result["status"] in {"idle", "succeeded", "failed"}


def test_default_mode_is_off_and_apply_is_opt_in():
    """Auto-deploying to the box holding your mail and calendar has to be a
    decision, never a default."""
    assert settings().improve_mode == "off"


def test_issue_description_carries_enough_to_act_on():
    described = describe_issue(
        {
            "kind": "tool_failure",
            "tool": "calendar_create",
            "count": 14,
            "detail": "iCloud rejected the event",
            "example_request": "book me a haircut tomorrow",
        }
    )
    assert "14 times" in described
    assert "calendar_create" in described
    assert "book me a haircut" in described


def test_every_missing_precondition_is_reported_not_just_the_first():
    """Setting IMPROVE_MODE alone leaves the loop running and failing every
    cycle. Reporting one blocker at a time turns that into three rounds of
    'still disabled'."""
    from jarvis.agent.selfimprove import blockers
    from jarvis.config import Settings

    found = blockers(Settings())
    what = " ".join(b["what"] for b in found)

    assert "IMPROVE_MODE" in what
    assert "AUTONOMY_ENABLED" in what
    assert all(b["fix"] for b in found), "a blocker with no fix is just a complaint"


def test_no_blockers_once_everything_is_set(tmp_path):
    from jarvis.agent.selfimprove import blockers
    from jarvis.config import Settings

    (tmp_path / ".git").mkdir()
    settings = Settings(
        improve_mode="propose", autonomy_enabled=True, autonomy_repo_path=tmp_path
    )

    assert blockers(settings) == []


def test_a_checkout_without_git_is_a_blocker(tmp_path):
    """Without a repo there's no branch to isolate changes on, so the engine
    refuses — silently, from the user's point of view."""
    from jarvis.agent.selfimprove import blockers
    from jarvis.config import Settings

    settings = Settings(
        improve_mode="propose", autonomy_enabled=True, autonomy_repo_path=tmp_path
    )

    assert any("git" in b["what"] for b in blockers(settings))


# ------------------------------------------------- asking for things by name
#
# Two paths lead into the engine now: something broke, or you asked. They are
# deliberately not equal in priority.


async def test_a_request_you_made_outranks_a_bug_that_keeps_happening():
    """A person asking is a stronger signal than a counter crossing a
    threshold, and waiting behind a bug queue is not what "add this" means."""
    from jarvis.agent.selfimprove import get_improver
    from jarvis.db import AutonomyRun, session_scope

    async with session_scope() as session:
        session.add(AutonomyRun(goal="add a parcel tracker", status="queued"))

    picked = await get_improver().next_request()
    assert picked is not None
    run_id, goal = picked
    assert "parcel tracker" in goal

    # Claimed, so a manual cycle racing the loop cannot start it twice.
    assert await get_improver().next_request() is None

    async with session_scope() as session:
        assert (await session.get(AutonomyRun, run_id)).status == "running"


async def test_requests_are_taken_oldest_first():
    from jarvis.agent.selfimprove import get_improver
    from jarvis.db import AutonomyRun, session_scope

    async with session_scope() as session:
        session.add(AutonomyRun(goal="first thing", status="queued"))
    async with session_scope() as session:
        session.add(AutonomyRun(goal="second thing", status="queued"))

    _, first = await get_improver().next_request()
    _, second = await get_improver().next_request()
    assert "first thing" in first
    assert "second thing" in second


async def test_nothing_queued_is_not_an_error():
    from jarvis.agent.selfimprove import get_improver

    assert await get_improver().next_request() is None


async def test_a_recorded_failure_wakes_the_loop():
    """Otherwise a bug found at nine in the morning gets looked at after lunch."""
    import asyncio

    from jarvis.agent.selfimprove import SelfImprover
    from jarvis.telemetry import _watchers, on_failure, record_failure

    improver = SelfImprover()
    improver._owner_loop = asyncio.get_running_loop()
    on_failure(improver.nudge)
    try:
        assert not improver._wake.is_set()
        await record_failure("tool_error", "calendar_list blew up again")
        # call_soon_threadsafe lands on the next loop pass.
        await asyncio.sleep(0)
        assert improver._wake.is_set(), "a failure should ask for attention"
    finally:
        _watchers.remove(improver.nudge)


async def test_the_nudge_is_safe_before_the_loop_is_running():
    """record_failure runs everywhere, including at import time and from worker
    threads. A nudge with no loop yet must be a no-op, not a crash."""
    from jarvis.agent.selfimprove import SelfImprover

    SelfImprover().nudge()   # no loop bound; must not raise


async def test_a_broken_watcher_cannot_break_telemetry():
    """The thing that watches failures must not become one."""
    from jarvis.telemetry import _watchers, on_failure, record_failure

    def explode():
        raise RuntimeError("watcher is broken")

    on_failure(explode)
    try:
        await record_failure("tool_error", "something ordinary")
    finally:
        _watchers.remove(explode)


def test_registering_the_same_watcher_twice_does_not_stack_it():
    from jarvis.telemetry import _watchers, on_failure

    def watcher():
        pass

    on_failure(watcher)
    on_failure(watcher)
    try:
        assert _watchers.count(watcher) == 1
    finally:
        _watchers.remove(watcher)
