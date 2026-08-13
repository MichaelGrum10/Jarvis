# Your notes

Every `.md` file under this folder becomes a star in the knowledge galaxy, and
a source Jarvis can answer from. Sub-folders become clusters, so the shape of
the galaxy is the shape of your filing.

Two things link notes together:

- `[[Wikilinks]]`, the Obsidian syntax — `[[Punic Wars]]` links to
  `Punic Wars.md` wherever it lives;
- a note simply naming another's title in its text, because most vaults are
  only partly wikilinked.

YAML frontmatter (the `---` block at the top) is stripped before indexing, so
tags and dates don't pollute search.

## Using an Obsidian vault instead

Point the host side of the mount at it, in `.env`:

    NOTES_HOST_DIR=/home/ubuntu/vault

Then `docker compose up -d`. Leave `NOTES_DIR` alone — that is the path inside
the container, and it stays `/notes`.

Nothing here is synced anywhere by Jarvis. If you want this folder on your
phone as well, that is Obsidian Sync's job, or iCloud's, or git's.

## Saying "remember that…"

Anything Jarvis captures by voice lands in `captures/`, with frontmatter
recording when it was written and that Jarvis wrote it. They are ordinary
markdown files — move, rename, edit or delete them like any other note.
