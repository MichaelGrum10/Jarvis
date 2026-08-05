# Messages from an iPhone, without a Mac

## What an iPhone can and can't do

**It cannot read your message history.** iOS sandboxes `chat.db` so completely
that no app, script or Shortcut can reach it. This isn't a permission you can
grant — the file is simply not addressable from user space on iOS. Only a Mac can
read the archive.

**It can forward new incoming messages**, using a Shortcuts personal automation
that fires when a message arrives and POSTs it to your server.

So the honest comparison:

| | Mac bridge | iPhone Shortcut |
|---|---|---|
| Message history | ✅ full archive | ❌ nothing before setup |
| New incoming messages | ✅ automatic | ⚠️ if your iOS has the trigger |
| Group chats | ✅ | ⚠️ patchy |
| Sender names | ✅ from Contacts | ⚠️ often just a number |
| **Sending replies** | ✅ unattended | ❌ needs a tap every time |
| Runs unattended | ✅ | ⚠️ iOS may require confirmation |

The Shortcut route gives Jarvis awareness of what's arriving. It does **not** give
it the ability to reply on your behalf — iOS won't send a message without user
interaction, by design.

**If you own a Mac, use the Mac.** It doesn't need to be on continuously; power it
on when convenient and the bridge backfills from where it left off. This page is
for the gap in between, or for people with no Mac at all.

---

## Before you start

Check whether your iOS version actually has the trigger. Open **Shortcuts →
Automation → +** and look for a **Message** trigger ("When I get a message").

Apple has moved this around between releases and it isn't present in every
version. If you don't see it, this route isn't available to you and there's no
workaround — skip to [messages-bridge.md](messages-bridge.md) and use the Mac.

You'll also need your `BRIDGE_TOKEN`, the same value that's in your server's
`.env`. `scripts/setup.sh` prints it at the end.

---

## Build the Shortcut

**Shortcuts → Automation → + → Message**

1. Set the trigger to fire on messages from **anyone** (or restrict it to specific
   people — this is a good way to keep Jarvis focused on people who matter).
2. Turn **Run Immediately** on, and **Notify When Run** off, if your iOS offers
   them. Without "Run Immediately" you'll get a confirmation prompt for every
   message, which makes this useless in practice.
3. Add one action: **Get Contents of URL**.

Configure it:

| Field | Value |
|---|---|
| URL | `https://your-domain.duckdns.org/api/bridge/ingest` |
| Method | `POST` |
| Headers | `X-Bridge-Token` → your BRIDGE_TOKEN |
| | `Content-Type` → `application/json` |
| Request Body | **JSON** |

For the JSON body, add a field named `hostname` (Text) set to `iPhone`, and a
field named `messages` of type **Array** containing one **Dictionary** with:

| Key | Type | Value |
|---|---|---|
| `sender` | Text | the automation's **Sender** variable |
| `text` | Text | the automation's **Message** variable |
| `service` | Text | `iMessage` |

That's everything. The server fills in the timestamp and derives a stable ID from
the content, so duplicate deliveries collapse into one entry.

Sent as JSON it looks like:

```json
{
  "hostname": "iPhone",
  "messages": [
    { "sender": "+15551234567", "text": "are we still on for 7?", "service": "iMessage" }
  ]
}
```

---

## Test it

Send yourself a message from another device, then ask Jarvis *"any messages?"*

Or check directly:

```bash
curl -H "X-Bridge-Token: YOUR_TOKEN" https://your-domain.duckdns.org/api/bridge/health
```

`connected: true` with a recent `seconds_ago` means messages are arriving.

---

## Living with it

**No history.** Jarvis knows only what arrived after you set this up. Asking about
last week returns nothing, and it will say so rather than inventing an answer.

**Replies need you.** `messages_send` queues the message and reports it as queued,
but nothing delivers it until a Mac runs the bridge. Jarvis will tell you this
rather than claiming it sent something. If you want to reply from the phone, ask
Jarvis to draft the wording and paste it into Messages yourself.

**Automations are fragile.** iOS disables personal automations that error
repeatedly, and Low Power Mode delays them. If messages stop appearing, check
Shortcuts → Automation and confirm yours is still enabled.

**Battery.** One small HTTPS request per message is negligible. Restricting the
trigger to specific senders keeps it lower still.

---

## When the Mac comes back

Nothing to undo. Start the bridge on the Mac:

```bash
export JARVIS_URL="https://your-domain.duckdns.org"
export JARVIS_BRIDGE_TOKEN="same token"
python3 bridge/jarvis_bridge.py
```

It backfills the last 24 hours and takes over. The two sources share the same
deduplication, so anything the Shortcut already sent won't double up — the Mac's
real chat.db GUIDs are used where present.

You can leave the Shortcut running as a belt-and-braces layer for when the Mac
sleeps, or delete the automation. Either is fine.
