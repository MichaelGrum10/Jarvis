"""Skills matching, and the tool selection that keeps requests small."""

from __future__ import annotations

import pytest

from jarvis.agent.toolpicker import relevant_domains, select_tools
from jarvis.config import get_settings
from jarvis.skills import ensure_builtins, match_skill, normalise, skill_preamble
from jarvis.tools.base import load_all_tools


@pytest.fixture
def tools():
    registry = load_all_tools()
    settings = get_settings()
    settings.icloud_email = "a@b.c"
    settings.icloud_app_password = "x"
    settings.bridge_token = "t"
    return registry.available(settings)


# ---------------------------------------------------------------- tool picking


@pytest.mark.parametrize(
    "message,expected",
    [
        ("what's on my calendar today", "calendar"),
        ("am I free thursday", "calendar"),
        ("anything important in my inbox", "mail"),
        ("how's NVDA doing", "finance"),
        ("wsj markets headlines", "news"),
        ("text Sara I'm late", "messages"),
        ("open spotify", "device"),
        ("remember I prefer aisle seats", "memory"),
    ],
)
def test_domain_detection(message, expected):
    assert expected in relevant_domains(message)


def test_haircut_pulls_in_both_calendar_and_places(tools):
    """The flagship flow: wanting a haircut needs somewhere to go *and* a slot to
    put it in. Missing either half breaks the booking."""
    names = {t.name for t in select_tools("I need a haircut", tools)}
    assert "places_search" in names
    assert "calendar_create" in names


def test_selection_is_much_smaller_than_the_full_set(tools):
    chosen = select_tools("how's NVDA doing", tools)
    assert len(chosen) < len(tools) / 2


def test_core_tools_are_always_offered(tools):
    """current_time especially — without it the model dates 'today' from its
    training data and gets it confidently wrong."""
    for message in ("how's NVDA doing", "open spotify", "something unrelated"):
        names = {t.name for t in select_tools(message, tools)}
        assert "current_time" in names, message


def test_ambiguous_request_gets_a_useful_fallback(tools):
    """Matching nothing must not mean offering nothing."""
    chosen = select_tools("hello there", tools)
    assert len(chosen) >= 3


def test_write_tools_accompany_their_readers(tools):
    """'cancel my 3pm' has to be able to both find and delete the event."""
    names = {t.name for t in select_tools("cancel my 3pm meeting", tools)}
    assert "calendar_list" in names and "calendar_delete" in names


# ---------------------------------------------------------------- skills


async def test_builtin_skills_install_and_are_idempotent():
    await ensure_builtins()
    await ensure_builtins()
    assert (await match_skill("good morning")).name == "morning brief"


@pytest.mark.parametrize(
    "message,skill",
    [
        ("good morning", "morning brief"),
        ("brief me", "morning brief"),
        ("how are markets", "market check"),
        ("wrap up", "end of day"),
        ("i need a haircut", "book appointment"),
    ],
)
async def test_skill_triggers(message, skill):
    await ensure_builtins()
    matched = await match_skill(message)
    assert matched is not None and matched.name == skill


@pytest.mark.parametrize("message", ["what is 2+2", "bookmark this page", "tell me a joke"])
async def test_unrelated_messages_match_no_skill(message):
    """A skill firing on an unrelated request would silently reshape the answer."""
    await ensure_builtins()
    assert await match_skill(message) is None


async def test_longest_trigger_wins():
    """'book' and 'book appointment' both match a booking request; the more
    specific phrase is the better read of intent."""
    await ensure_builtins()
    matched = await match_skill("book appointment for tomorrow")
    assert matched is not None and matched.name == "book appointment"


def test_preamble_is_empty_without_a_skill():
    assert skill_preamble(None) == ""


def test_normalise_strips_punctuation():
    assert normalise("Good Morning, Jarvis!") == "good morning  jarvis"
