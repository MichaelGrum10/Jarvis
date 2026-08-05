"""Long-term memory: facts Jarvis should still know next week."""

from __future__ import annotations

from sqlalchemy import select

from ..db import Memory, session_scope, utcnow
from .base import ToolResult, registry


@registry.tool(
    name="memory_save",
    description=(
        "Remember a durable fact about the user — a preference, a recurring detail, a person "
        "in their life, how they like things done. Save proactively when they tell you "
        "something that will still matter later ('I always get my hair cut at Tony's', 'my "
        "wife's name is Sara'). Do not save one-off task details."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Short stable identifier, e.g. 'preferred_barber'. Re-using a key overwrites it.",
            },
            "value": {"type": "string", "description": "The fact itself, written as a full sentence."},
            "category": {
                "type": "string",
                "enum": ["preference", "person", "routine", "work", "health", "finance", "general"],
                "description": "Grouping for retrieval.",
            },
        },
        "required": ["key", "value"],
    },
    tags=["memory"],
)
async def memory_save(key: str, value: str, category: str = "general"):
    async with session_scope() as session:
        existing = (
            await session.execute(select(Memory).where(Memory.key == key))
        ).scalar_one_or_none()
        if existing:
            existing.value = value
            existing.category = category
            existing.updated_at = utcnow()
            action = "updated"
        else:
            session.add(Memory(key=key, value=value, category=category))
            action = "saved"
    return ToolResult.success({action: key, "value": value})


@registry.tool(
    name="memory_search",
    description=(
        "Look up what you already know about the user. Call this when a request depends on "
        "their preferences or history and the answer isn't in the current conversation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword to match. Omit to list everything."},
            "category": {"type": "string", "description": "Optional category filter."},
        },
    },
    tags=["memory"],
)
async def memory_search(query: str = "", category: str = ""):
    async with session_scope() as session:
        stmt = select(Memory)
        if category:
            stmt = stmt.where(Memory.category == category)
        rows = list((await session.execute(stmt.order_by(Memory.updated_at.desc()))).scalars().all())

    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in r.key.lower() or needle in r.value.lower()]

    return ToolResult.success(
        {
            "count": len(rows),
            "memories": [
                {"key": r.key, "value": r.value, "category": r.category} for r in rows[:50]
            ],
        }
    )


@registry.tool(
    name="memory_forget",
    description="Delete a stored fact by key. Use when the user says something is no longer true.",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "The memory key to remove."}},
        "required": ["key"],
    },
    confirm=True,
    tags=["memory"],
)
async def memory_forget(key: str):
    async with session_scope() as session:
        row = (await session.execute(select(Memory).where(Memory.key == key))).scalar_one_or_none()
        if row is None:
            return ToolResult.fail(f"Nothing stored under '{key}'.")
        await session.delete(row)
    return ToolResult.success({"forgotten": key})


async def memory_preamble(limit: int = 40) -> str:
    """Injected into the system prompt so Jarvis starts every turn already knowing you."""
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(select(Memory).order_by(Memory.updated_at.desc()).limit(limit))
            ).scalars().all()
        )
    if not rows:
        return ""
    lines = "\n".join(f"- [{r.category}] {r.value}" for r in rows)
    return f"\n\nWhat you already know about the user:\n{lines}"
