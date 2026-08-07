"""Skills: named routines you can teach Jarvis once and invoke forever.

A skill is a saved instruction with trigger phrases. When something you say
matches a trigger, the skill's instruction is folded into the system prompt for
that turn — so "run my morning brief" becomes a detailed, consistent procedure
rather than whatever the model improvises today.

This is deliberately prompt-level rather than a scripting language. The model
already knows how to use every tool; what it lacks is *your* standing preferences
about how a recurring job should be done. A paragraph of instruction captures
that far better than a rigid sequence of steps would, and it degrades gracefully
when the situation isn't quite what you imagined when you wrote it.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from .db import Skill, session_scope, utcnow

log = logging.getLogger(__name__)

# Shipped with the assistant so it's useful before you've written anything.
# Each is a real procedure rather than a slogan — the detail is the point.
BUILTIN_SKILLS: list[dict] = [
    {
        "name": "morning brief",
        "triggers": "morning brief,good morning,brief me,catch me up,what did i miss",
        "instruction": (
            "Give the morning briefing. Read the calendar for today, the inbox, and "
            "recent messages — in parallel, one pass. Then report in this order: "
            "(1) anything time-critical in the next few hours, (2) what needs a reply "
            "today and from whom, (3) the day's schedule as a short list, (4) one line "
            "on markets only if they hold positions worth mentioning. Lead with the "
            "single most important item. Skip any section that is empty rather than "
            "saying it is empty. Keep the whole thing under 150 words."
        ),
    },
    {
        "name": "end of day",
        "triggers": "end of day,wrap up,evening summary,how did today go",
        "instruction": (
            "Close out the day. Check what remains unanswered in mail and messages, "
            "and what is on tomorrow's calendar. Report: anything still owed a reply "
            "today, then tomorrow's first commitment and anything needing preparation "
            "tonight. If tomorrow starts early, say so plainly."
        ),
    },
    {
        "name": "book appointment",
        "triggers": "book,make an appointment,schedule me,i need a,find me a",
        "instruction": (
            "Drive this to an actual booking, do not just answer. First establish when "
            "they are free using calendar_find_free. Then find nearby options with "
            "places_search using their device location. Present at most three, with "
            "distance and phone number. Once they choose both a place and a time, "
            "create the calendar event with the place name and full address in the "
            "location field. Do not create anything until both are settled."
        ),
    },
    {
        "name": "market check",
        "triggers": "market check,how are markets,portfolio,my stocks",
        "instruction": (
            "Check the indices (^GSPC, ^IXIC, ^DJI) plus any tickers they hold in "
            "memory. Report each with price and day move. Then one sentence on what "
            "moved most and why, using stock_news if the move is over 3%. Numbers "
            "exact, no rounding into vagueness."
        ),
    },
]


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


async def ensure_builtins() -> None:
    """Install the shipped skills once. Never overwrites an edited copy — if the
    user has changed a builtin, their version is the one they want."""
    async with session_scope() as session:
        existing = {
            row.name for row in (await session.execute(select(Skill))).scalars().all()
        }
        for spec in BUILTIN_SKILLS:
            if spec["name"] in existing:
                continue
            session.add(
                Skill(
                    name=spec["name"],
                    triggers=spec["triggers"],
                    instruction=spec["instruction"],
                    builtin=1,
                )
            )


async def match_skill(message: str) -> Skill | None:
    """Find the skill a message invokes, if any.

    Longest trigger wins. "book appointment" and "book" would both match "book me
    a table", and the more specific phrase is the better signal of intent.
    """
    text = normalise(message)
    if not text:
        return None

    async with session_scope() as session:
        skills = list(
            (await session.execute(select(Skill).where(Skill.enabled == 1))).scalars().all()
        )

    best: tuple[int, Skill] | None = None
    for skill in skills:
        for trigger in skill.trigger_list:
            trigger = normalise(trigger)
            if not trigger:
                continue
            # Word-boundary match so "book" doesn't fire on "bookmark".
            if re.search(rf"\b{re.escape(trigger)}\b", text) and (
                best is None or len(trigger) > best[0]
            ):
                best = (len(trigger), skill)

    if best is None:
        return None

    skill = best[1]
    async with session_scope() as session:
        row = await session.get(Skill, skill.id)
        if row is not None:
            row.uses += 1
            row.last_used = utcnow()
    log.info("Skill matched: %s", skill.name)
    return skill


def skill_preamble(skill: Skill | None) -> str:
    if skill is None:
        return ""
    return (
        f"\n\n## Active skill: {skill.name}\n"
        f"{skill.instruction}\n"
        "Follow this procedure for this turn. If the situation genuinely doesn't fit, "
        "adapt rather than forcing it, and say what you did differently."
    )
