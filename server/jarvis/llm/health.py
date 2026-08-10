"""Measured endpoint health, remembered between runs.

The pool's order is the user's stated preference, which is the right default and
a poor way to survive a dozen free tiers. Half of them will be broken on any
given day — a retired model, a spent allowance, a provider that answers but
cannot format a tool call — and discovering that during a request costs a
wasted round-trip each time.

So the benchmark writes what it measured, and the pool reads it: endpoints known
to fail tool calls go last, and among the rest the faster ones go first. Nothing
is dropped on the strength of a recording — a measurement is evidence about the
past, and a provider that was broken this morning may be fine now.

Deliberately not automatic. Rankings come only from `python -m jarvis.benchmark`,
a command the user runs, because silently reordering which model answers on the
basis of a latency sample is the kind of helpfulness nobody asked for.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)

# Past this, a measurement says more about the day it was taken than about now.
MAX_AGE_DAYS = 14.0


def _path() -> Path:
    return Path(get_settings().data_dir) / "endpoint-health.json"


def record(results: list[dict]) -> None:
    """Store one benchmark run's findings, keyed by endpoint label."""
    health = {
        row["label"]: {
            "tools": bool(row.get("tools_large")),
            "latency": row.get("large_latency") or row.get("latency"),
            "error": (row.get("error") or "")[:120],
        }
        for row in results
        if row.get("label")
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time(), "endpoints": health}, indent=1))
    log.info("Recorded health for %d endpoints", len(health))


def load() -> dict[str, dict]:
    """What the last run found, or nothing if it is missing or too old."""
    try:
        payload = json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if (time.time() - payload.get("at", 0)) > MAX_AGE_DAYS * 86_400:
        return {}
    return payload.get("endpoints", {})


def rank(endpoints: list) -> list:
    """Reorder by what was measured, leaving unmeasured endpoints where they are.

    Three tiers: endpoints measured working, then ones never measured, then ones
    measured failing. Within the working tier, faster first. An unmeasured
    endpoint sits in the middle rather than last, because "not yet tested" is
    not evidence against it — the pool would otherwise bury anything newly
    added behind everything already known.
    """
    health = load()
    if not health:
        return endpoints

    def key(item):
        index, endpoint = item
        measured = health.get(endpoint.label)
        if measured is None:
            return (1, 0.0, index)
        if not measured.get("tools"):
            return (2, 0.0, index)
        return (0, measured.get("latency") or 99.0, index)

    return [e for _, e in sorted(enumerate(endpoints), key=key)]


def describe() -> str:
    """One line per endpoint, for the doctor."""
    health = load()
    if not health:
        return ""
    lines = []
    for label, measured in sorted(health.items()):
        if measured.get("tools"):
            lines.append(f"{label}: {measured.get('latency', 0):.2f}s, tool calls OK")
        else:
            lines.append(f"{label}: fails tool calls ({measured.get('error', 'no detail')[:60]})")
    return "\n".join(lines)
