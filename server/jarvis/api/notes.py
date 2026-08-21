"""The notes graph, for the galaxy viewer.

The whole vault goes over the wire in one response — a few thousand notes with
700-character excerpts is a couple of megabytes, and paging it would mean the
viewer could not lay the graph out until every page had landed. It is cached,
because rebuilding the link set is O(notes × titles) and the vault changes on
the timescale of someone typing, not of someone opening the HUD.
"""

from __future__ import annotations

from collections import deque

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import cache
from .. import notes as vault
from ..llm.client import LLMError, get_llm
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

    store = vault.get_vault()
    data = store.graph()
    data["generation"] = store.signature()
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


# --- asking the vault a question, from inside the galaxy ---

TOP_NOTES = 6
ANSWER_TOKENS = 400          # 2-3 sentences needs nowhere near this
HISTORY_TURNS = 6            # 3 exchanges: enough for "and the other one?"
MAX_SESSIONS = 50

SYSTEM_PROMPT = """You are answering a question about this person's own markdown \
notes. The notes below are the ones most relevant to what they asked.

Rules, in order of importance:

1. Answer ONLY from the notes provided. They are the whole of what you know here.
2. If the notes do not cover it, say so plainly in one sentence. Do not fill the \
gap from general knowledge and do not soften it — "your notes don't cover that" is \
a useful answer and a guess is not.
3. Keep it to 2-3 sentences.
4. Name the notes you used when it helps them find them again.

The notes follow."""

# Per-session history, so follow-ups work. Bounded on both axes because this is
# process memory on a small box, not a database.
_SESSIONS: dict[str, deque] = {}


class Question(BaseModel):
    question: str
    session: str = "galaxy"
    generation: str = ""


def _history(session: str) -> deque:
    if session not in _SESSIONS:
        if len(_SESSIONS) >= MAX_SESSIONS:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        _SESSIONS[session] = deque(maxlen=HISTORY_TURNS)
    return _SESSIONS[session]


@router.post("/ask")
async def ask(device: CurrentDevice, body: Question):
    """Answer from the vault, and say which notes it came from.

    Deliberately not the full agent loop. This is one retrieval and one
    completion — no tool round-trips — so it answers in a single call instead of
    the three or four a tool-calling turn costs. On a metered free tier that is
    the difference between a galaxy you can ask questions of and one you cannot.

    It does go through the shared endpoint pool, so it inherits failover across
    every configured provider rather than dying whenever the primary is busy.
    """
    question = body.question.strip()[:2000]
    if not question:
        raise HTTPException(400, "Ask something.")
    if vault.notes_dir() is None:
        raise HTTPException(404, "No notes folder configured.")

    store = vault.get_vault()
    hits = store.search(question, limit=TOP_NOTES)
    notes = [note for note, _score in hits]

    if notes:
        context = "\n\n---\n\n".join(
            f"## {n.title}\n(folder: {n.folder})\n\n{n.excerpt}" for n in notes
        )
    else:
        context = "(No notes matched this question.)"

    history = _history(body.session)
    messages = (
        [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context}"}]
        + list(history)
        + [{"role": "user", "content": question}]
    )

    try:
        response = await get_llm().complete(
            messages,
            max_tokens=ANSWER_TOKENS,
            temperature=0.2,     # recall, not writing: stay close to the notes
        )
    except LLMError as exc:
        # The pool's own message names which endpoints were tried and why each
        # declined, which is far more useful than "the model failed".
        raise HTTPException(503, str(exc)) from None

    answer = response.content.strip() or "(no answer came back)"
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})

    current = store.signature()
    return {
        "answer": answer,
        # Node ids are positions in the graph array. If the vault changed since
        # the page loaded, these refer to a graph the browser does not have, and
        # flying the camera to them would land on unrelated notes.
        "nodes": [n.index for n in notes],
        "generation": current,
        "stale": bool(body.generation) and body.generation != current,
    }
