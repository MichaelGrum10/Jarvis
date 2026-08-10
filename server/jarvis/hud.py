"""What the HUD displays, assembled without going near the model.

This is the point of the whole design. A glanceable dashboard that refreshed
through the agent would spend a full turn — system prompt, tool schemas, several
round-trips — every sixty seconds, on a per-minute token budget that a single
booking already strains. The HUD calls the integrations directly and reads the
same cache the tools use, so it costs nothing the model can feel.

Failure is shown, never hidden. A section that cannot refresh keeps its last
known value and reports how old it is, because a blank panel and a stale one
look identical while meaning opposite things, and invented data is worse than
either.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from . import cache
from .config import get_settings
from .quotes import quote_of_the_day

log = logging.getLogger(__name__)

# Independent of the tool cache TTLs: a person glances at a HUD, they do not
# interrogate it, so these follow the brief rather than the tools.
PANEL_TTL = {"markets": 60.0, "calendar": 300.0, "inbox": 120.0, "messages": 120.0}

# How long a panel may keep showing its last value before it stops being useful.
STALE_LIMIT = 3600.0


async def _panel(name: str, build) -> dict[str, Any]:
    """Run one panel's fetch, falling back to its last good value.

    Each panel is isolated: an iCloud outage must not blank the markets, and a
    market holiday must not hide the calendar.
    """
    fresh = cache.get(f"hud_{name}", "current")
    if fresh is not None:
        return {"status": "ok", "age": 0, **fresh}

    try:
        value = await build()
    except Exception as exc:
        log.warning("HUD panel %s failed: %s", name, exc)
        last = cache.get(f"hud_{name}_last", "current")
        if last is None:
            return {"status": "down", "error": str(exc)[:120], "age": None}
        age = time.time() - last.get("_at", 0)
        if age > STALE_LIMIT:
            return {"status": "down", "error": str(exc)[:120], "age": int(age)}
        # Amber: real data, plainly labelled with its age.
        return {"status": "stale", "age": int(age), **{k: v for k, v in last.items() if k != "_at"}}

    cache.put(f"hud_{name}", "current", value)
    cache.put(f"hud_{name}_last", "current", {**value, "_at": time.time()})
    return {"status": "ok", "age": 0, **value}


async def _markets() -> dict:
    from .tools.stocks_tool import _quote_sync

    settings = get_settings()
    symbols = [s.strip().upper() for s in settings.watchlist.split(",") if s.strip()]
    indices = ["^GSPC", "^IXIC", "^DJI"]

    settled = await asyncio.gather(
        *(asyncio.to_thread(_quote_sync, s) for s in symbols + indices),
        return_exceptions=True,
    )
    quotes = [q for q in settled if not isinstance(q, BaseException)]

    watch = [q for q in quotes if q.get("symbol") in symbols]
    index = [q for q in quotes if q.get("symbol") in indices]
    movers = sorted(watch, key=lambda q: abs(q.get("change_percent") or 0), reverse=True)[:3]
    # Anything moving hard enough that you would want to know without asking.
    alerts = [q for q in watch if abs(q.get("change_percent") or 0) >= 5]

    return {"watchlist": watch, "indices": index, "movers": movers, "alerts": alerts}


async def _calendar() -> dict:
    from .integrations.apple_calendar import get_calendar

    settings = get_settings()
    now = dt.datetime.now(ZoneInfo(settings.timezone))
    events = await get_calendar().events_between(now, now + dt.timedelta(days=2))

    upcoming = [e.to_dict() for e in events if e.start >= now.isoformat()][:3]
    soon = (now + dt.timedelta(hours=1)).isoformat()
    for event in upcoming:
        event["imminent"] = event["start"] <= soon
    return {"events": upcoming}


async def _inbox() -> dict:
    from .integrations.apple_mail import get_mail

    recent = await get_mail().recent(limit=5, unread_only=True, days=3)
    return {
        "messages": [
            {"from": (m.sender or m.sender_email)[:40], "subject": (m.subject or "")[:50]}
            for m in recent
        ][:5]
    }


async def _messages() -> dict:
    from sqlalchemy import select

    from .db import ChatMessage, session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.is_from_me == 0)
                .order_by(ChatMessage.sent_at.desc()).limit(5)
            )
        ).scalars().all()
    return {
        "messages": [
            {"from": r.sender_name or r.sender or "unknown", "text": (r.text or "")[:50]}
            for r in rows
        ]
    }


async def snapshot() -> dict:
    """Everything the HUD shows, in one call."""
    settings = get_settings()
    now = dt.datetime.now(ZoneInfo(settings.timezone))

    panels = ("markets", "calendar", "inbox", "messages")
    builders = (_markets, _calendar, _inbox, _messages)
    # Gathered, because four sequential network calls is exactly the latency a
    # glanceable dashboard cannot afford.
    results = await asyncio.gather(
        *(_panel(name, build) for name, build in zip(panels, builders, strict=True))
    )

    return {
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%a %d %b"),
        "quote": quote_of_the_day(now.date()),
        **dict(zip(panels, results, strict=True)),
    }
