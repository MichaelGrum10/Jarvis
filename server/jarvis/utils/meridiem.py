"""Deciding whether a bare hour means morning or afternoon.

"Book me a haircut at 2" means 2 PM — nobody cuts hair at two in the morning.
"Put a meeting in at 8" means either, and picking one silently is worse than
asking, because the wrong answer is a missed meeting rather than a visible error.

So this resolves only where the evidence is strong, and reports uncertainty
everywhere else. Two kinds of evidence:

  the hour itself   1-5 has no plausible morning reading for an appointment;
                    12 is noon; 6-11 genuinely reads both ways
  the activity      dinner is evening, breakfast is morning, and a dentist
                    keeps business hours

Ambiguity is resolved in favour of asking. A clarifying question costs one
exchange; a meeting entered twelve hours out is missed entirely.
"""

from __future__ import annotations

# Activities that fix the half of the day on their own, whatever the hour.
_EVENING = (
    "dinner", "supper", "drinks", "cocktail", "party", "movie", "cinema", "concert",
    "gig", "show", "theatre", "theater", "bedtime", "nightcap", "evening",
)
_MORNING = (
    "breakfast", "sunrise", "standup", "stand-up", "school run", "morning",
    "commute", "flight check-in",
)

# Activities that keep business hours. These don't fix the half of the day by
# themselves, but they rule out the small hours and the late evening, which is
# enough to read 1-6 as afternoon and 7-11 as morning.
_BUSINESS = (
    "haircut", "barber", "hairdresser", "salon", "dentist", "doctor", "gp",
    "clinic", "physio", "physiotherapy", "massage", "optician", "optometrist",
    "vet", "appointment", "checkup", "check-up", "consultation", "lunch",
    "coffee", "viewing", "inspection", "dmv", "bank", "lawyer", "accountant",
)

# Activities where both readings are real and being wrong is expensive. These
# are never guessed, at any hour where both readings exist.
_ALWAYS_ASK = (
    "meeting", "call", "sync", "interview", "flight", "train", "bus", "shift",
    "class", "lesson", "exam", "deadline", "webinar", "presentation", "review",
)


def _mentions(title: str, words) -> bool:
    lowered = f" {title.lower()} "
    return any(word in lowered for word in words)


def resolve_meridiem(hour: int, title: str) -> tuple[int | None, str]:
    """Map a bare 1-12 hour to a 24-hour hour, or return None to ask.

    Returns (hour_24, reason). `reason` explains the reading in plain words so
    the assistant can state it back — "2 PM, since a haircut at 2 AM seemed
    unlikely" — which is what lets the user catch a wrong guess immediately.
    """
    if not 1 <= hour <= 12:
        return None, ""

    title = title or ""

    if _mentions(title, _EVENING):
        return (hour + 12 if hour < 12 else 12), "an evening activity"
    if _mentions(title, _MORNING):
        return (hour if hour < 12 else 0), "a morning activity"

    # Noon reads as midday for essentially every appointment; midnight is
    # written as 00:00 or "midnight" by anyone who means it.
    if hour == 12:
        return 12, "midday"

    ask_always = _mentions(title, _ALWAYS_ASK)

    # 1-5 AM is not a time anyone books anything, so the afternoon reading is
    # safe even for a meeting — the guess this rules out is the absurd one.
    if 1 <= hour <= 5:
        return hour + 12, f"{hour} AM being an implausible hour for this"

    if ask_always:
        return None, ""

    if _mentions(title, _BUSINESS):
        # Within business hours, 6-11 can only sensibly be the morning.
        return hour, "business hours"

    # 6-11 with nothing to go on: both readings are ordinary. Ask.
    return None, ""


def clarification_question(hour: int, title: str) -> str:
    """What to ask when the hour can't be resolved."""
    subject = f"the {title.strip()}" if title.strip() else "it"
    return f"Did you mean {hour} AM or {hour} PM for {subject}?"
