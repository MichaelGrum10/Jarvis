"""Acting on the user's device.

A server in Oracle Cloud cannot launch an app on your iPhone. What it can do is
queue an instruction that the Jarvis app — already open on that device — picks up
and executes locally. Opening an app becomes a URL-scheme navigation; anything
richer becomes an Apple Shortcut you've defined once on the device.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from ..db import Device, DeviceCommand, session_scope
from .base import ToolContext, ToolResult, registry

# URL schemes that work on Apple platforms. Extend freely — it's just a lookup.
APP_SCHEMES: dict[str, str] = {
    "spotify": "spotify://",
    "music": "music://",
    "apple music": "music://",
    "podcasts": "podcasts://",
    "maps": "maps://",
    "apple maps": "maps://",
    "google maps": "comgooglemaps://",
    "mail": "message://",
    "messages": "sms://",
    "facetime": "facetime://",
    "phone": "tel://",
    "calendar": "calshow://",
    "notes": "mobilenotes://",
    "reminders": "x-apple-reminderkit://",
    "photos": "photos-redirect://",
    "settings": "App-Prefs://",
    "safari": "https://",
    "youtube": "youtube://",
    "netflix": "nflx://",
    "slack": "slack://",
    "whatsapp": "whatsapp://",
    "instagram": "instagram://",
    "twitter": "twitter://",
    "x": "twitter://",
    "zoom": "zoomus://",
    "chrome": "googlechrome://",
    "wsj": "wsj://",
    "shortcuts": "shortcuts://",
}


async def _queue(device_id: str, kind: str, payload: dict) -> int:
    async with session_scope() as session:
        command = DeviceCommand(device_id=device_id, kind=kind, payload=json.dumps(payload))
        session.add(command)
        await session.flush()
        return command.id


@registry.tool(
    name="device_open_app",
    description=(
        "Open an app on the user's device. The app launches on whichever device they are "
        "currently using Jarvis from. Use for 'open Spotify', 'pull up Maps', etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "App name. Known: " + ", ".join(sorted(APP_SCHEMES)),
            },
            "query": {
                "type": "string",
                "description": "Optional text to pass through, e.g. a search term or address.",
            },
        },
        "required": ["app"],
    },
    tags=["device"],
)
async def device_open_app(app: str, query: str = "", ctx: ToolContext = None):
    if not ctx or not ctx.device_id:
        return ToolResult.fail("No device is registered for this session.")

    key = app.lower().strip()
    scheme = APP_SCHEMES.get(key)
    if scheme is None:
        return ToolResult.fail(
            f"I don't have a URL scheme for '{app}'. Known apps: {', '.join(sorted(APP_SCHEMES))}. "
            "You can also ask me to run an Apple Shortcut instead."
        )

    url = scheme
    if query:
        from urllib.parse import quote

        if key in ("maps", "apple maps"):
            url = f"maps://?q={quote(query)}"
        elif key == "google maps":
            url = f"comgooglemaps://?q={quote(query)}"
        elif key in ("spotify", "music", "apple music"):
            url = f"{scheme}search:{quote(query)}"
        elif key == "youtube":
            url = f"youtube://results?search_query={quote(query)}"
        elif key == "safari":
            url = query if query.startswith("http") else f"https://duckduckgo.com/?q={quote(query)}"
        else:
            url = f"{scheme}{quote(query)}"

    job = await _queue(ctx.device_id, "open_app", {"app": app, "url": url})
    return ToolResult.success(
        {"queued": True, "job_id": job, "app": app, "url": url},
        display={"type": "device_action", "action": f"Opening {app}", "url": url},
    )


@registry.tool(
    name="device_open_url",
    description="Open a web page or deep link on the user's current device.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL or deep link to open."},
            "reason": {"type": "string", "description": "One line on why, shown to the user."},
        },
        "required": ["url"],
    },
    tags=["device"],
)
async def device_open_url(url: str, reason: str = "", ctx: ToolContext = None):
    if not ctx or not ctx.device_id:
        return ToolResult.fail("No device is registered for this session.")
    job = await _queue(ctx.device_id, "open_url", {"url": url, "reason": reason})
    return ToolResult.success(
        {"queued": True, "job_id": job, "url": url},
        display={"type": "device_action", "action": reason or "Opening link", "url": url},
    )


@registry.tool(
    name="device_run_shortcut",
    description=(
        "Run an Apple Shortcut by name on the user's device. This is the escape hatch for "
        "anything iOS/macOS can do that has no direct API — controlling HomeKit, setting a "
        "timer, toggling a focus mode, playing a specific playlist. The Shortcut must already "
        "exist on their device with exactly this name."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact Shortcut name as saved on the device."},
            "input": {"type": "string", "description": "Optional text passed to the Shortcut."},
        },
        "required": ["name"],
    },
    tags=["device"],
)
async def device_run_shortcut(name: str, input: str = "", ctx: ToolContext = None):
    if not ctx or not ctx.device_id:
        return ToolResult.fail("No device is registered for this session.")
    from urllib.parse import quote

    url = f"shortcuts://run-shortcut?name={quote(name)}"
    if input:
        url += f"&input=text&text={quote(input)}"
    job = await _queue(ctx.device_id, "shortcut", {"name": name, "url": url, "input": input})
    return ToolResult.success(
        {"queued": True, "job_id": job, "shortcut": name},
        display={"type": "device_action", "action": f"Running shortcut '{name}'", "url": url},
    )


@registry.tool(
    name="device_list",
    description="List the devices the user has authorised, and where each was last seen.",
    parameters={"type": "object", "properties": {}},
    tags=["device"],
)
async def device_list():
    async with session_scope() as session:
        rows = list((await session.execute(select(Device))).scalars().all())
    return ToolResult.success(
        {
            "devices": [
                {
                    "label": d.label,
                    "platform": d.platform,
                    "last_seen": d.last_seen.isoformat(),
                    "has_location": d.lat is not None,
                    "location_at": d.location_at.isoformat() if d.location_at else None,
                }
                for d in rows
            ]
        }
    )
