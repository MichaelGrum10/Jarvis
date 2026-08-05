"""Introspection: what time is it, what's wired up, what can I actually do."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ..config import get_settings
from .base import ToolContext, ToolResult, registry


@registry.tool(
    name="current_time",
    description=(
        "Get the current date and time in the user's timezone. Call this before any "
        "reasoning about 'today', 'tomorrow', 'this week' or scheduling — never assume "
        "the date from your training data."
    ),
    parameters={"type": "object", "properties": {}},
    tags=["system"],
)
async def current_time(ctx: ToolContext = None):
    tz_name = (ctx.timezone if ctx else None) or get_settings().timezone
    now = dt.datetime.now(ZoneInfo(tz_name))
    return ToolResult.success(
        {
            "iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "day_of_week": now.strftime("%A"),
            "timezone": tz_name,
            "friendly": now.strftime("%A %d %B %Y, %-I:%M %p"),
        }
    )


@registry.tool(
    name="system_status",
    description=(
        "Report which integrations are configured and which are missing credentials. Use "
        "when the user asks what you can do, or when a capability isn't working and you "
        "need to tell them exactly which setting to fill in."
    ),
    parameters={"type": "object", "properties": {}},
    tags=["system"],
)
async def system_status():
    from .base import registry as reg

    settings = get_settings()
    features = {}
    for feature in ("llm", "mail", "calendar", "messages", "auth"):
        missing = settings.missing_for(feature)
        features[feature] = {
            "configured": not missing,
            "missing_env_vars": missing,
        }
    features["search"] = {
        "configured": True,
        "backend": "searxng" if settings.searxng_url else ("brave" if settings.brave_api_key else "duckduckgo"),
    }
    features["stocks"] = {"configured": True, "backend": "yfinance"}
    features["news"] = {"configured": True, "backend": "wsj public rss"}
    features["places"] = {"configured": True, "backend": "openstreetmap"}
    features["autonomy"] = {"configured": settings.autonomy_enabled}

    return ToolResult.success(
        {
            "features": features,
            "active_tools": sorted(t.name for t in reg.available(settings)),
            "timezone": settings.timezone,
        }
    )
