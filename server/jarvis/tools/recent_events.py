"""What the calendar tools just touched, so "move it" has an antecedent.

Tool results live only inside the turn that produced them: `history_from_rows`
deliberately drops tool messages, because replaying full transcripts costs more
context than it is worth. That is the right trade for most tools and exactly
wrong for one case — creating an event and then saying "actually make it 4".
The uid was in a tool result that no longer exists, so the follow-up has nothing
to refer to and the model either guesses or gives up.

This keeps just the identifying fields of recently touched events, per
conversation, and the agent puts them in the system prompt. Small enough to cost
almost nothing, specific enough that "it" resolves.

Deliberately in memory rather than the database: this is scratch context for the
next few turns, not a record. The calendar itself is the record, and
calendar_list re-reads it whenever a conversation goes cold.
"""

from __future__ import annotations

import datetime as dt
import threading

MAX_PER_CONVERSATION = 6
# Long enough to cover a booking and the corrections that follow it, short
# enough that yesterday's events never masquerade as what "it" means.
TTL = dt.timedelta(hours=2)

_lock = threading.Lock()
_recent: dict[int, list[dict]] = {}


def remember(conversation_id: int | None, event: dict, action: str = "created") -> None:
    """Record an event the assistant just created, changed or read."""
    if conversation_id is None or not event.get("uid"):
        return
    entry = {
        "uid": event["uid"],
        "title": event.get("summary") or event.get("title") or "",
        "start": event.get("start", ""),
        "calendar": event.get("calendar", ""),
        "action": action,
        "at": dt.datetime.now(dt.UTC),
    }
    with _lock:
        items = [e for e in _recent.get(conversation_id, []) if e["uid"] != entry["uid"]]
        items.append(entry)
        _recent[conversation_id] = items[-MAX_PER_CONVERSATION:]


def forget(conversation_id: int | None, uid: str) -> None:
    """Drop a deleted event, so it can't be offered as a target again."""
    if conversation_id is None:
        return
    with _lock:
        items = [e for e in _recent.get(conversation_id, []) if e["uid"] != uid]
        if items:
            _recent[conversation_id] = items
        else:
            _recent.pop(conversation_id, None)


def recent(conversation_id: int | None) -> list[dict]:
    if conversation_id is None:
        return []
    cutoff = dt.datetime.now(dt.UTC) - TTL
    with _lock:
        items = [e for e in _recent.get(conversation_id, []) if e["at"] > cutoff]
        if items:
            _recent[conversation_id] = items
        else:
            _recent.pop(conversation_id, None)
        return list(items)


def preamble(conversation_id: int | None) -> str:
    """The system-prompt block. Empty when there's nothing recent."""
    items = recent(conversation_id)
    if not items:
        return ""
    lines = [
        f"- {e['title'] or '(untitled)'} at {e['start']} — uid `{e['uid']}` ({e['action']})"
        for e in reversed(items)
    ]
    return (
        "\n\n## Calendar events from this conversation\n"
        "You touched these just now. When the user says \"it\", \"that\", \"the "
        "appointment\" or similar, they almost certainly mean the most recent one — "
        "use its uid with calendar_update or calendar_delete directly, without "
        "calling calendar_list first.\n" + "\n".join(lines) + "\n"
    )


def reset() -> None:
    """Clear everything. For tests."""
    with _lock:
        _recent.clear()
