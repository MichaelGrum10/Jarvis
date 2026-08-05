# Self-improvement

The AutoGPT-shaped part: give Jarvis a goal about its own codebase and it plans,
edits its source, runs the tests, reads the failures, and iterates until green.

## Turning it on

```bash
# .env
AUTONOMY_ENABLED=true
AUTONOMY_MAX_ITERATIONS=6
```

**If you run under Docker, you also need to give it a git repository.** The image
deliberately doesn't contain `.git`, and without one there's no branch to isolate
work on and no way to undo it. Uncomment this line in `docker-compose.yml`:

```yaml
    volumes:
      - jarvis-data:/data
      - ./:/app/repo:rw        # ← uncomment this
```

Then `docker compose up -d`. If you skip this, autonomy refuses to run and tells
you why rather than editing files with no way back.

Open the drawer → **Self-improve**.

It's off by default deliberately. This is the one subsystem that writes to the
code that runs it, so it should be a decision you make rather than a default you
inherit.

## What a run looks like

Give it something concrete:

> Add a weather tool using the free Open-Meteo API. It should use the device
> location from ToolContext and return current temperature and conditions.
> Include tests.

The loop:

1. **Branch.** `jarvis/auto/add-a-weather-tool-1754409600`, cut from your current HEAD.
2. **Explore.** `list_files`, `read_file` — it reads the existing tools to match the pattern.
3. **Edit.** `write_file` with complete file contents.
4. **Test.** `run_tests` executes `pytest -q` and returns the real output.
5. **Iterate.** On failure it reads the traceback, forms a hypothesis, and fixes the cause.
6. **Finish.** Commits to the branch and hands you a diff.

Then you review it yourself:

```bash
git log --oneline jarvis/auto/add-a-weather-tool-1754409600
git diff main...jarvis/auto/add-a-weather-tool-1754409600

git merge jarvis/auto/add-a-weather-tool-1754409600   # if you like it
git branch -D jarvis/auto/add-a-weather-tool-1754409600  # if you don't
docker compose up -d --build                          # to deploy
```

## The guardrails

This engine has write access to its own source, so the sandbox is the part worth
understanding. Every rule below is covered by a test in
`server/tests/test_autonomy.py`.

**Filesystem.** Every path is resolved to an absolute path and checked against the
repo root. `../../etc/passwd`, `jarvis/../../escape.py` and absolute paths are all
rejected. On top of that, `.env`, `.git/`, `data/` and virtualenvs are blocked
outright — it cannot read your credentials, and it cannot rewrite git history.

**Shell.** Not a shell. Commands are split with `shlex`, the binary is checked
against an allowlist (`pytest`, `python`, `ruff`, `git`, `ls`, `cat`, `grep`,
`find`, `wc`, `head`, `tail`), and anything containing `&&`, `||`, `;`, `|`, `>`,
`<`, backticks or `$(` is refused. Without that last rule the allowlist would be
decorative: `python -c pass && curl evil.com` starts with an allowed binary.

**Blast radius.** It never merges, never pushes, never deploys, never restarts the
server. Worst realistic case is a branch with bad code on it, which you delete.

**Budget.** Capped at `AUTONOMY_MAX_ITERATIONS` model turns and a 300s test
timeout, so a confused run stops instead of burning your Groq quota.

**Isolation.** Separate tool registry from the chat agent. A normal conversation
has no path into these tools — you can't talk Jarvis into rewriting itself
mid-chat.

## Writing a good goal

Specific goals work. Vague ones waste iterations.

Good:
- "The calendar tool crashes on all-day events because `_parse` assumes DTSTART is a datetime. Fix it and add a regression test."
- "Add a `mail_archive` tool that moves a message to the Archive mailbox by uid."
- "`stock_quote` is slow for multiple symbols. Profile it and cache `get_info` for 60 seconds."

Poor:
- "Make it better" — no success criterion, so it can't know when to stop
- "Add all the features" — exceeds the iteration budget before anything lands
- "Fix the bug" — which bug?

If you're reporting a crash, paste the actual traceback into the goal. It'll go
straight to the cause instead of spending two iterations reproducing it.

## Limits worth knowing

It runs the tests that exist. If a change breaks something untested, tests still
pass and it reports success — so review the diff, and ask for tests as part of the
goal.

It's also working with a 70B model on a free tier. It's genuinely good at
mechanical work — new tools following an existing pattern, targeted bug fixes,
adding coverage. It's much weaker at open-ended architecture. Scope accordingly:
one clear change per run beats one sweeping one.

Because it edits the running codebase, keep your own work committed before
starting a run. The engine branches from HEAD, so uncommitted changes come along
for the ride.
