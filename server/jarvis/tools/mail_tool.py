"""Mail tools: triage the inbox, expand a single message, search, send."""

from __future__ import annotations

from ..integrations.apple_mail import get_mail
from .base import ToolResult, registry

# Cheap heuristics so the model gets a pre-sorted inbox instead of 40 flat rows.
_IMPORTANT_HINTS = (
    "urgent", "asap", "action required", "deadline", "invoice", "payment", "overdue",
    "interview", "offer", "contract", "signature", "confirm", "verification code",
    "security alert", "suspicious", "past due", "final notice", "appointment",
)
_NOISE_HINTS = (
    "unsubscribe", "newsletter", "no-reply", "noreply", "promotions", "sale", "% off",
    "deal", "webinar", "digest", "notification", "do not reply",
)


def _score(item) -> tuple[int, list[str]]:
    """Rank a message 0-100. Deterministic, so the LLM isn't guessing at priority."""
    haystack = f"{item.subject} {item.sender} {item.sender_email}".lower()
    score, reasons = 30, []

    if item.unread:
        score += 20
        reasons.append("unread")
    for hint in _IMPORTANT_HINTS:
        if hint in haystack:
            score += 25
            reasons.append(f"mentions '{hint}'")
            break
    for hint in _NOISE_HINTS:
        if hint in haystack:
            score -= 30
            reasons.append("looks like bulk mail")
            break
    if item.has_attachments:
        score += 8
        reasons.append("has attachment")
    if any(d in item.sender_email.lower() for d in ("@icloud.com", "@gmail.com", "@me.com")):
        score += 10
        reasons.append("from a person")

    return max(0, min(100, score)), reasons


@registry.tool(
    name="mail_summary",
    description=(
        "Fetch recent email from the user's iCloud inbox, pre-ranked by importance. Returns "
        "headers and a priority score only — not full bodies. Use this for 'what's in my "
        "inbox', 'anything important', or a morning briefing. To read one message in full, "
        "follow up with mail_read using its uid."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many days back to look. Default 3."},
            "limit": {"type": "integer", "description": "Max messages to fetch. Default 25."},
            "unread_only": {"type": "boolean", "description": "Only unread mail. Default false."},
            "mailbox": {"type": "string", "description": "Mailbox name. Default INBOX."},
        },
    },
    requires="mail",
    tags=["mail"],
)
async def mail_summary(
    days: int = 3, limit: int = 25, unread_only: bool = False, mailbox: str = "INBOX"
):
    items = await get_mail().recent(
        limit=min(limit, 60), mailbox=mailbox, unread_only=unread_only, days=days
    )
    ranked = []
    for item in items:
        score, reasons = _score(item)
        row = item.to_dict()
        row["priority"] = score
        row["priority_reasons"] = reasons
        ranked.append(row)
    ranked.sort(key=lambda r: r["priority"], reverse=True)

    important = [r for r in ranked if r["priority"] >= 55]
    return ToolResult.success(
        {
            "total": len(ranked),
            "important_count": len(important),
            "important": important[:10],
            "other": [
                {k: r[k] for k in ("uid", "subject", "sender", "date", "unread")}
                for r in ranked
                if r not in important
            ][:20],
            "hint": "Use mail_read with a uid to show the full body of any of these.",
        },
        display={"type": "mail_list", "messages": ranked[:20]},
    )


@registry.tool(
    name="mail_read",
    description=(
        "Read one email in full, including its body. Use when the user asks to expand, open, "
        "or see the details of a message you previously summarised."
    ),
    parameters={
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "Message uid from mail_summary or mail_search."},
            "mailbox": {"type": "string", "description": "Mailbox the message is in. Default INBOX."},
        },
        "required": ["uid"],
    },
    requires="mail",
    tags=["mail"],
)
async def mail_read(uid: str, mailbox: str = "INBOX"):
    message = await get_mail().full(uid, mailbox)
    data = message.__dict__.copy()
    return ToolResult.success(data, display={"type": "mail_full", "message": data})


@registry.tool(
    name="mail_search",
    description="Search the user's mailbox by keyword, sender, or subject text.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for."},
            "limit": {"type": "integer", "description": "Max results. Default 20."},
            "mailbox": {"type": "string", "description": "Mailbox to search. Default INBOX."},
        },
        "required": ["query"],
    },
    requires="mail",
    tags=["mail"],
)
async def mail_search(query: str, limit: int = 20, mailbox: str = "INBOX"):
    items = await get_mail().search(query, limit=limit, mailbox=mailbox)
    rows = [i.to_dict() for i in items]
    return ToolResult.success(
        {"query": query, "count": len(rows), "results": rows},
        display={"type": "mail_list", "messages": rows},
    )


@registry.tool(
    name="mail_send",
    description=(
        "Send an email from the user's iCloud address. Always show the user the exact "
        "recipient, subject and body and get an explicit yes before calling this."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient address."},
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Plain-text body."},
            "cc": {"type": "string", "description": "Optional CC address."},
        },
        "required": ["to", "subject", "body"],
    },
    requires="mail",
    confirm=True,
    tags=["mail", "write"],
)
async def mail_send(to: str, subject: str, body: str, cc: str = ""):
    await get_mail().send(to, subject, body, cc)
    return ToolResult.success({"sent": True, "to": to, "subject": subject})
