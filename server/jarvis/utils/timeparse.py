"""Natural-language date/time handling.

The model is told to emit ISO-8601 and drifts anyway ("tomorrow at 4", "friday
at 11"), so both have to work.

Handing those phrases to dateutil's fuzzy parser does not work, and fails in the
worst possible way — silently, with a plausible answer. Fuzzy parsing skips words
it doesn't understand rather than objecting, so "tomorrow" is discarded and the
4 is read as a day of the month: "tomorrow at 4" becomes the 4th, which is in
the past for most of any month. "tonight at 8" becomes the 8th at the default
hour. Every one of these produced a real event at a real wrong time.

So relative days are resolved here, explicitly, and only what remains is handed
to dateutil — which is genuinely good at "9 August 2026" and has no business
guessing at "tomorrow".
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Phrases that name a day on their own. Ordered longest-first so "day after
# tomorrow" is matched before "tomorrow".
_DAY_OFFSETS = (
    ("day after tomorrow", 2),
    ("the day after tomorrow", 2),
    ("day before yesterday", -2),
    ("tomorrow", 1),
    ("tonight", 0),
    ("this evening", 0),
    ("this afternoon", 0),
    ("this morning", 0),
    ("yesterday", -1),
    ("today", 0),
)

# Phrases that also say something about the time. The hour is used when none is
# stated ("tonight" alone is 7pm); the period settles a bare one ("tonight at 8"
# is 20:00, not breakfast).
_IMPLIED_HOUR = {
    "tonight": 19, "this evening": 19, "this afternoon": 14, "this morning": 9,
    "noon": 12, "midday": 12, "midnight": 0,
}
_IMPLIED_PERIOD = {
    "tonight": "pm", "this evening": "pm", "this afternoon": "pm", "this morning": "am",
}

_RELATIVE = {"now", "today", "tonight", "tomorrow", "yesterday"}

_IN_PATTERN = re.compile(
    r"\bin\s+(\d+)\s*(minute|min|hour|hr|day|week|month)s?\b", re.IGNORECASE
)

# An explicit am/pm marker, in any of the spellings people actually type.
_MERIDIEM = re.compile(r"\b\d{1,2}(:\d{2})?\s*([ap])\.?\s?m\.?\b", re.IGNORECASE)
# A 24-hour clock time: either an hour past 12, or a zero-padded hour.
_TWENTY_FOUR = re.compile(r"\b(1[3-9]|2[0-3]):[0-5]\d\b|\b0\d:[0-5]\d\b")
# ISO-8601, where the hour is unambiguous by definition.
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}")
_BARE_HOUR = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\b")

_TIME_PATTERN = re.compile(
    r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s?m\.?\b"  # 4pm, 4:30 p.m.
    r"|\b(\d{1,2}):([0-5]\d)\b"                          # 16:30
    r"|\bat\s+(\d{1,2})\b"                               # at 4
    r"|\b(noon|midday|midnight)\b",
    re.IGNORECASE,
)

# Anything that looks like a calendar date. When one is present the text is
# dateutil's job, not ours — "9 August at 2pm" needs no relative-day handling.
_EXPLICIT_DATE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)


def local_now(timezone: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(timezone))


def meridiem_is_ambiguous(text: str) -> int | None:
    """The hour a bare time refers to, when am/pm was never stated.

    Returns None when the text settles it — an am/pm marker, a 24-hour clock, or
    an ISO timestamp — and the hour (1-12) when it does not. "Book it at 2" is
    the case this exists for: 02:00 and 14:00 are both valid readings, and
    guessing silently is how an appointment lands in the middle of the night.
    """
    text = (text or "").strip()
    if not text or _MERIDIEM.search(text) or _TWENTY_FOUR.search(text) or _ISO.search(text):
        return None

    lowered = text.lower()
    if lowered in _RELATIVE or any(word in lowered for word in _IMPLIED_HOUR):
        return None
    # A duration is not a clock reading — "in 2 hours" has no am/pm to settle.
    if _IN_PATTERN.search(lowered):
        return None

    # Strip anything that names a day, so its numbers can't be read as a clock.
    stripped = _strip_day_phrases(lowered)

    for match in _BARE_HOUR.finditer(stripped):
        value = int(match.group(1))
        following = stripped[match.end() : match.end() + 3].lower()
        if following.startswith(("st", "nd", "rd", "th", "/", "-", ":")):
            continue  # an ordinal date, a written date, or a minutes field
        if 1 <= value <= 12:
            return value
    return None


def _strip_day_phrases(text: str) -> str:
    for phrase, _ in _DAY_OFFSETS:
        text = text.replace(phrase, " ")
    for name in _WEEKDAYS:
        text = re.sub(rf"\b(next|this|coming)?\s*{name}\b", " ", text)
    return _EXPLICIT_DATE.sub(" ", text)


def _extract_day(text: str, now: dt.datetime):
    """Pull a relative day out of the text.

    Returns (date, remaining text, implied hour, implied period). The date is
    None when nothing relative was named, which is the signal to let dateutil
    read the whole string.
    """
    for phrase, offset in _DAY_OFFSETS:
        if phrase in text:
            return (
                (now + dt.timedelta(days=offset)).date(),
                text.replace(phrase, " ", 1),
                _IMPLIED_HOUR.get(phrase),
                _IMPLIED_PERIOD.get(phrase),
            )

    # Coarse ranges. calendar_list is asked for "next week" constantly, and
    # dateutil rejects it outright — which surfaced as "could not understand the
    # time" on a phrase anyone would consider ordinary.
    for phrase, delta in (
        ("next week", dt.timedelta(days=7)),
        ("this week", dt.timedelta(0)),
        ("next fortnight", dt.timedelta(days=14)),
    ):
        if phrase in text:
            return (now + delta).date(), text.replace(phrase, " ", 1), None, None
    for phrase, months in (("next month", 1), ("this month", 0)):
        if phrase in text:
            return (
                (now + relativedelta(months=months)).date(),
                text.replace(phrase, " ", 1), None, None,
            )

    for name, index in _WEEKDAYS.items():
        match = re.search(rf"\b(next|this|coming)?\s*{name}\b", text)
        if not match:
            continue
        # Always the next occurrence, and never today. "Book me in for Friday"
        # said on a Friday means the coming one — and resolving it to today
        # would usually put the event in the past, which is the failure this
        # whole module exists to stop.
        ahead = (index - now.weekday()) % 7 or 7
        remainder = text[: match.start()] + text[match.end() :]
        return (now + dt.timedelta(days=ahead)).date(), remainder, None, None

    return None, text, None, None


def _extract_time(text: str) -> tuple[int, int, bool] | None:
    """Pull a time of day out of the text.

    Returns (hour, minute, bare), where `bare` marks an hour given with no am/pm
    — the caller still has to decide which half of the day it belongs to.
    """
    match = _TIME_PATTERN.search(text)
    if not match:
        return None

    if match.group(7):  # noon / midday / midnight
        return (0, 0, False) if match.group(7).lower() == "midnight" else (12, 0, False)

    if match.group(1):  # 4pm, 4:30 pm
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if meridiem == "p" and hour < 12:
            hour += 12
        elif meridiem == "a" and hour == 12:
            hour = 0
        return hour, minute, False

    if match.group(4):  # 16:30
        return int(match.group(4)), int(match.group(5)), False

    if match.group(6):  # at 4 — half of the day still unknown
        return int(match.group(6)), 0, True

    return None


def parse_when(value: str, timezone: str, *, default_hour: int = 9) -> dt.datetime:
    """Parse a user- or model-supplied time into an aware datetime in `timezone`."""
    tz = ZoneInfo(timezone)
    now = dt.datetime.now(tz)
    text = (value or "").strip()
    if not text:
        return now

    # ISO first and strictly: it's what the model is asked for, it is never
    # ambiguous, and no amount of natural-language guessing can improve on it.
    try:
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        pass

    lowered = text.lower()
    if lowered == "now":
        return now

    match = _IN_PATTERN.search(lowered)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        unit = {"min": "minute", "hr": "hour"}.get(unit, unit)
        return now + relativedelta(**{f"{unit}s": amount})

    # An explicit calendar date is dateutil's strength; don't second-guess it.
    if not _EXPLICIT_DATE.search(lowered):
        day, remainder, implied_hour, period = _extract_day(lowered, now)
        clock = _extract_time(remainder if day is not None else lowered)

        if day is not None or clock is not None:
            base = day or now.date()
            if clock is None:
                hour, minute = (implied_hour if implied_hour is not None else default_hour), 0
            else:
                hour, minute, bare = clock
                # "tonight at 8" is 20:00. The phrase already said which half of
                # the day it meant; only the digits were unqualified.
                if bare and period == "pm" and hour < 12:
                    hour += 12
                elif bare and period == "am" and hour == 12:
                    hour = 0
            return dt.datetime(base.year, base.month, base.day, hour, minute, tzinfo=tz)

    try:
        parsed = dateparser.parse(
            text, fuzzy=True,
            default=now.replace(hour=default_hour, minute=0, second=0, microsecond=0),
        )
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Could not understand the time '{value}'.") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def day_bounds(when: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def humanise(when: dt.datetime, timezone: str) -> str:
    local = when.astimezone(ZoneInfo(timezone))
    return local.strftime("%a %d %b, %-I:%M %p") if local.minute or local.hour else local.strftime(
        "%a %d %b"
    )
