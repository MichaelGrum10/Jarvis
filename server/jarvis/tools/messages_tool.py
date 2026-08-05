"""iMessage / SMS tools.

Apple ships no server-side API for Messages, and there is no way to read iMessage
from a Linux box. What *does* work is a small agent on a Mac you own, which reads
the local `chat.db` and mirrors new messages up to this server. These tools read
that mirror; sending queues a job the bridge picks up and delivers via AppleScript.

If the bridge has not checked in recently, every tool here says so plainly rather
than pretending the inbox is empty.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from ..config import get_settings
from ..db import BridgeHeartbeat, ChatMessage, OutboundMessage, session_scope, utcnow
from .base import ToolResult, registry

_NOISE_SENDERS = ("verify", "no-reply", "noreply", "info@", "alerts@")


async def _bridge_state() -> tuple[bool, str]:
    settings = get_settings()
    async with session_scope() as session:
        row = await session.execute(select(BridgeHeartbeat).limit(1))
        beat = row.scalar_one_or_none()
    if beat is None:
        return False, (
            "The Messages bridge has never connected. Install bridge/jarvis_bridge.py on "
            "your Mac and start it — see docs/messages-bridge.md."
        )
    age = (utcnow() - beat.last_seen).total_seconds() / 60
    if age > settings.bridge_stale_minutes:
        return False, (
            f"The Messages bridge on {beat.hostname or 'your Mac'} last checked in "
            f"{int(age)} minutes ago, so this data may be stale. Make sure the Mac is awake "
            "and the bridge is running."
        )
    return True, ""


def _rank(row: ChatMessage) -> int:
    score = 40
    text = (row.text or "").lower()
    sender = (row.sender or "").lower()
    if row.is_group:
        score -= 15
    if any(n in sender for n in _NOISE_SENDERS) or sender.isdigit() and len(sender) <= 6:
        score -= 35
    if "?" in text:
        score += 20
    if any(w in text for w in ("can you", "are you", "call me", "urgent", "asap", "tonight", "tomorrow")):
        score += 25
    if len(text) > 120:
        score += 5
    return max(0, min(100, score))


@registry.tool(
    name="messages_recent",
    description=(
        "Read recent iMessage/SMS conversations mirrored from the user's Mac, ranked by how "
        "likely they are to need a reply. Use for 'any messages', 'what did X say', or a "
        "morning briefing. Returns message text directly — no separate expand step needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "description": "How far back to look. Default 24."},
            "limit": {"type": "integer", "description": "Max messages. Default 40."},
            "from_contact": {
                "type": "string",
                "description": "Filter to one person: phone number, email, or contact name.",
            },
            "include_sent": {
                "type": "boolean",
                "description": "Include messages the user sent. Default false.",
            },
        },
    },
    requires="messages",
    tags=["messages"],
)
async def messages_recent(
    hours: int = 24, limit: int = 40, from_contact: str = "", include_sent: bool = False
):
    healthy, warning = await _bridge_state()
    since = utcnow() - dt.timedelta(hours=max(hours, 1))

    async with session_scope() as session:
        stmt = select(ChatMessage).where(ChatMessage.sent_at >= since)
        if not include_sent:
            stmt = stmt.where(ChatMessage.is_from_me == 0)
        if from_contact:
            needle = f"%{from_contact.lower()}%"
            stmt = stmt.where(
                func.lower(ChatMessage.sender).like(needle)
                | func.lower(ChatMessage.sender_name).like(needle)
                | func.lower(ChatMessage.chat_name).like(needle)
            )
        stmt = stmt.order_by(ChatMessage.sent_at.desc()).limit(min(limit, 100))
        rows = list((await session.execute(stmt)).scalars().all())

    messages = [
        {
            "guid": r.guid,
            "from": r.sender_name or r.sender,
            "handle": r.sender,
            "chat": r.chat_name,
            "text": r.text,
            "service": r.service,
            "is_group": bool(r.is_group),
            "from_me": bool(r.is_from_me),
            "at": r.sent_at.isoformat(),
            "priority": _rank(r),
        }
        for r in rows
    ]
    needs_reply = sorted(
        (m for m in messages if not m["from_me"] and m["priority"] >= 55),
        key=lambda m: m["priority"],
        reverse=True,
    )

    payload = {
        "count": len(messages),
        "likely_need_reply": needs_reply[:10],
        "all_messages": messages,
    }
    if not healthy:
        payload["bridge_warning"] = warning
    return ToolResult.success(payload, display={"type": "messages", "messages": messages[:25]})


@registry.tool(
    name="messages_send",
    description=(
        "Send an iMessage or SMS through the user's Mac. Show the user the exact recipient "
        "and wording and get an explicit yes before calling this — it sends from their real "
        "number and cannot be unsent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Phone number in +1XXXXXXXXXX form, or an iMessage email address.",
            },
            "text": {"type": "string", "description": "Exact message body to send."},
            "service": {
                "type": "string",
                "enum": ["iMessage", "SMS"],
                "description": "Delivery service. Default iMessage.",
            },
        },
        "required": ["recipient", "text"],
    },
    requires="messages",
    confirm=True,
    tags=["messages", "write"],
)
async def messages_send(recipient: str, text: str, service: str = "iMessage"):
    healthy, warning = await _bridge_state()
    async with session_scope() as session:
        outbound = OutboundMessage(recipient=recipient, text=text, service=service)
        session.add(outbound)
        await session.flush()
        job_id = outbound.id

    result = {
        "queued": True,
        "job_id": job_id,
        "recipient": recipient,
        "note": "The Mac bridge will deliver this within a few seconds.",
    }
    if not healthy:
        result["warning"] = warning + " The message is queued and will send once it reconnects."
    return ToolResult.success(result)


@registry.tool(
    name="messages_threads",
    description="List the user's most recently active message conversations with a preview of each.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max threads. Default 15."},
            "days": {"type": "integer", "description": "Look back this many days. Default 7."},
        },
    },
    requires="messages",
    tags=["messages"],
)
async def messages_threads(limit: int = 15, days: int = 7):
    since = utcnow() - dt.timedelta(days=max(days, 1))
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.sent_at >= since)
                    .order_by(ChatMessage.sent_at.desc())
                    .limit(500)
                )
            ).scalars().all()
        )

    threads: dict[str, dict] = {}
    for row in rows:
        key = row.chat_id or row.sender
        if key in threads:
            threads[key]["message_count"] += 1
            continue
        threads[key] = {
            "chat_id": key,
            "name": row.chat_name or row.sender_name or row.sender,
            "is_group": bool(row.is_group),
            "last_message": row.text[:180],
            "last_from_me": bool(row.is_from_me),
            "last_at": row.sent_at.isoformat(),
            "message_count": 1,
        }
        if len(threads) >= limit:
            break

    return ToolResult.success({"threads": list(threads.values())})
