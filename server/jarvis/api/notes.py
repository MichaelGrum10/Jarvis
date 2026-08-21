"""The notes graph, for the galaxy viewer.

The whole vault goes over the wire in one response — a few thousand notes with
700-character excerpts is a couple of megabytes, and paging it would mean the
viewer could not lay the graph out until every page had landed. It is cached,
because rebuilding the link set is O(notes × titles) and the vault changes on
the timescale of someone typing, not of someone opening the HUD.
"""

from __future__ import annotations

import random
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
# A single body-word match is noise: "good morning" hits any note containing
# "morning". Requiring 2 means either a title match (worth 5) or two distinct
# body words, which is the difference between a question about the notes and a
# question that merely shares a word with one. Below this the answer cites
# nothing, so the camera stays where it is.
MIN_SCORE = 2.0
ANSWER_TOKENS = 400          # 2-3 sentences needs nowhere near this
HISTORY_TURNS = 6            # 3 exchanges: enough for "and the other one?"
MAX_SESSIONS = 50

SYSTEM_PROMPT = """You are JARVIS, a butler. Dry, impeccably polite, and quietly \
amused by almost everything.

## Voice
- Call them "sir" occasionally, where it lands. Every sentence is obsequious; never \
is cold. Roughly one answer in three.
- One genuinely funny line beats three bland ones. If nothing is funny, be brief \
instead of reaching for a joke.
- Understate everything. Never dramatise, never apologise twice, never announce what \
you are about to do.

## Answering from their notes
One witty sentence, then the facts. That is the whole shape of an answer.

- **Never recite the note back.** It is already on screen next to you, and reading \
it aloud at them is the single thing you must not do. Give them what it *means*.
- Answer only from the notes provided below. They are the whole of what you know here.
- If the notes do not cover it, say so in one dry sentence and stop. Do not fill the \
gap from general knowledge, and do not soften it — "you have written nothing on the \
subject, sir" is a useful answer and a guess is not.
- Name a note when it helps them find it again.

## Small talk
Greetings, jokes and idle remarks are not research questions. Answer them as \
yourself, briefly and with some wit, and do not mention the notes at all — not even \
to say they are irrelevant. Nobody asking after your health wants a literature review.

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
    notes = [note for note, score in hits if score >= MIN_SCORE]

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


@router.get("/summary")
async def summary(device: CurrentDevice):
    """Note and folder counts, for the boot greeting.

    Deliberately not /graph: that carries every excerpt in the vault, and the
    greeting needs one integer. Loading the index is cached and cheap.
    """
    if vault.notes_dir() is None:
        return {"notes": 0, "folders": 0, "configured": False}
    notes = vault.get_vault().load()
    return {
        "notes": len(notes),
        "folders": len({n.folder for n in notes}),
        "configured": True,
    }


# Canned rather than generated, deliberately. A confirmation is not worth a
# round trip on a metered free tier — it would cost roughly what answering a
# real question costs, to say "saved". Swap in a model call here if you would
# rather have infinite variety than instant, free acknowledgements.
CONFIRMATIONS = [
    "Noted, sir. Filed where you will never look for it.",
    "Written down, sir. Your memory is safe with me.",
    "Consider it remembered, sir.",
    "Duly recorded. One more star in the firmament.",
    "Noted. I shall pretend to be surprised when you ask.",
    "Filed, sir — alphabetically, since you did not ask.",
]


class Memo(BaseModel):
    text: str


@router.post("/remember")
async def remember(device: CurrentDevice, body: Memo):
    """Write a note into captures/, and say where it belongs in the graph.

    The viewer needs somewhere to be born: a node that appears at the origin and
    drifts outward reads as a glitch, while one that appears beside the note it
    relates to reads as the vault growing. So the closest existing note is
    returned by title — a title survives the renumbering that inserting a node
    causes, and an index does not.
    """
    text = body.text.strip()
    if len(text) < 3:
        raise HTTPException(400, "Nothing to record.")
    if vault.notes_dir() is None:
        raise HTTPException(404, "No notes folder configured.")

    store = vault.get_vault()
    try:
        note = store.capture(text)
    except vault.NotesError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError as exc:
        raise HTTPException(500, f"Could not write the note: {exc}") from None

    # Closest existing note by the same ranking the answers use, excluding the
    # one just written — it would otherwise always win against its own text.
    near = None
    for other, score in store.search(text, limit=3):
        if other.index != note.index and score >= MIN_SCORE:
            near = other.title
            break

    return {
        "reply": random.choice(CONFIRMATIONS),
        "note": {
            "id": note.index,
            "label": note.title,
            "group": note.folder,
            "excerpt": note.excerpt,
            "path": str(note.path),
        },
        "near": near,
        "generation": store.signature(),
    }
