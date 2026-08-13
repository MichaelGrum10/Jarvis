"""Searching and growing a markdown vault."""

from __future__ import annotations

from ..notes import NotesError, get_vault
from .base import ToolResult, registry


@registry.tool(
    name="notes_search",
    description=(
        "Search the user's own markdown notes and return the most relevant ones with "
        "excerpts. Use this whenever they ask something that their own notes would answer — "
        "about their projects, decisions, people, or anything they have written down. "
        "Answer from what comes back and say plainly when the notes don't cover it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "limit": {"type": "integer", "description": "How many notes. Default 6."},
        },
        "required": ["query"],
    },
    requires="notes",
    tags=["notes"],
)
async def notes_search(query: str, limit: int = 6):
    hits = get_vault().search(query, limit=max(1, min(limit, 12)))
    if not hits:
        return ToolResult.success(
            {"query": query, "count": 0,
             "note": "Nothing in the notes matches. Say so rather than answering from memory."}
        )

    results = [
        {**note.to_dict(), "score": round(score, 1), "node": note.index}
        for note, score in hits
    ]
    return ToolResult.success(
        {"query": query, "count": len(results), "notes": results},
        # The viewer uses these to fly the camera to the notes an answer came
        # from, which is the whole point of showing the graph.
        display={"type": "notes", "query": query, "notes": results},
    )


@registry.tool(
    name="notes_remember",
    description=(
        "Write something into the user's notes as a new markdown file. Use it when they say "
        "'remember that…', or ask you to note, capture or write something down. Not for "
        "facts about how they like things done — memory_save handles those."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Exactly what to record."},
        },
        "required": ["text"],
    },
    requires="notes",
    tags=["notes", "write"],
)
async def notes_remember(text: str):
    text = (text or "").strip()
    if len(text) < 3:
        return ToolResult.fail("Nothing to record.")
    try:
        note = get_vault().capture(text)
    except NotesError as exc:
        return ToolResult.fail(str(exc))
    except OSError as exc:
        return ToolResult.fail(f"Could not write the note: {exc}")

    return ToolResult.success(
        {"created": note.to_dict()},
        display={"type": "note_created", "title": note.title},
    )
