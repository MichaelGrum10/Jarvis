"""Natural-language date/time handling.

The model is told to emit ISO-8601, but it drifts ("tomorrow 3pm", "next friday").
Rather than fight it, we accept both: try ISO, then dateutil's fuzzy parser
anchored on the user's local now.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta

_RELATIVE = {
    "now": lambda now: now,
    "today": lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0),
    "tonight": lambda now: now.replace(hour=19, minute=0, second=0, microsecond=0),
    "tomorrow": lambda now: (now + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ),
    "yesterday": lambda now: (now - dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ),
}

_IN_PATTERN = re.compile(
    r"in\s+(\d+)\s*(minute|min|hour|hr|day|week|month)s?", re.IGNORECASE
)


def local_now(timezone: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(timezone))


def parse_when(value: str, timezone: str, *, default_hour: int = 9) -> dt.datetime:
    """Parse a user- or model-supplied time into an aware datetime in `timezone`."""
    tz = ZoneInfo(timezone)
    now = dt.datetime.now(tz)
    text = (value or "").strip()
    if not text:
        return now

    key = text.lower()
    if key in _RELATIVE:
        return _RELATIVE[key](now)

    match = _IN_PATTERN.search(key)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        unit = {"min": "minute", "hr": "hour"}.get(unit, unit)
        return now + relativedelta(**{f"{unit}s": amount})

    try:
        parsed = dateparser.parse(text, fuzzy=True, default=now.replace(
            hour=default_hour, minute=0, second=0, microsecond=0
        ))
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
