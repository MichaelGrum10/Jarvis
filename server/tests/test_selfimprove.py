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
