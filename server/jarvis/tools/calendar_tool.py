"""Calendar tools: read the schedule, book things, cancel things."""

from __future__ import annotations

import datetime as dt

from ..config import get_settings
from ..integrations.apple_calendar import get_calendar
from ..utils.meridiem import clarification_question, resolve_meridiem
from ..utils.timeparse import local_now, meridiem_is_ambiguous, parse_when
from . import recent_events
from .base import ToolContext, ToolResult, registry

# A booking a minute or two old is a clock difference, not a request to write
# into the past. Anything older was meant differently.
PAST_GRACE = dt.timedelta(minutes=2)


def _apply_meridiem(start_text: str, start_dt: dt.datetime, title: str):
    """Fix a bare hour to morning or afternoon, or report that it can't be.

    Returns (datetime, note, question). Exactly one of `note` and `question` is
    ever set: a note when the reading was inferred and should be stated back, a
    question when it has to be asked instead.
    """
    hour = meridiem_is_ambiguous(start_text)
    if hour is None:
        return start_dt, "", ""

    resolved, reason = resolve_meridiem(hour, title)
    if resolved is None:
        return start_dt, "", clarification_question(hour, title)

    if start_dt.hour == resolved:
        return start_dt, "", ""
    return (
        start_dt.replace(hour=resolved),
        f"Read '{start_text.strip()}' as {resolved % 12 or 12} "
        f"{'PM' if resolved >= 12 else 'AM'} — {reason}. Say so when confirming.",
        "",
    )


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
    # Only when the range picked out a handful. Remembering a whole week would
    # make "it" ambiguous rather than resolvable, which is the opposite of the point.
    if len(payload) <= 3:
        for item in payload:
            recent_events.remember(ctx.conversation_id if ctx else None, item, "read")
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
            "allow_past": {
                "type": "boolean",
                "description": (
                    "Set true ONLY when the user has confirmed they want an event in the "
                    "past, e.g. logging something that already happened. Never set it to "
                    "get past a rejection without asking them first."
                ),
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
    allow_past: bool = False,
    ctx: ToolContext = None,
):
    tz = ctx.timezone if ctx else get_settings().timezone
    start_dt = parse_when(start, tz)

    start_dt, note, question = _apply_meridiem(start, start_dt, title)
    if question:
        # Refused rather than guessed. The model sees why, asks, and calls again
        # with an unambiguous time — one extra exchange instead of an
        # appointment silently twelve hours out.
        return ToolResult.fail(
            f"Ambiguous time: '{start.strip()}' could be AM or PM. Ask the user: {question} "
            "Then call calendar_create again with an explicit time."
        )

    if not allow_past and start_dt < local_now(tz) - PAST_GRACE:
        return ToolResult.fail(
            f"That start time ({start_dt.strftime('%a %d %b, %-I:%M %p')}) is in the past. "
            "The user probably meant a later date — check which one they want. "
            "If they genuinely want to record something that already happened, "
            "call again with allow_past=true."
        )

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
    recent_events.remember(ctx.conversation_id if ctx else None, event.to_dict(), "created")
    payload: dict = {"created": event.to_dict()}
    if note:
        payload["interpretation"] = note
    return ToolResult.success(
        payload,
        display={"type": "calendar_created", "event": event.to_dict()},
    )


@registry.tool(
    name="calendar_update",
    description=(
        "Change an existing calendar event — move it, rename it, change where it is. Use "
        "this whenever the user amends something already booked ('make it 4 instead', "
        "'move the dentist to Friday'). Only the fields you pass are changed. If the event "
        "was created earlier in this conversation its uid is in your system prompt; "
        "otherwise call calendar_list to find it. Prefer this over deleting and recreating, "
        "which loses invitees and alerts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "Event uid."},
            "title": {"type": "string", "description": "New title. Omit to keep."},
            "start": {"type": "string", "description": "New start time. Omit to keep."},
            "duration_minutes": {
                "type": "integer",
                "description": "New length in minutes, applied from the start time.",
            },
            "end": {"type": "string", "description": "New end time; overrides duration."},
            "location": {"type": "string", "description": "New location. Omit to keep."},
            "notes": {"type": "string", "description": "New notes. Omit to keep."},
            "calendar": {"type": "string", "description": "Calendar the event lives on."},
            "allow_past": {
                "type": "boolean",
                "description": "Only with the user's confirmation, as for calendar_create.",
            },
        },
        "required": ["uid"],
    },
    requires="calendar",
    tags=["calendar", "write"],
)
async def calendar_update(
    uid: str,
    title: str = "",
    start: str = "",
    duration_minutes: int | None = None,
    end: str = "",
    location: str = "",
    notes: str = "",
    calendar: str = "",
    allow_past: bool = False,
    ctx: ToolContext = None,
):
    tz = ctx.timezone if ctx else get_settings().timezone
    start_dt = end_dt = None
    note = ""

    if start:
        start_dt = parse_when(start, tz)
        start_dt, note, question = _apply_meridiem(start, start_dt, title)
        if question:
            return ToolResult.fail(
                f"Ambiguous time: '{start.strip()}' could be AM or PM. Ask the user: {question} "
                "Then call calendar_update again with an explicit time."
            )
        if not allow_past and start_dt < local_now(tz) - PAST_GRACE:
            return ToolResult.fail(
                f"That start time ({start_dt.strftime('%a %d %b, %-I:%M %p')}) is in the past. "
                "Check which date they meant, or call again with allow_past=true if they "
                "confirmed they want it there."
            )

    if end:
        end_dt = parse_when(end, tz)
    elif duration_minutes and start_dt:
        end_dt = start_dt + dt.timedelta(minutes=max(duration_minutes, 5))
    if start_dt and end_dt and end_dt <= start_dt:
        end_dt = start_dt + dt.timedelta(minutes=max(duration_minutes or 60, 5))

    event = await get_calendar().update_event(
        uid,
        summary=title or None,
        start=start_dt,
        end=end_dt,
        location=location or None,
        description=notes or None,
        calendar=calendar,
    )
    recent_events.remember(ctx.conversation_id if ctx else None, event.to_dict(), "updated")
    payload: dict = {"updated": event.to_dict()}
    if note:
        payload["interpretation"] = note
    return ToolResult.success(
        payload, display={"type": "calendar_created", "event": event.to_dict()}
    )


@registry.tool(
    name="calendar_delete",
    description=(
        "Delete an event from the Apple Calendar by its uid. If the event was created "
        "earlier in this conversation its uid is in your system prompt; otherwise call "
        "calendar_list first, and confirm which event they mean if more than one matches. "
        "To change an event rather than cancel it, use calendar_update instead."
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
async def calendar_delete(uid: str, calendar: str = "", ctx: ToolContext = None):
    await get_calendar().delete_event(uid, calendar)
    recent_events.forget(ctx.conversation_id if ctx else None, uid)
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
