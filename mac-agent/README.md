# The Mac companion agent

Some things Jarvis wants are simply not on the server. Apple Mail and Calendar
live behind AppleScript, screen capture needs a logged-in graphical session, and
Shortcuts is a macOS binary. None of that can be reached from an Oracle VM in
Frankfurt. So a small agent runs on the Mac and offers those capabilities over
one socket.

## Which way the connection goes

The Mac dials the server. The server never dials the Mac.

That is the whole reason this design exists. The Mac is behind NAT on a domestic
connection with no static address, so for the server to reach in, a port would
have to be forwarded on the home router — a standing invitation to anyone who
scans it, permanently, in exchange for saving a single outbound connection. An
outbound WebSocket rides the HTTPS endpoint that already exists, needs no router
configuration, and survives the address changing.

    Mac agent  ──── wss:// ────>  Caddy  ──>  Jarvis  /api/agent/ws

Caddy's existing `reverse_proxy` handles the WebSocket upgrade without any
configuration change.

## Safety rules

Three, in order of how badly they would end:

1. **Commands are named, and the names are an allowlist.** An unrecognised name
   is refused and logged. It never reaches anything that could run it — the
   refusal is a dictionary miss, not a check someone remembered to write.
   Both sides enforce the list independently, so a compromised server still
   cannot ask the Mac for something outside it.
2. **Nothing from the socket reaches a shell.** Commands take typed, structured
   arguments; there is no path from a received string to `subprocess`,
   `os.system`, `eval`, or AppleScript source. When these stubs get real
   implementations, arguments must be passed as argv elements — never
   interpolated into a command string.
3. **Anything that writes returns `pending_confirmation` instead of acting.**
   `mail.draft` and `system.run_shortcut` hand back a proposal. Reading mail and
   sending mail are not the same risk, and that difference is structural here
   rather than something the code is trusted to remember.

Every received command is appended to `~/jarvis-agent.log` with a timestamp,
before it runs and again with its outcome — so a command that hangs still leaves
proof it arrived, and the record of what the server asked for is independent of
what the server says it asked for.

## Capabilities

All six return fake data for now. The shapes are real; later prompts wire them
to the actual applications.

| Command | Writes? | Returns |
| --- | --- | --- |
| `mail.list_recent` | no | recent messages (`limit`, 1–50) |
| `mail.search` | no | matches for `query` |
| `mail.draft` | **yes** | `pending_confirmation` with the proposed draft |
| `calendar.list_events` | no | events over the next `days` (1–30) |
| `screen.capture` | no | a PNG, base64, for `display` |
| `system.run_shortcut` | **yes** | `pending_confirmation` with the shortcut name |

Adding a capability means one decorated function. The dispatcher does not change
— a new capability should never be a reason to touch the code that decides what
is allowed to run.

## Install

Everything here is per-user. No administrator account is needed at any point.

    mkdir -p ~/jarvis-agent
    # copy jarvis_agent.py and com.jarvis.agent.plist into ~/jarvis-agent
    pip3 install --user websockets
    python3 ~/jarvis-agent/jarvis_agent.py

The first run creates `~/.jarvis-agent.json` (mode 600) and stops. Put the
server URL and the shared secret in it:

    {
      "server": "wss://michael-jarvis.duckdns.org/api/agent/ws",
      "secret": "the same value as AGENT_SECRET on the server",
      "label": "michaels-laptop"
    }

On the server, generate the secret and put it in `.env` — over SSH, not through
chat:

    openssl rand -hex 32

    ./scripts/setkey.sh AGENT_SECRET <the value it printed>

Then run the agent again. It should print `Connected to wss://…`.

## Start it at login

    sed -i '' "s/YOUR-USERNAME/$(whoami)/g" ~/jarvis-agent/com.jarvis.agent.plist
    cp ~/jarvis-agent/com.jarvis.agent.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.agent.plist

Check it, stop it, and reload it after an edit:

    launchctl print gui/$(id -u)/com.jarvis.agent | head -20
    launchctl bootout gui/$(id -u)/com.jarvis.agent
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.agent.plist

(`launchctl load`/`unload` still work on current macOS but are deprecated; the
`bootstrap`/`bootout` pair above is the supported spelling and gives real error
messages when something is wrong.)

It is a LaunchAgent rather than a LaunchDaemon on purpose: Mail, Calendar and
screen capture only exist inside a logged-in graphical session, so a root daemon
starting at boot would find nothing to talk to.

## When your Mac sleeps

It will, several times a day, and it will come back to a socket the server has
already given up on. The agent reconnects on its own with exponential backoff —
1s, 2s, 4s, up to 60s, with jitter so a server restart does not bring every
agent back in the same millisecond. A clean session resets the delay. You should
not have to do anything; `~/jarvis-agent.log` will show the reconnects.

The server keeps one agent at a time and lets a new connection replace an old
one, because a Mac waking from sleep reconnects long before the dead socket's
TCP timeout notices. Refusing the new connection would lock the agent out until
a timer expired.

## macOS permissions

**System Settings → Privacy & Security.** Three panes, one per capability
family:

| Pane | Needed by | What to approve |
| --- | --- | --- |
| **Automation** | `mail.*`, `calendar.*` | Allow the agent's Python to control **Mail** and **Calendar** — each application appears as its own checkbox under the Python entry |
| **Full Disk Access** | `mail.search` | Add `/usr/bin/python3`. Mail's message store lives in `~/Library/Mail`, which is protected regardless of file permissions |
| **Screen & System Audio Recording** | `screen.capture` | Add `/usr/bin/python3` |

**The prompts appear only on the first invocation of each capability, and only
when something actually tries to use it.** That has two consequences worth
knowing before you go looking for a problem that is not there:

- Installing the agent grants nothing and prompts for nothing. The panes stay
  empty until a command runs.
- A dialog raised by a background LaunchAgent is easy to miss, and if it is
  dismissed or times out, macOS records that as a **denial** and does not ask
  again. The capability then fails silently forever. If a command starts
  returning a permission error, open the relevant pane and turn the switch on by
  hand — you will not get a second prompt.

Screen Recording additionally requires the agent to be restarted after you grant
it; macOS only re-reads that permission at process start.

Right now all six commands are stubs and touch none of this. The prompts will
start appearing when the real implementations land — which is the moment to
approve them, one at a time, so it is obvious which capability asked.

## Calling it from the server

    curl -s -X POST http://127.0.0.1:8000/api/agent/call \
      -H 'Content-Type: application/json' \
      -H "Cookie: $JARVIS_COOKIE" \
      -d '{"command": "mail.list_recent", "args": {"limit": 3}}'

`GET /api/agent/status` reports whether an agent is connected, its label, and
how many calls are in flight. Both endpoints need a logged-in device — the Mac's
capabilities are not anonymous.

If no agent is connected the call returns **503** with a plain explanation
rather than hanging. If the agent is connected but does not answer within 25
seconds, the same 503 with a timeout message.
