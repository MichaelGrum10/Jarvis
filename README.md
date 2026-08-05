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
| Conversation + reasoning | Groq (`llama-3.3-70b-versatile`), full tool calling | free tier |
| **Email** — triage, expand, search, send | iCloud IMAP/SMTP, app-specific password | free |
| **Calendar** — read, book, delete, find free slots | iCloud CalDAV | free |
| **Messages** — read iMessage/SMS, send | Mac bridge reading `chat.db` | free, needs a Mac |
| **Stocks** — quotes, history, ticker news | yfinance | free, no key |
| **WSJ headlines** | WSJ public RSS feeds | free |
| **Web search + page reading** | SearXNG → Brave → DuckDuckGo | free |
| **Nearby places** — "I need a haircut" | OpenStreetMap, using *device* location | free, no key |
| **Open apps on your device** | URL schemes + Apple Shortcuts | free |
| **Memory** | SQLite, injected into every prompt | free |
| **Self-improvement** | Sandboxed agent that edits its own code and runs tests | free |

29 tools, all registered through one plugin-style registry — adding a capability
means dropping a file in `server/jarvis/tools/`.

---

## Read this before you start

Three things about the request are worth being straight about up front.

**iMessage cannot be read from a Linux server.** Apple ships no server API for
Messages, and no amount of credentials changes that. The only supported path is a
Mac you own: `bridge/jarvis_bridge.py` reads the local `chat.db` and mirrors
messages to your server. If you have a Mac (or a Mac mini left powered on), this
works properly and sends replies too. If you don't, every other feature still
works — the messages tools just stay hidden. **This is the one capability with a
hardware prerequisite.**

**WSJ headlines come from their public feeds, not your account.** The Journal
publishes free RSS for every section with headlines and summaries, and that's what
Jarvis reads. It does *not* log in with your subscription to scrape article text —
automating a paywall login breaches the WSJ's terms of use and risks your account
getting flagged. You get every headline and summary; tap through to read the full
piece signed in. If you specifically want full article text automated, that's a
decision to make knowingly, and it isn't what this ships with.

**"Free" has one honest asterisk.** The software and every API are free. The
Oracle Cloud Always Free tier covers the server. A domain name is ~$10/year, and
you do need one: browsers refuse to hand out geolocation over plain HTTP, so the
"near me" feature depends on a real HTTPS certificate. A DuckDNS subdomain works
free if you'd rather not buy one.

---

## Setup

### 1. Server

On your Oracle Cloud instance (Ubuntu 22.04+, the free ARM shape is plenty):

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker

git clone https://github.com/MichaelGrum10/Jarvis.git
cd Jarvis
cp .env.example .env
```

Fill in `.env`. The two secrets:

```bash
openssl rand -base64 32   # → AUTH_SECRET
openssl rand -base64 32   # → BRIDGE_TOKEN
```

Then set `ACCESS_PASSWORD` (what you type once per device) and `GROQ_API_KEY`
(free, no card, from <https://console.groq.com/keys>).

### 2. Apple credentials

Both mail and calendar use one **app-specific password** — not your Apple ID
password, and revocable at any time without touching your account.

1. <https://account.apple.com> → Sign-In and Security → App-Specific Passwords
2. Generate one, label it "Jarvis"
3. Put it in `.env` as `ICLOUD_APP_PASSWORD`, with your Apple ID in `ICLOUD_EMAIL`

Calendar reuses those automatically.

### 3. Domain and TLS

Point a domain at your instance's public IP, then edit `Caddyfile` and replace
`jarvis.example.com`. Open ports 80 and 443 in both the Oracle security list *and*
the instance firewall:

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Also set a real `secret_key` in `searxng/settings.yml`.

### 4. Launch

```bash
docker compose up -d
docker compose logs -f jarvis
```

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

---

## Self-improvement

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
.venv/bin/pytest -q          # 43 tests
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
  tools/           29 tools, one file per domain
  integrations/    iCloud CalDAV + IMAP
  api/             auth, chat, device, bridge, autonomy routes
web/               installable PWA
bridge/            Mac iMessage bridge (stdlib only)
```

---

## Security

One password guards everything, so treat it like a house key.

- Device tokens are signed and expire after 90 days; rotating `AUTH_SECRET`
  revokes every device instantly
- The bridge uses a *separate* secret, so a compromised phone token still can't
  forge messages into your history
- TLS via Caddy with automatic Let's Encrypt renewal; the app port binds to
  localhost only
- Your Apple app-specific password is revocable without disturbing your Apple ID
- Nothing leaves your server except calls to Groq, and to the specific public
  APIs a tool needs
