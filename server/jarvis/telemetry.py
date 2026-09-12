"""Recording what goes wrong, so self-improvement has something real to work on.

An agent told to "improve yourself" with no evidence writes plausible-looking
churn: it refactors things nobody complained about and invents features nobody
wanted. An agent handed "calendar_create has failed 14 times this week with
VTIMEZONE errors" fixes an actual problem.

So every failure is recorded with enough context to act on: what the user asked,
which tool broke, and the error. Recurring failures rise to the top by frequency,
which is a decent proxy for "what is actually costing you".

Also recorded: requests where nothing failed but the assistant said it *couldn't*
do something. Those are the strongest signal for a missing capability, and they
are invisible in an error log because technically nothing went wrong.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from collections.abc import Callable

from sqlalchemy import func, select

from .db import FailureEvent, session_scope, utcnow

log = logging.getLogger(__name__)

# Phrases that mean "I was asked for something I can't do". Deliberately narrow:
# a false positive here sends the improvement engine chasing a feature that
# already exists.
CANNOT_PATTERNS = (
    r"\bi (?:can'?t|cannot|am unable to|don'?t have (?:a|the) (?:tool|way|ability))\b",
    r"\bi don'?t have access to\b",
    r"\bthere'?s no (?:tool|way) (?:for|to)\b",
    r"\bnot something i can\b",
)
_CANNOT = re.compile("|".join(CANNOT_PATTERNS), re.IGNORECASE)


def fingerprint(kind: str, detail: str) -> str:
    """Group similar failures.

    Volatile parts — ids, timestamps, quantities — are stripped first, so
    "Limit 6000, Requested 11081" and "Limit 6000, Requested 9004" count as the
    same problem rather than two.
    """
    normalised = detail.lower()
    # Long opaque tokens first, before digit-stripping mangles them beyond
    # recognition. This is what makes recurrence detectable at all: a calendar
    # error embeds a fresh event UID every time, so without collapsing these,
    # fifty occurrences of one bug look like fifty unrelated problems and none
    # ever reaches the threshold to be worked on.
    normalised = re.sub(r"\b[0-9a-f]{8,}(?:-[0-9a-f]{4,}){0,4}\b", "ID", normalised)
    normalised = re.sub(r"\b[a-z0-9_]{16,}\b", "ID", normalised)
    normalised = re.sub(r"\d+", "N", normalised)
    normalised = re.sub(r"\s+", " ", normalised).strip()[:300]
    return hashlib.sha256(f"{kind}|{normalised}".encode()).hexdigest()[:16]


# Called after each recorded failure, so the improvement engine can look at a
# problem while it is still happening rather than at the next scheduled tick.
# A list of callbacks rather than a direct import, because telemetry is imported
# by nearly everything and must not depend on the engine that reads it.
_watchers: list[Callable[[], None]] = []


def on_failure(callback: Callable[[], None]) -> None:
    """Register a nudge. Idempotent, so a restarted loop doesn't stack them."""
    if callback not in _watchers:
        _watchers.append(callback)


async def record_failure(
    kind: str,
    detail: str,
    *,
    request: str = "",
    tool: str = "",
) -> None:
    """Log a failure. Never raises — telemetry must not break the thing it watches."""
    try:
        async with session_scope() as session:
            session.add(
                FailureEvent(
                    kind=kind[:40],
                    tool=tool[:60],
                    fingerprint=fingerprint(kind, detail),
                    detail=detail[:2000],
                    request=request[:500],
                )
            )
    except Exception:
        log.exception("Could not record telemetry (continuing)")

    for watcher in _watchers:
        try:
            watcher()
        except Exception:
            # Same rule as above: the thing that watches failures must not
            # become one. A broken watcher is logged and ignored.
            log.exception("A failure watcher raised (continuing)")


async def record_if_refusal(request: str, reply: str) -> None:
    """Note replies that decline for lack of capability — a missing-feature signal."""
    if not reply or not _CANNOT.search(reply):
        return
    await record_failure(
        "missing_capability",
        f"Asked: {request[:200]}\nReplied: {reply[:300]}",
        request=request,
    )


async def top_issues(days: int = 7, limit: int = 10) -> list[dict]:
    """Recurring failures, most frequent first."""
    since = utcnow() - dt.timedelta(days=max(days, 1))
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    FailureEvent.fingerprint,
                    FailureEvent.kind,
                    FailureEvent.tool,
                    func.count(FailureEvent.id).label("count"),
                    func.max(FailureEvent.created_at).label("last_seen"),
                    func.max(FailureEvent.detail).label("detail"),
                    func.max(FailureEvent.request).label("request"),
                )
                .where(FailureEvent.created_at >= since, FailureEvent.resolved == 0)
                .group_by(FailureEvent.fingerprint)
                .order_by(func.count(FailureEvent.id).desc())
                .limit(limit)
            )
        ).all()

    return [
        {
            "fingerprint": r.fingerprint,
            "kind": r.kind,
            "tool": r.tool,
            "count": r.count,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "detail": r.detail,
            "example_request": r.request,
        }
        for r in rows
    ]


async def mark_resolved(fingerprint_value: str) -> int:
    """Stop an issue being re-worked once it's been addressed."""
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(FailureEvent).where(
                        FailureEvent.fingerprint == fingerprint_value,
                        FailureEvent.resolved == 0,
                    )
                )
            ).scalars().all()
        )
        for row in rows:
            row.resolved = 1
    return len(rows)


def describe_issue(issue: dict) -> str:
    """Turn an aggregated issue into a goal the improvement engine can act on."""
    header = (
        f"{issue['kind']} occurring repeatedly "
        f"({issue['count']} times in the last week"
        + (f", in tool '{issue['tool']}'" if issue["tool"] else "")
        + ")"
    )
    body = f"\n\nError detail:\n{issue['detail']}"
    if issue.get("example_request"):
        body += f"\n\nA request that triggered it:\n{issue['example_request']}"
    return header + body
