"""The notes graph, for the galaxy viewer.

The whole vault goes over the wire in one response — a few thousand notes with
700-character excerpts is a couple of megabytes, and paging it would mean the
viewer could not lay the graph out until every page had landed. It is cached,
because rebuilding the link set is O(notes × titles) and the vault changes on
the timescale of someone typing, not of someone opening the HUD.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import cache
from .. import notes as vault
from ..security import CurrentDevice

router = APIRouter(prefix="/api/notes", tags=["notes"])


@router.get("/graph")
async def graph(device: CurrentDevice, refresh: bool = False):
    if vault.notes_dir() is None:
        raise HTTPException(
            404,
            "No notes folder configured. Set NOTES_DIR to a directory of markdown "
            "files and restart.",
        )

    if refresh:
        cache.invalidate("notes_graph")
    cached = cache.get("notes_graph", "all")
    if cached is not None:
        return cached

    data = vault.get_vault().graph()
    cache.put("notes_graph", "all", data)
    return data


@router.get("/search")
async def search(device: CurrentDevice, q: str, limit: int = 8):
    """Used by the viewer's own search box, so it need not go through the model."""
    hits = vault.get_vault().search(q, limit=max(1, min(limit, 25)))
    return {
        "query": q,
        "notes": [
            {**note.to_dict(), "score": round(score, 1), "node": note.index}
            for note, score in hits
        ],
    }


@router.get("/note/{index}")
async def note(device: CurrentDevice, index: int):
    notes = vault.get_vault().load()
    if not 0 <= index < len(notes):
        raise HTTPException(404, "No such note.")
    found = notes[index]
    return {**found.to_dict(), "text": found.text, "node": found.index}
