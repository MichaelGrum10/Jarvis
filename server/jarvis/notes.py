"""A folder of markdown notes, indexed for search and for the graph.

This is the Obsidian answer from earlier, made concrete: a vault is just .md
files, so nothing here needs Obsidian's API, its sync, or its running. Point
NOTES_DIR at any folder of markdown — an Obsidian vault synced by git, an
exported Notion, a directory you write by hand — and it works.

Scoring is keyword overlap with titles weighted, not embeddings. That is a real
choice, not a shortcut: embeddings would need a model call per note on every
reindex, against a token budget that a single booking already strains, and for a
few thousand personal notes with distinctive vocabulary the overlap ranking is
good enough that the difference would not be visible.

The index lives in memory and rebuilds when a file's mtime moves. A vault of a
few thousand notes is a few megabytes — small enough that scanning it is
cheaper than maintaining a database that could disagree with the files.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_settings

log = logging.getLogger(__name__)

EXCERPT_CHARS = 700
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
# Words too common to carry meaning in a personal vault.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being
i me my we our you your he she it they them of in on at to for from with without by
as about into over after before under above do does did done have has had will would
can could should may might must not no yes what when where who whom which why how
""".split())


@dataclass
class Note:
    path: Path
    title: str
    folder: str
    text: str
    links: list[str] = field(default_factory=list)
    modified: float = 0.0
    # Position in the vault, which is also the node id in graph(). Search
    # reports it so the viewer can fly to a note without a second lookup.
    index: int = -1

    @property
    def excerpt(self) -> str:
        return self.text[:EXCERPT_CHARS]

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "folder": self.folder,
            "excerpt": self.excerpt,
            "path": str(self.path),
            "modified": dt.datetime.fromtimestamp(self.modified, dt.UTC).isoformat(),
        }


class NotesError(RuntimeError):
    pass


def notes_dir() -> Path | None:
    raw = get_settings().notes_dir
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", text.lower()) if w not in STOPWORDS}


class Vault:
    """The indexed notes, rebuilt when the files change."""

    def __init__(self) -> None:
        self._notes: list[Note] = []
        self._signature: tuple = ()

    def _scan(self, root: Path) -> list[Note]:
        found: list[Note] = []
        for path in sorted(root.rglob("*.md")):
            # Obsidian keeps its own state here and it is not notes.
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                modified = path.stat().st_mtime
            except OSError as exc:
                log.warning("Could not read %s: %s", path, exc)
                continue

            body = FRONTMATTER.sub("", raw).strip()
            relative = path.relative_to(root)
            found.append(
                Note(
                    path=relative,
                    title=path.stem,
                    folder=str(relative.parent) if str(relative.parent) != "." else "root",
                    text=body,
                    links=[m.group(1).strip() for m in WIKILINK.finditer(body)],
                    modified=modified,
                    index=len(found),
                )
            )
        return found

    def load(self, force: bool = False) -> list[Note]:
        root = notes_dir()
        if root is None:
            return []

        # Cheap staleness check: count and newest mtime. A vault changes by
        # someone editing a file, so this catches every case that matters
        # without stat-ing everything twice.
        try:
            paths = list(root.rglob("*.md"))
            signature = (len(paths), max((p.stat().st_mtime for p in paths), default=0.0))
        except OSError:
            signature = ()

        if force or signature != self._signature or not self._notes:
            self._notes = self._scan(root)
            self._signature = signature
            log.info("Indexed %d notes from %s", len(self._notes), root)
        return self._notes

    def search(self, query: str, limit: int = 6) -> list[tuple[Note, float]]:
        """Rank notes against a question. Title matches weigh more."""
        notes = self.load()
        wanted = _tokens(query)
        if not wanted or not notes:
            return []

        scored: list[tuple[Note, float]] = []
        for note in notes:
            title_hits = len(wanted & _tokens(note.title))
            body_hits = len(wanted & _tokens(note.text))
            if not (title_hits or body_hits):
                continue
            # A title match is a much stronger signal than one mention buried in
            # a long note, and without the weighting a single long note beats
            # every short one on sheer surface area.
            scored.append((note, title_hits * 5.0 + body_hits))

        scored.sort(key=lambda pair: (-pair[1], pair[0].title))
        return scored[:limit]

    def graph(self) -> dict:
        """Nodes and links for the viewer.

        Node ids are their index in the array, because the answer endpoint
        returns indexes and the viewer has to look them up without a second
        lookup table that could drift out of step.
        """
        notes = self.load()
        by_title = {n.title.lower(): n.index for n in notes}

        nodes = [
            {
                "id": n.index,
                "label": n.title,
                "group": n.folder,
                "excerpt": n.excerpt,
                "size": len(n.text),
            }
            for n in notes
        ]
        # Titles filed under their own first word. Checking every title against
        # every note is the obvious way to do this and it is quadratic in
        # substring searches — a 3,000-note vault would spend most of a minute
        # here. A note can only contain a title if it contains that title's
        # first word, so this looks at a handful of candidates instead of all of
        # them, and reaches the same answer.
        by_first_word: dict[str, list[tuple[str, int]]] = {}
        for note in notes:
            lowered_title = note.title.lower()
            words = re.findall(r"[a-z0-9']+", lowered_title)
            # Anything this short matches by accident: a note called "Q3" would
            # link to every note that mentioned a quarter.
            if words and len(lowered_title) > 4:
                by_first_word.setdefault(words[0], []).append((lowered_title, note.index))

        links: set[tuple[int, int]] = set()
        for note in notes:
            index = note.index
            lowered = note.text.lower()

            # Wikilinks are exact and go through the title map directly, so a
            # deliberate [[Q3]] still counts where a passing mention would not.
            for target in note.links:
                other = by_title.get(target.lower())
                if other is not None and other != index:
                    links.add((min(index, other), max(index, other)))

            # And a note that simply names another's title is a link too — most
            # vaults are only partly wikilinked.
            for word in set(re.findall(r"[a-z0-9']+", lowered)):
                for title, other in by_first_word.get(word, ()):
                    if other != index and title in lowered:
                        links.add((min(index, other), max(index, other)))

        return {
            "nodes": nodes,
            "links": [{"source": a, "target": b} for a, b in sorted(links)],
        }

    def capture(self, text: str) -> Note:
        """Write a new note into captures/, titled from its first few words."""
        root = notes_dir()
        if root is None:
            raise NotesError(
                "No notes folder configured. Set NOTES_DIR to a directory of markdown files."
            )

        words = re.findall(r"[A-Za-z0-9']+", text)[:8]
        stem = " ".join(words) or "Note"
        # Colons and slashes are legal in a title and not in a filename.
        safe = re.sub(r"[^\w\s-]", "", stem).strip()[:60] or "Note"

        folder = root / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe}.md"
        if path.exists():
            path = folder / f"{safe} {dt.datetime.now().strftime('%H%M%S')}.md"

        now = dt.datetime.now().astimezone()
        path.write_text(
            f"---\ncreated: {now.isoformat()}\ntags: [capture]\nsource: jarvis\n---\n\n{text}\n",
            encoding="utf-8",
        )
        self.load(force=True)
        log.info("Captured note %s", path.name)
        return next(n for n in self._notes if n.path.name == path.name)


_vault: Vault | None = None


def get_vault() -> Vault:
    global _vault
    if _vault is None:
        _vault = Vault()
    return _vault
