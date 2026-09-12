# Continuous self-improvement

Jarvis can watch itself fail, fix the failures, and verify the fix — on a timer,
without being asked.

---

## What this can and cannot do

**It cannot make the model smarter by editing itself.** No amount of
self-editing changes what the model can reason about, and anyone promising
otherwise is selling something.

**You can change which model does the editing, and it is the one setting here
that changes the quality of the result.** With `ANTHROPIC_API_KEY` set, the
engine runs on Claude; without it, on the same free pool that answers your
calendar questions. Ordinary conversation stays on the free pool either way —
this key is spent only when something broke or you asked for something. See
[Which model writes the code](#which-model-writes-the-code).

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

A merge is not a deploy. The engine runs *inside* the container and merges into
the checkout on the host, but the server is built from an image — so until
something rebuilds, it has fixed a bug in a file it is not executing.

`scripts/autodeploy.sh` is that something, and it runs on the host because a
process cannot rebuild the container it is running in and still be around to see
whether that worked. `bash scripts/self-improve.sh apply` sets it up; every ten
minutes it checks whether the branch moved, and if so:

1. records the commit that is currently working
2. rebuilds and restarts
3. waits for `/api/health` to answer
4. **if it does not answer, resets to the recorded commit and rebuilds again**

That rollback is the guard rail that makes `apply` defensible. Nothing is lost
when it fires: the change is still on its own `jarvis/auto/*` branch, so a
rolled-back fix is a branch to read rather than something that vanished.

```bash
bash scripts/autodeploy.sh status      # what's deployed vs what's committed
bash scripts/autodeploy.sh             # ship it now
bash scripts/autodeploy.sh watch off   # stop deploying automatically
```

---

## Asking for a feature

You do not have to wait for something to break. Ask, and it gets built:

- **Out loud or in the type bar** — "add a tool that tracks parcels". Jarvis
  asks you to confirm first, showing the exact wording it will work from,
  because a model that can commission changes to its own source on a loose
  reading of "make this better" is one paraphrase away from rewriting something
  you liked.
- **In the app**, ☰ → Self-improve, which also shows which model is doing the
  editing and what is queued.
- **Over the API**: `POST /api/autonomy/request {"goal": "..."}`.

Requests are **queued rather than started immediately**, and they jump ahead of
bug reports — a person asking is a stronger signal than a counter crossing a
threshold. Queueing them through the same loop means one change in flight at a
time, the same test gate, and the same deploy-and-roll-back path. Two engines
editing one checkout is how you get a merge conflict with yourself.

In `apply` mode a request goes from typed to running, tested and deployed with
no further input. In `propose` mode it stops at a branch and tells you.

---

## When it acts

A failure no longer waits for the next tick. Recording one wakes the loop, which
then settles for a couple of minutes before looking — errors arrive in bursts,
and the first line of a burst is rarely the whole story.

```bash
IMPROVE_INTERVAL_HOURS=12       # the unprompted sweep
IMPROVE_SETTLE_SECONDS=120      # pause after being woken, before working
IMPROVE_MIN_OCCURRENCES=3       # how many times a thing must break to count
```

So in practice: a bug that happens three times is being worked on within
minutes, not at lunchtime.

---

## Which model writes the code

Two jobs, two models, and they are not the same job.

Answering you — what's on my calendar, read me that email — happens dozens of
times a day and is mostly tool dispatch. A free tier does it well, and that is
what the pool is for.

Editing this source is not that. It happens when something is already broken or
you have asked for a feature, it is the one place where a wrong answer costs you
a working assistant, and it is worth the best model available.

```bash
bash scripts/setkey.sh ANTHROPIC_API_KEY sk-ant-...
docker compose up -d
```

`ANTHROPIC_MODEL` defaults to `claude-opus-5` and `ANTHROPIC_EFFORT` to `high`
(reading unfamiliar code and forming a hypothesis about a failure is exactly the
work that repays thinking). Without a key the engine runs on the free pool,
which works and is worse. `python -m jarvis.doctor` says which is in play.

**This is an API key from console.anthropic.com, billed per token.** A claude.ai
subscription is a different product; its session token is not an API credential,
it expires within hours, and using one this way is against Anthropic's terms.
There is no way around that, and `agent/coder.py` says so in the same words
rather than leaving you to discover it.

---

## Turning it on

```bash
bash scripts/self-improve.sh propose    # or: apply
```

That sets all of it: the mode, `AUTONOMY_ENABLED`, the git mount the sandbox
needs — and, for `apply`, the auto-deploy timer. Setting `IMPROVE_MODE` alone
leaves the loop running and failing every cycle in a log nobody reads, which is
why it gets a script rather than a line in a README.

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
