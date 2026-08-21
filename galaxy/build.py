#!/usr/bin/env python3
"""Scan a markdown vault and emit viewer/graph-data.js.

Standard library only, by requirement and by preference: this runs on a box
where adding a dependency means a pip install that has to be maintained, and
nothing here is hard enough to justify one.

The vault is a Syncthing target, which is the constraint that shapes this file.
Files appear, vanish and get rewritten *while the walk is running* — Syncthing
writes to a temporary name and renames over the top, deletes propagate from
another machine mid-scan, and a whole directory can disappear between being
listed and being entered. So nothing here assumes a listing is still true by
the time it is used: every read is guarded, and the node list is built from
files that were actually read rather than from what os.walk reported.

Node ids are positions in the nodes array, because the viewer looks nodes up by
index. That makes ordering load-bearing, so the scan is sorted by relative path
— without that, os.walk's arbitrary order would reshuffle every id on each run
and any saved reference to "node 12" would silently point somewhere else.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_NOTES = Path("/home/ubuntu/Jarvis/notes")
EXCERPT_CHARS = 700

# Frontmatter is metadata, not prose: leaving it in puts "tags:" and a date at
# the top of every excerpt and pollutes title matching.
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
WORD = re.compile(r"[a-z0-9']+")

# A title shorter than this matches by accident — a note called "Q3" would link
# to everything that mentioned a quarter. Short titles still link when they are
# deliberately bracketed as a wikilink.
MIN_LOOSE_TITLE = 5


def notes_dir() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).expanduser()
    return Path(os.environ.get("NOTES_DIR", DEFAULT_NOTES)).expanduser()


def scan(root: Path) -> list[dict]:
    """Every readable .md under root, sorted by path so ids are reproducible."""
    found: list[dict] = []

    # onerror swallows a directory that vanished mid-walk; the default is to
    # silently skip it anyway, but being explicit documents that it is expected.
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        # Pruning in place stops os.walk descending. Dropping every dot-directory
        # covers .stfolder and .stversions (Syncthing's own state, and its
        # archive of superseded copies — which would otherwise appear as dozens
        # of duplicate stars) as well as .obsidian, and anything similar added
        # later without needing this list updated.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

        for name in sorted(filenames):
            # iCloud placeholders. The .md test below already excludes the
            # usual ".Note.md.icloud" form, but the check is explicit because
            # the requirement is explicit, and a stub must never become a node:
            # it is a few hundred bytes of plist, not a note.
            if name.endswith(".icloud") or name.startswith("."):
                continue
            if not name.endswith(".md"):
                continue

            path = Path(dirpath) / name
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                # Deleted or replaced between listing and reading. Normal here.
                continue

            relative = path.relative_to(root)
            body = FRONTMATTER.sub("", raw).strip()
            folder = str(relative.parent) if str(relative.parent) != "." else "root"

            found.append(
                {
                    "label": path.stem,
                    "group": folder,
                    "excerpt": body[:EXCERPT_CHARS],
                    "path": relative.as_posix(),
                    "_text": body.lower(),
                    "_links": [m.group(1).strip().lower() for m in WIKILINK.finditer(body)],
                }
            )

    found.sort(key=lambda n: n["path"])
    for index, node in enumerate(found):
        node["id"] = index
    return found


def link(nodes: list[dict]) -> list[dict]:
    """Join notes that reference each other.

    Two rules: an explicit [[wikilink]], and one note simply naming another's
    title. The second exists because most real vaults are only partly
    wikilinked, and a graph built on brackets alone is mostly dust.

    Checking every title against every note body is the obvious implementation
    and is quadratic in substring searches — a few thousand notes spend most of
    a minute in it. A note can only contain a title if it contains that title's
    first word, so titles are filed under their first word and only a handful of
    candidates are tested per note. Same answer, and it stays interactive.
    """
    by_title: dict[str, int] = {}
    by_first_word: dict[str, list[tuple[str, int]]] = {}

    for node in nodes:
        title = node["label"].lower()
        by_title.setdefault(title, node["id"])
        words = WORD.findall(title)
        if words and len(title) >= MIN_LOOSE_TITLE:
            by_first_word.setdefault(words[0], []).append((title, node["id"]))

    edges: set[tuple[int, int]] = set()
    for node in nodes:
        here = node["id"]

        for target in node["_links"]:
            other = by_title.get(target)
            if other is not None and other != here:
                edges.add((min(here, other), max(here, other)))

        text = node["_text"]
        for word in set(WORD.findall(text)):
            for title, other in by_first_word.get(word, ()):
                if other != here and title in text:
                    edges.add((min(here, other), max(here, other)))

    return [{"source": a, "target": b} for a, b in sorted(edges)]


def write(nodes: list[dict], links: list[dict], out: Path) -> None:
    """Write atomically: the viewer may be fetching this file right now, and a
    half-written graph-data.js is a syntax error rather than a partial graph."""
    public = [
        {"id": n["id"], "label": n["label"], "group": n["group"],
         "excerpt": n["excerpt"], "path": n["path"]}
        for n in nodes
    ]
    payload = json.dumps({"nodes": public, "links": links}, ensure_ascii=False)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(
        "// Generated by build.py. Do not edit — regenerate instead.\n"
        f"const GRAPH = {payload};\n",
        encoding="utf-8",
    )
    tmp.replace(out)


def main() -> int:
    root = notes_dir()
    if not root.is_dir():
        print(f"No such notes folder: {root}", file=sys.stderr)
        print("Pass one as an argument, or set NOTES_DIR.", file=sys.stderr)
        return 1

    nodes = scan(root)
    links = link(nodes)
    out = Path(__file__).resolve().parent / "viewer" / "graph-data.js"
    write(nodes, links, out)

    groups = len({n["group"] for n in nodes})
    print(f"{len(nodes)} notes · {groups} folder(s) · {len(links)} links")
    print(f"wrote {out}")
    if not nodes:
        print(f"(nothing matched *.md under {root})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
