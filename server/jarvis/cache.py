"""A small on-disk cache with per-kind TTLs.

Two separate wins, and the second is the one that matters here.

Latency: a repeated question hits the file rather than the network, so "how's
NVDA" asked twice in a minute costs one Yahoo round-trip instead of two.

Tokens: this assistant's real constraint is a per-minute token budget, and every
avoided tool call is also an avoided model round-trip carrying the whole system
prompt and tool schemas back up the wire. A cache hit saves far more than the
API call it replaces.

On disk rather than in memory, deliberately: the container restarts on every
update, and an in-memory cache is empty exactly when someone has just rebuilt to
fix something and is testing it hardest.

Not for anything the user is about to change. Writes invalidate rather than
populate — booking an event clears the calendar cache — because showing someone
their own change missing is worse than any latency it saved.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Chosen from how fast each thing actually changes, not by taste. Quotes move
# constantly; a calendar changes when a person does something; a place's address
# is effectively static.
TTLS = {
    "stocks": 60.0,
    "calendar": 300.0,
    "mail": 120.0,
    "news": 600.0,
    "places": 86_400.0,
    "search": 900.0,
    # HUD panels refresh on their own schedule — a person glances at a dashboard
    # rather than interrogating it. The `_last` variants never expire: they are
    # the fallback shown with an age when a refresh fails.
    "hud_markets": 60.0,
    "hud_calendar": 300.0,
    "hud_inbox": 120.0,
    "hud_messages": 120.0,
    "hud_markets_last": 86_400.0,
    "hud_calendar_last": 86_400.0,
    "hud_inbox_last": 86_400.0,
    "hud_messages_last": 86_400.0,
    # The vault's own index catches file changes by mtime, so this only avoids
    # re-walking the link set for a viewer that reloads. Short, because a note
    # captured by voice should appear as a new star without waiting.
    "notes_graph": 30.0,
}
DEFAULT_TTL = 120.0


def _dir() -> Path:
    path = Path(get_settings().data_dir) / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file(kind: str, key: str) -> Path:
    # Hashed because keys contain user text — a search query, an email address —
    # and those have no business becoming filenames on disk.
    digest = hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:20]
    return _dir() / f"{kind}-{digest}.json"


def get(kind: str, key: str) -> Any | None:
    """The cached value, or None when absent or stale."""
    path = _file(kind, key)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    age = time.time() - payload.get("at", 0)
    if age > TTLS.get(kind, DEFAULT_TTL):
        return None
    return payload.get("value")


def put(kind: str, key: str, value: Any) -> None:
    path = _file(kind, key)
    try:
        # Written whole then moved: a half-written file read by the next request
        # is a JSON error where a cache miss was wanted.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"at": time.time(), "value": value}, default=str))
        temporary.replace(path)
    except (OSError, TypeError) as exc:
        log.debug("Could not cache %s: %s", kind, exc)


def invalidate(kind: str) -> None:
    """Drop everything of one kind, after a write that changed it."""
    for path in _dir().glob(f"{kind}-*.json"):
        path.unlink(missing_ok=True)


def clear() -> None:
    for path in _dir().glob("*.json"):
        path.unlink(missing_ok=True)


def stats() -> dict:
    files = list(_dir().glob("*.json"))
    return {
        "entries": len(files),
        "kinds": sorted({f.name.split("-", 1)[0] for f in files}),
        "bytes": sum(f.stat().st_size for f in files),
    }
