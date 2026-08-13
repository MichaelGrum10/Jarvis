# Notes and the knowledge galaxy

Point Jarvis at a folder of markdown and two things happen: it answers from what
you have written down instead of from what a model happens to remember, and the
whole folder becomes a 3D galaxy you can fly through — one star per note, threads
between the ones that reference each other.

Saying *"remember that the Oracle box reboots on Sundays"* writes a new file and
a new star appears in it.

---

## Turning it on

Nothing to do. The default mount is `./notes` in this checkout, so the feature
works out of the box on an empty folder — drop `.md` files in, or say *"remember
that…"* and Jarvis writes the first one.

To use an existing vault instead, put the **host** path in `.env`:

```bash
bash scripts/setkey.sh NOTES_HOST_DIR /home/ubuntu/vault
docker compose up -d
```

Leave `NOTES_DIR` alone. It is the path *inside* the container and stays
`/notes`; only the host side changes. Confirm what got picked up with:

```bash
docker compose exec jarvis python -m jarvis.doctor
```

which prints the note count, folder count and link count, or tells you exactly
which of the two paths is wrong.

### Getting an Obsidian vault onto the server

Jarvis reads the folder; it never syncs it. Whatever puts the files there is
your choice, and the options differ mainly in what happens when both ends edit
at once:

| How | Two-way | Notes |
|---|---|---|
| `git` clone + a cron `git pull` | yes, with conflicts you resolve | works with the Obsidian Git plugin; free |
| `rsync` from a machine that has the vault | one-way | simplest; server-side captures get overwritten |
| Obsidian Sync / iCloud Drive on a Mac, then rsync | one-way | needs a machine that stays on |

If you want *"remember that…"* to reach your phone, you need the two-way option.
With a one-way sync the captures live only on the server, which is fine if the
galaxy and Jarvis's own answers are what you wanted them for.

---

## What counts as a link

Two notes are joined when either:

- one contains `[[The Other Note]]` — Obsidian's wikilink syntax, matched
  against titles; or
- one simply names the other's title in its text.

The second rule exists because most real vaults are only partly wikilinked, and
a graph built on brackets alone shows a handful of threads and a cloud of
isolated dust. Titles shorter than five characters are ignored for this — a note
called `Q3` would otherwise link to everything that mentioned a quarter.

Sub-folders become clusters. YAML frontmatter is stripped before indexing, so
tags and dates never pollute search, and `.obsidian/` is skipped — it is
application state, not notes.

---

## How search works

Keyword overlap, with title matches weighted five times a body match. Not
embeddings.

That is a deliberate trade, not a shortcut. Embeddings would need a model call
per note on every reindex, against a free-tier budget where a single calendar
booking already spends most of a minute's tokens. For a few thousand personal
notes — which use your vocabulary, about your projects, with your names in them
— overlap ranking finds the right note, and the difference would not be visible
against what it costs.

The index lives in memory and rebuilds when any file's modification time moves,
so an edit in Obsidian is live on the next question.

---

## The galaxy

Open it from ☰ → **Knowledge galaxy**, or the **Galaxy** button on the voice HUD.

- **Drag** to orbit, **pinch** or scroll to zoom.
- **Tap a star** to read that note in full.
- **Search** in the bar at the top: the camera dives to the matches and lights
  them up, along with everything they link to.
- **⤢** pulls back out to the whole vault.

Ask a question by voice with the galaxy open and it flies to the notes the
answer came from as the answer arrives. That is the point of the thing: you can
see which of your own notes Jarvis used, and go read them.

It is drawn on a plain 2D canvas with a hand-written perspective projection —
no WebGL, no library, nothing fetched from a CDN, so it works with the phone
offline. The layout places folders as clusters on a sphere and lets links pull
notes together; it is honest about folders and honest about links, and does not
claim to be a global energy minimum.

---

## Where captures go

`captures/` inside the vault, one file per capture, with frontmatter recording
when it was written and that Jarvis wrote it:

```markdown
---
created: 2026-08-13T21:14:02+01:00
tags: [capture]
source: jarvis
---

The Oracle box reboots on Sundays
```

Ordinary markdown files. Move them, rename them, edit them, delete them — the
index follows.

Two names to keep apart:

- **`notes_remember`** — *"remember that the box reboots on Sundays"* — writes a
  note. For things you would want to read again.
- **`memory_save`** — *"remember I prefer morning meetings"* — writes a fact into
  the database that goes into every system prompt. For how you like things done.

The model picks between them, and the split matters for the token budget:
memory is paid for on every single turn, notes are paid for only when searched.

---

## If something looks wrong

| Symptom | Cause |
|---|---|
| "No notes folder configured" in the galaxy | `NOTES_DIR` empty — the container was started without the mount |
| Galaxy opens empty | the mount points at a folder with no `.md` files in it; `jarvis.doctor` prints which folder |
| A note is missing | it is under a dot-folder, or it is not `.md` |
| Everything is one cluster | every note is at the top level — sub-folders are what make arms |
| Jarvis answers from general knowledge instead | the notes genuinely didn't match; it is told to say so, and it will |
