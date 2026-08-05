# Messages bridge (iMessage on a Linux server)

## Why a bridge is necessary

There is no way around this one. Apple provides no server-side API for Messages —
no OAuth scope, no REST endpoint, nothing that iCloud credentials unlock. iMessage
traffic is end-to-end encrypted between registered Apple devices, and a Linux box
in Oracle Cloud cannot be one of those devices.

What every Mac *does* have is `~/Library/Messages/chat.db`: a plain SQLite database
holding your entire message history, written by Messages.app as it receives things.
Reading that database on a Mac you own is the supported path, and it's what every
tool in this space does.

So the bridge is a small script that runs on your Mac and does two jobs:

1. Reads new rows out of `chat.db` and pushes them to your Jarvis server
2. Polls the server for messages to send, and delivers them via AppleScript

Your Mac needs to be powered on and awake for this to work. A Mac mini tucked
behind a router is the usual answer; a laptop that sleeps will sync in bursts
whenever it wakes.

## What you need

- A Mac signed into the same iMessage account as your phone
- Python 3.9+ (macOS ships this — `python3 --version` to confirm)
- Full Disk Access for whatever runs the script

No `pip install`. The bridge is deliberately stdlib-only so there's nothing to
maintain on the Mac.

## Grant Full Disk Access

`chat.db` sits behind macOS's privacy protection, so this step is mandatory —
without it you'll get a `PermissionError` immediately.

1. **System Settings → Privacy & Security → Full Disk Access**
2. Click **+**
3. Add **Terminal** (`/Applications/Utilities/Terminal.app`)
4. Quit and reopen Terminal completely — the permission only applies to a fresh launch

If you'll run it under launchd, add `/usr/bin/python3` to that list as well.

## Run it

```bash
git clone https://github.com/MichaelGrum10/Jarvis.git
cd Jarvis

export JARVIS_URL="https://your-domain.com"
export JARVIS_BRIDGE_TOKEN="the exact BRIDGE_TOKEN from your server .env"

python3 bridge/jarvis_bridge.py
```

Expected output:

```
2026-08-05 11:00:01 INFO    Bridge 0.1.0 starting against https://your-domain.com
2026-08-05 11:00:03 INFO    Pushed 4 message(s), stored 4
```

Verify from the server side:

```bash
curl -H "X-Bridge-Token: YOUR_TOKEN" https://your-domain.com/api/bridge/health
# {"connected":true,"hostname":"mac.local","seconds_ago":3}
```

Useful flags:

| Flag | Effect |
|---|---|
| `--once` | Single cycle then exit — good for testing |
| `--no-send` | Mirror only, never send. Use while you're still building trust |
| `--interval 10` | Poll every 10s instead of 5 |

## Run it forever with launchd

`~/Library/LaunchAgents/com.jarvis.bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.jarvis.bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOU/Jarvis/bridge/jarvis_bridge.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>JARVIS_URL</key><string>https://your-domain.com</string>
        <key>JARVIS_BRIDGE_TOKEN</key><string>your-token</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/jarvis-bridge.log</string>
    <key>StandardErrorPath</key><string>/tmp/jarvis-bridge.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.jarvis.bridge.plist
tail -f /tmp/jarvis-bridge.log
```

Also worth doing: **System Settings → Lock Screen → Turn display off… → Never**
(on desktop Macs), and disable "Put hard disks to sleep" in Energy settings.

## How it behaves

**First run** starts 24 hours back rather than importing years of history. Progress
is stored in `~/.jarvis_bridge_state.json`; delete it to re-sync the last day.

**Deduplication** is on message GUID, server-side. Replays and restarts are
harmless — the ingest endpoint is idempotent.

**Reads are safe.** The script copies `chat.db` (plus its WAL sidecars) to a temp
directory before reading, so it never holds a lock on the live database and never
sees a torn write. It opens the copy read-only. It cannot corrupt your messages.

**Sending** goes through AppleScript against Messages.app. The recipient and text
are escaped before they reach the script, and the service is validated against a
fixed set (`iMessage` / `SMS`) rather than interpolated, so a malformed payload
can't inject AppleScript.

**Attachments and reactions** are skipped — they have no text body, so they'd just
be noise in a summary.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `PermissionError ... chat.db` | Full Disk Access not granted, or Terminal not restarted |
| `Server rejected the request (401)` | `BRIDGE_TOKEN` mismatch between Mac and server |
| Messages arrive empty | Normal for attachment-only or reaction messages |
| `bridge_warning` in Jarvis replies | Mac asleep or script stopped — check `launchctl list \| grep jarvis` |
| Sending fails | Messages.app must be running and signed in |

## If you don't have a Mac

Everything else works unchanged. The three `messages_*` tools stay hidden from the
model, so Jarvis simply won't claim it can read your texts. `system_status` will
report Messages as unconfigured.

A partial alternative: an Apple Shortcut on your iPhone can forward new incoming
messages to the ingest endpoint. No history, and iOS won't send replies
unattended — but Jarvis at least knows what's arriving. Full walkthrough in
[iphone-messages.md](iphone-messages.md).
