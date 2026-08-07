"""Reading pages a plain HTTP fetch can't: rendered, and signed in."""

from __future__ import annotations

import datetime as dt

from ..config import get_settings
from ..integrations.browser import (
    BrowserError,
    fence_untrusted,
    get_browser,
    session_summary,
)
from .base import ToolResult, registry


@registry.tool(
    name="browse_page",
    description=(
        "Open a URL in a real browser and read it — including JavaScript-rendered pages "
        "and subscriber-only articles on sites where a session has been imported. Use this "
        "when web_read returns a teaser, a paywall notice, or almost nothing, and when the "
        "user asks you to open or read a specific article. Prefer web_read for ordinary "
        "pages: it is much faster."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL including https://."},
            "max_chars": {
                "type": "integer",
                "description": "Truncate the text at this length. Default 8000.",
            },
        },
        "required": ["url"],
    },
    requires="browser",
    tags=["search"],
)
async def browse_page(url: str, max_chars: int = 0):
    settings = get_settings()
    limit = max_chars or settings.browser_max_chars

    try:
        page = await get_browser().read(url, max_chars=limit)
    except BrowserError as exc:
        return ToolResult.fail(str(exc))

    # A paywall usually returns a real page with a short teaser, so an empty-ish
    # body is the signal — not an error status. Saying which of the two cases it
    # is turns "that didn't work" into something the user can act on.
    thin = len(page.text) < 600
    if thin and not page.used_session:
        return ToolResult.fail(
            f"Only got {len(page.text)} characters from {url} — this looks like a paywall or "
            "a sign-in wall, and no browser session is stored for that site. To read it, "
            "import your session: see docs/browser.md."
        )
    if thin:
        return ToolResult.fail(
            f"Only got {len(page.text)} characters from {url}, despite using the stored "
            "session. The session may have expired — check with browser_session_status, "
            "and re-import if so."
        )

    return ToolResult.success(
        {
            "url": page.url,
            "title": page.title,
            "signed_in": page.used_session,
            "truncated": page.truncated,
            "content": fence_untrusted(page.text),
        },
        display={"type": "article", "url": page.url, "title": page.title},
    )


@registry.tool(
    name="browser_session_status",
    description=(
        "Check which sites Jarvis has a stored browser session for, and when it expires. "
        "Use this when a subscriber-only page fails to load, to tell the user whether "
        "their session has lapsed."
    ),
    parameters={"type": "object", "properties": {}},
    requires="browser",
    tags=["search"],
)
async def browser_session_status():
    summary = session_summary()
    if not summary["present"]:
        return ToolResult.success({
            "session": False,
            "note": "No browser session stored. Subscriber-only pages will show a teaser. "
                    "The user can import one — see docs/browser.md.",
        })

    expires = summary["expires"]
    when = dt.datetime.fromtimestamp(expires, dt.UTC).isoformat() if expires else None
    days = round((expires - dt.datetime.now(dt.UTC).timestamp()) / 86400, 1) if expires else None

    return ToolResult.success({
        "session": True,
        "sites": summary["domains"],
        "cookies": summary["cookies"],
        "earliest_expiry": when,
        "days_until_expiry": days,
        "note": (
            "Expired — the user needs to re-import from docs/browser.md."
            if days is not None and days <= 0
            else "Active."
        ),
    })


def browser_available() -> bool:
    """Whether these tools should be offered at all."""
    return get_settings().browser_enabled


def session_note() -> str:
    """A line for the system prompt, so the model knows what it can open."""
    if not get_settings().browser_enabled:
        return ""
    summary = session_summary()
    if not summary["present"]:
        return (
            "\n\n## Browser\nYou can open and read JavaScript-rendered pages with "
            "browse_page. No signed-in session is stored, so subscriber-only articles "
            "will still show only a teaser.\n"
        )
    sites = ", ".join(summary["domains"][:8]) or "none"
    return (
        "\n\n## Browser\nYou can open and read pages with browse_page, including "
        f"subscriber-only articles on: {sites}. When the user asks you to open, read or "
        "expand an article on one of those, use browse_page with the article URL rather "
        "than saying it is paywalled.\n"
    )
