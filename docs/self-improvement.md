# Continuous self-improvement

Jarvis can watch itself fail, fix the failures, and verify the fix — on a timer,
without being asked.

---

## What this can and cannot do

**It cannot make the model smarter.** The reasoning comes from whatever Groq
serves. No amount of self-editing changes that, and anyone promising otherwise is
selling something.

**It can make everything around the model better**, which matters more than it
sounds:

- Bugs that keep happening get diagnosed and fixed
- Tools it keeps being asked for and doesn't have get built
- Slow paths get faster
- Test coverage grows, so future changes are safer

Over months that compounds into a genuinely better assistant. Through ordinary
maintenance, done automatically — not bootstrapping.

---

## How it decides what to work on

This is the part that makes it useful rather than destructive.

An agent told "improve yourself" with no evidence writes plausible churn: it
refactors things nobody complained about and invents features nobody wanted. So
it isn't told that. Every failure is recorded as it happens:

| Signal | Meaning |
|---|---|
| `tool_failure` | A tool returned an error — the richest signal, it names the broken capability |
| `llm_error` | The model call failed: rate limits, oversized requests, malformed tool calls |
| `missing_capability` | **Nothing errored** — but Jarvis said it *couldn't* do something |

That last one is the strongest signal for a missing feature and is invisible in
an ordinary error log, because technically nothing went wrong.

Similar failures are grouped by fingerprint, with volatile parts stripped first.
This matters more than it looks: a calendar error embeds a fresh event UID every
time, so without collapsing those, one recurring bug looks like fifty unrelated
ones and never rises to the top.

**A problem must recur at least 3 times before it's worked on.** A single failure
is usually a network blip, and rewriting working code to chase noise makes things
worse.

If nothing qualifies, it does nothing. That's a good outcome.

---

## The three modes

```bash
IMPROVE_MODE=off       # nothing runs (default)
IMPROVE_MODE=propose   # fix on a branch, run tests, notify you — you merge
IMPROVE_MODE=apply     # merge automatically when tests pass
```

### Why `propose` is the one to use

The risk of `apply` isn't bad code — the test suite catches most of that. It's
that **the assistant breaks itself at three in the morning while you're asleep,
and the thing you check your email with is now down.**

A branch waiting for review costs you two minutes. A bricked assistant costs you
the morning.

Start with `propose`. Read a few diffs. Move to `apply` once you've seen the kind
of change it actually makes.

### If you do use `apply`

It merges but **deliberately does not restart the server**. Restarting the
process that's running the merge, from inside that merge, is how you get a
container that dies halfway and comes back to a half-applied state. Your restart
policy picks up the new code on the next natural restart; the merge is the part
that needed automating.

---

## Turning it on

```bash
# .env
IMPROVE_MODE=propose
IMPROVE_INTERVAL_HOURS=12
AUTONOMY_ENABLED=true          # the engine that does the actual editing
```

Under Docker you also need to give it a git repository — uncomment the
`./:/app/repo` volume in `docker-compose.yml`. See
[autonomy.md](autonomy.md), which covers the sandbox: it can't read `.env`,
can't escape the repo, and gets an allowlisted shell rather than a real one.

---

## Watching it

```bash
curl https://your-domain/api/autonomy/health -H "Authorization: Bearer TOKEN"
```

Shows what's actually been failing, ranked, and what it would work on next.
**Worth reading even with self-improvement off** — it's the clearest picture of
where Jarvis is letting you down.

Run a cycle immediately instead of waiting:

```bash
curl -X POST https://your-domain/api/autonomy/improve-now -H "Authorization: Bearer TOKEN"
```

Review a proposed fix:

```bash
cd ~/Jarvis
git log --oneline --all | head
git diff main...jarvis/auto/some-branch
git merge jarvis/auto/some-branch && docker compose up -d --build
```

---

## What a cycle actually does

1. Read failures from the last 7 days, grouped and ranked
2. Pick the most frequent that's recurred 3+ times
3. Hand the sandboxed engine a goal naming the error, an example request, and an
   instruction to fix the root cause — explicitly *not* to paper over it with a
   try/except or by weakening a test
4. It reads the code, edits, runs the suite, reads failures, iterates
5. Tests pass → branch committed. Propose mode stops here and notifies you
6. The issue is marked resolved so it isn't worked on twice

---

## Honest limits

**It's a 70B model on a free tier.** Good at mechanical work — fixing a
traceback, adding a tool that follows an existing pattern, writing a regression
test. Much weaker at open-ended architecture. Expect competent maintenance, not
invention.

**It only knows what the tests know.** A change that breaks something untested
passes and reports success. This is why the goal always demands a regression
test, and why reviewing diffs in `propose` mode is worth the two minutes.

**It can only fix what it can see.** Something annoying that never throws an
error and never makes Jarvis say "I can't" is invisible to it. Tell it directly
via the Self-improve panel.
