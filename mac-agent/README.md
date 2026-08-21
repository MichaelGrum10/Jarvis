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

## The wake word

Say "Jarvis" to the Mac and it answers out loud. This is the only place a wake
word can live: it needs an always-open recogniser, WebKit requires a fresh user
gesture for every `start()`, and every browser on iOS is WebKit underneath. So
the browser says "wake word runs on the Mac, tap to talk here" rather than
showing an indicator that does nothing.

    pip3 install --user openwakeword sounddevice

Both install per-user with no admin rights — the sounddevice wheel bundles
PortAudio, so there is no Homebrew step. Without them the agent runs exactly as
before and logs that the wake word is unavailable.

macOS will ask for **Microphone** permission the first time the stream opens.

### What leaves the Mac, and when

Nothing, until the word is heard. openWakeWord runs locally against a small ONNX
model; the microphone feeds a buffer that is continuously overwritten and never
written to disk. Only after a match does anything get sent — the few seconds of
speech that follow, over the socket that already exists. Silence, background
conversation, and everything before the trigger are discarded in place.

### Muting

`"muted": true` in `~/.jarvis-agent.json` means the microphone stream is never
opened — not opened and ignored, so the recording indicator in the menu bar goes
out. The file is re-read while running, so it takes effect within a second.

`voice.mute` acts immediately: turning a microphone off is the safe direction and
a mute that needs approval is not a mute. **`voice.unmute` returns
`pending_confirmation`** instead of acting, because turning one on from a remote
instruction is precisely what confirmation is for. To unmute now, edit the file
and restart the agent.

An unreadable config counts as muted — being unable to prove that muting is off
is not the same as it being on.

## Capabilities

| Command | Writes? | Live? | Returns |
| --- | --- | --- | --- |
| `mail.list_recent` | no | **yes** | recent inbox messages (`limit`, 1–50) |
| `mail.search` | no | **yes** | subject/sender matches for `query` |
| `calendar.list_events` | no | **yes** | events over the next `days` (1–30) |
| `mail.draft` | **yes** | stub | `pending_confirmation` with the proposed draft |
| `screen.capture` | no | stub | a PNG, base64, for `display` |
| `system.run_shortcut` | **yes** | stub | `pending_confirmation` with the shortcut name |
| `voice.status` | no | **yes** | whether the mic is open, muted, and how many wakes |
| `voice.mute` | no | **yes** | closes the microphone immediately |
| `voice.unmute` | **yes** | **yes** | `pending_confirmation` — never opens a mic unasked |
| `audio.play` | no | **yes** | plays a clip **from this server only** |

The three reads went first on purpose. They cannot do damage, they are the ones
that make Jarvis immediately more useful, and they are where you meet the
permission prompts for the first time — better on a command that can only read
than on one that can send mail.

Adding a capability means one decorated function. The dispatcher does not change
— a new capability should never be a reason to touch the code that decides what
is allowed to run.

### How the live ones talk to macOS

Through AppleScript in `scripts/`, run by `osascript`. The arguments arrive
through each script's `on run argv` handler, so a search term is *data* the
whole way down — it is never part of the script's source and never touches a
shell. There is no `do shell script` anywhere in these files, and a test asserts
that stays true.

Each script emits one record per result, fields separated by ASCII 31 and
records by ASCII 30. Control characters, because a subject line can contain a
comma, a tab, a quote or a newline, and a printable delimiter would turn "Lunch,
then dentist" into two events.

Dates come back as numeric components (`2026,8,21,15,0`) rather than as text.
AppleScript's `date string` is formatted for the machine's locale, so the same
event reads `21/08/2026` on one Mac and `8/21/26` on another — and both parse
wrong somewhere. The agent reassembles them and attaches the Mac's UTC offset,
which is what makes "3pm" still mean 3pm to a server in another timezone.

You can run any of them by hand, which is the fastest way to tell a permissions
problem from a parsing one:

    osascript ~/jarvis-agent/scripts/calendar_list.applescript 1
    osascript ~/jarvis-agent/scripts/mail_recent.applescript 3
    osascript ~/jarvis-agent/scripts/mail_search.applescript "lease" 5

Output that looks like one long line with invisible separators is correct.

**`mail.search` looks at subjects and senders, not message bodies.** Mail can
search bodies with `whose content contains`, but it does so by pulling every
message across one Apple event at a time — minutes on a real mailbox, against a
caller that gives up after twenty seconds. The response says so in a `searched`
field, so an empty result is never mistaken for an empty inbox.

## Install

Everything here is per-user. No administrator account is needed at any point.

    mkdir -p ~/jarvis-agent
    # copy jarvis_agent.py, com.jarvis.agent.plist AND the scripts/ folder
    # into ~/jarvis-agent — the AppleScripts are found next to the agent
    pip3 install --user websockets
    python3 ~/jarvis-agent/jarvis_agent.py

From a Mac that can reach the server over SSH:

    scp -r SERVER:Jarvis/mac-agent/jarvis_agent.py \
           SERVER:Jarvis/mac-agent/wake.py \
           SERVER:Jarvis/mac-agent/com.jarvis.agent.plist \
           SERVER:Jarvis/mac-agent/scripts ~/jarvis-agent/

The first run creates `~/.jarvis-agent.json` (mode 600) and stops. Put the
server URL and the shared secret in it:

    {
      "server": "wss://michael-jarvis.duckdns.org/api/agent/ws",
      "secret": "the same value as AGENT_SECRET on the server",
      "label": "michaels-laptop",
      "basic_auth": {"username": "your Caddy user", "password": "your Caddy password"}
    }

`basic_auth` is the site-wide HTTP basic auth Caddy enforces — the same username
and password your browser asks for. It is not optional here: without it Caddy
returns 401 and the handshake never reaches Jarvis. Leave `username` empty only
if you have removed basic auth from the site.

The agent's own secret travels in `X-Agent-Secret`, not `Authorization`, because
basic auth has already claimed `Authorization` and one request cannot carry two.
Two independent credentials guard the socket, which is the intent: the Caddy
password gets you to the door, the agent secret opens it.

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
| **Automation** | `mail.*`, `calendar.*` | Allow **python3** to control **Mail** and **Calendar** — each application appears as its own checkbox under the python3 entry |
| **Screen & System Audio Recording** | `screen.capture` | Add `/usr/bin/python3`. Also needs the agent restarted afterwards; macOS only re-reads this one at process start |

**Full Disk Access is probably not needed, contrary to what this file said
before.** That claim assumed reading Mail's store at `~/Library/Mail` directly.
These scripts don't — they ask Mail.app, and Mail reads its own data, which is
governed by Automation instead. If `mail.search` fails or returns nothing while
Automation is clearly granted, adding `/usr/bin/python3` to Full Disk Access is
the next thing to try, but start without it.

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
