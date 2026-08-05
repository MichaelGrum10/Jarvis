"""Calendar tools: read the schedule, book things, cancel things."""

from __future__ import annotations

import datetime as dt

from ..config import get_settings
from ..integrations.apple_calendar import get_calendar
from ..utils.timeparse import parse_when
from .base import ToolContext, ToolResult, registry


@registry.tool(
    name="calendar_list",
    description=(
        "Read events from the user's Apple Calendar over a date range. Use this for any "
        "question about their schedule, what they have on, whether they are free, or when "
        "something is happening. Always call this before claiming the user is free."
    ),
    parameters={
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "Start of range. ISO-8601 or natural language ('today', 'next monday').",
            },
            "end": {
                "type": "string",
                "description": "End of range. Defaults to 7 days after start.",
            },
            "calendar": {"type": "string", "description": "Optional calendar name to restrict to."},
        },
        "required": ["start"],
    },
    requires="calendar",
    tags=["calendar"],
)
async def calendar_list(start: str, end: str = "", calendar: str = "", ctx: ToolContext = None):
    tz = ctx.timezone if ctx else get_settings().timezone
    start_dt = parse_when(start, tz, default_hour=0)
    end_dt = parse_when(end, tz, default_hour=23) if end else start_dt + dt.timedelta(days=7)
    if end_dt <= start_dt:
        end_dt = start_dt + dt.timedelta(days=1)

    events = await get_calendar().events_between(start_dt, end_dt, calendar)
    payload = [e.to_dict() for e in events]
    return ToolResult.success(
        {
            "range": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "count": len(payload),
            "events": payload,
        },
        display={"type": "calendar", "events": payload},
    )


@registry.tool(
    name="calendar_create",
    description=(
        "Create an event in the user's Apple Calendar. Before calling, you must know the "
        "title, date and start time. If the user gave a vague request like 'book me a "
        "haircut', first find a place (places_search) and confirm a specific time with them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": "Start time, ISO-8601 preferred."},
            "duration_minutes": {
                "type": "integer",
                "description": "Length in minutes. Default 60.",
            },
            "end": {"type": "string", "description": "Explicit end time; overrides duration."},
            "location": {"type": "string", "description": "Address or place name."},
            "notes": {"type": "string", "description": "Description/notes on the event."},
            "calendar": {"type": "string", "description": "Which calendar to add it to."},
            "reminder_minutes": {
                "type": "integer",
                "description": "Alert this many minutes before. Omit for no alert.",
            },
        },
        "required": ["title", "start"],
    },
    requires="calendar",
    tags=["calendar", "write"],
)
async def calendar_create(
    title: str,
    start: str,
    duration_minutes: int = 60,
    end: str = "",
    location: str = "",
    notes: str = "",
    calendar: str = "",
    reminder_minutes: int | None = None,
    ctx: ToolContext = None,
):
    tz = ctx.timezone if ctx else get_settings().timezone
    start_dt = parse_when(start, tz)
    end_dt = parse_when(end, tz) if end else start_dt + dt.timedelta(minutes=max(duration_minutes, 5))
    if end_dt <= start_dt:
        end_dt = start_dt + dt.timedelta(minutes=max(duration_minutes, 5))

    event = await get_calendar().create_event(
        title,
        start_dt,
        end_dt,
        location=location,
        description=notes,
        calendar=calendar,
        alarm_minutes=reminder_minutes,
    )
    return ToolResult.success(
        {"created": event.to_dict()},
        display={"type": "calendar_created", "event": event.to_dict()},
    )


@registry.tool(
    name="calendar_delete",
    description=(
        "Delete an event from the Apple Calendar by its uid. Call calendar_list first to "
        "get the uid, and confirm with the user which event they mean if more than one matches."
    ),
    parameters={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "Event uid from calendar_list."},
            "calendar": {"type": "string", "description": "Calendar the event lives on."},
        },
        "required": ["uid"],
    },
    requires="calendar",
    confirm=True,
    tags=["calendar", "write", "destructive"],
)
async def calendar_delete(uid: str, calendar: str = ""):
    await get_calendar().delete_event(uid, calendar)
    return ToolResult.success({"deleted": uid})


@registry.tool(
    name="calendar_find_free",
    description=(
        "Find free time slots in the user's calendar on a given day, respecting working "
        "hours. Use this when booking an appointment so you can offer real options."
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "The day to check."},
            "duration_minutes": {"type": "integer", "description": "Slot length needed. Default 60."},
            "earliest_hour": {"type": "integer", "description": "Earliest hour, 24h. Default 9."},
            "latest_hour": {"type": "integer", "description": "Latest end hour, 24h. Default 19."},
        },
        "required": ["date"],
    },
    requires="calendar",
    tags=["calendar"],
)
async def calendar_find_free(
    date: str,
    duration_minutes: int = 60,
    earliest_hour: int = 9,
    latest_hour: int = 19,
    ctx: ToolContext = None,
):
    tz = ctx.timezone if ctx else get_settings().timezone
    day = parse_when(date, tz, default_hour=0)
    window_start = day.replace(hour=earliest_hour, minute=0, second=0, microsecond=0)
    window_end = day.replace(hour=latest_hour, minute=0, second=0, microsecond=0)

    events = await get_calendar().events_between(window_start, window_end)
    busy: list[tuple[dt.datetime, dt.datetime]] = []
    for event in events:
        if event.all_day:
            continue
        try:
            busy.append((dt.datetime.fromisoformat(event.start), dt.datetime.fromisoformat(event.end)))
        except ValueError:
            continue
    busy.sort()

    slots = []
    cursor = window_start
    need = dt.timedelta(minutes=duration_minutes)
    for busy_start, busy_end in busy:
        if busy_start - cursor >= need:
            slots.append({"start": cursor.isoformat(), "end": busy_start.isoformat()})
        cursor = max(cursor, busy_end)
    if window_end - cursor >= need:
        slots.append({"start": cursor.isoformat(), "end": window_end.isoformat()})

    return ToolResult.success(
        {"date": day.date().isoformat(), "duration_minutes": duration_minutes, "free_slots": slots}
    )
