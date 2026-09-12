# Voice identity and the HUD

Voice mode is a full-screen HUD with no text at all: you speak, the ring
responds, Jarvis answers out loud. Optionally it only answers *you*, and tells
you when someone else tries.

---

## Read this first

**Voice matching is a filter, not a security boundary.**

A recording of your voice, played at the microphone, will pass it. That is true
of every consumer speaker-verification system, and no amount of tuning here
changes it. What actually keeps strangers out of your assistant is the access
password, HTTPS, and the device token.

What voice matching genuinely gives you:

- Someone else in the room saying *"Jarvis, read my messages"* gets refused
- You find out it happened, on every device, with what they said

That is worth having. Treating it as a lock is not.

### How good is it, in numbers

The embedding is classical — MFCC statistics rather than a neural speaker model
— chosen so it runs in a few hundred lines of numpy on a free ARM instance
instead of pulling in a ~200MB PyTorch dependency.

Measured on synthetic speakers, at the default threshold of 0.78:

| | rate |
|---|---|
| Genuine attempts rejected | ~5% |
| Impostors accepted | ~7% |

The distributions **overlap** — a genuine attempt can score as low as 0.71, and a
similar-sounding impostor as high as 0.84. There is no threshold that separates
them cleanly, which is the honest reason this is described as a filter. Expect to
occasionally repeat yourself, and don't be shocked if someone with a similar
voice gets through.

Tune it for your situation:

```bash
VOICE_MATCH_THRESHOLD=0.85   # stricter: fewer impostors, more repeating yourself
VOICE_MATCH_THRESHOLD=0.72   # looser: smoother for you, more gets through
```

---

## The HUD

Voice mode hides the message list and the composer entirely. There's deliberately
no transcript: you already know what you said, and reading a reply you're
simultaneously being told is just noise.

State is carried by the ring alone:

| State | Look |
|---|---|
| **Idle** | Slow cyan drift |
| **Listening** | Brighter cyan, breathing, rings speed up |
| **Thinking** | **Amber, pulsing** — the core throbs while it works |
| **Speaking** | Bright cyan bloom in time with speech |
| **Not recognised** | Red, a short shake |

Tap the ring to talk. Tap it again while Jarvis is speaking to cut him off and
start listening — barge-in works, so you never wait out a long answer.

Switch modes with the **⌨ / 🎙** button in the top bar. The choice sticks per
device; phones still default to text, desktops to voice.

---

## The voice

Jarvis speaks through the browser's own speech synthesiser, choosing the closest
available voice to the character: British, male, unhurried. On Apple hardware
that's usually **Daniel** (en-GB). The rate is set slightly slow and the pitch
slightly low to match the delivery.

Being straight: **this is not the voice from the films.** Nothing free is. It's
the nearest thing the platform offers, and on an Apple device it's a reasonable
likeness. If you want the real thing you'd need a paid voice-cloning service and
the rights to that performance.

To hear better voices on macOS: **System Settings → Accessibility → Spoken
Content → System Voice → Manage Voices** and install a premium British voice.
iOS: **Settings → Accessibility → Spoken Content → Voices**.

Nothing is uploaded for speech output — synthesis happens entirely on your
device.

---

## Wake word

Say **"Jarvis"** and it starts listening. No tapping.

This uses the browser's own recogniser, which runs locally and sends no audio
anywhere — it exists only to spot the word. Once it fires, real recording begins.

Mishearings are tolerated, because short names get mangled constantly: "Travis",
"Harvis" and "Jarvus" all count. The tolerance is deliberately tight, though —
every false trigger opens the microphone and spends transcription quota, so
"marvellous" and "service" do not.

In the browser the wake word only works on Chrome, on the desktop, while the app
is in the foreground — and not at all on Safari or on any iOS browser, which
require a fresh tap for every listen. The always-on wake word lives on the Mac
agent instead ([voice.md](voice.md)); this isn't a smart speaker in a phone.

To enforce it server-side as well:

```bash
REQUIRE_WAKE_WORD=true
```

Then anything transcribed without a wake word near the start is ignored.

---

## Enrolling your voice

Tap **Enrol voice** under the ring. You'll read three short phrases.

Three separate recordings matter more than they sound: one captures a single
posture, distance and moment. Several average that out, and the spread between
them also tells the system how consistent your voice is — reported back as a
consistency percentage. Below about 55% it warns you, which usually means
background noise or more than one person talking.

Record somewhere reasonably quiet, at the distance you'd normally hold the phone.

### Turning enforcement on

Enrolment alone changes nothing. To make Jarvis actually refuse other voices:

```bash
# .env
REQUIRE_VOICE_MATCH=true
```

then `docker compose up -d`. Until you do, matches are scored but nothing is
turned away — which is a good way to calibrate first.

Check what you actually score before enforcing:

```bash
curl -X POST https://your-domain/api/identity/verify \
  -H "Authorization: Bearer YOUR_TOKEN" -F audio=@sample.wav
```

---

## Alerts

When an unrecognised voice speaks with enforcement on:

1. The request is **refused before reaching the model** — nothing is actioned
2. What was said is transcribed anyway, purely so the alert is useful
3. An alert is recorded, and pushed to **every** registered device — if it's a
   stranger, the phone in your pocket is where you want to hear about it
4. An email goes to your iCloud address (disable with `VOICE_ALERT_EMAIL=false`)

Review them:

```bash
curl https://your-domain/api/identity/alerts -H "Authorization: Bearer YOUR_TOKEN"
```

The app shows a banner for unacknowledged alerts when it opens.

**If the alerts are all you:** your enrolment probably isn't representative.
Re-enrol, or lower the threshold. A system that keeps refusing its owner gets
switched off, which protects nobody.

---

## What this costs

Nothing. Enrolment and verification run on your own server in numpy. Wake word
detection runs in the browser. Speech output runs on your device. Only
transcription touches Groq, on the free tier you already have.

---

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `REQUIRE_VOICE_MATCH` | `false` | Refuse voices that don't match |
| `VOICE_MATCH_THRESHOLD` | `0.78` | Similarity needed, 0–1 |
| `VOICE_ALERT_EMAIL` | `true` | Email on an unrecognised voice |
| `WAKE_WORD` | `jarvis` | The word that starts listening |
| `REQUIRE_WAKE_WORD` | `false` | Ignore anything without it |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Wake word never fires | Browser has no speech recognition (Firefox). Tap the ring instead |
| Wake word fires constantly | Something nearby sounds like "Jarvis" — try a longer `WAKE_WORD` |
| Always says "not recognised" | Re-enrol somewhere quiet, or lower the threshold |
| Enrolment says "silent" | Mic not picking up — check permission and that you're not muted |
| Voice sounds robotic | Install a premium British voice (see above) |
| No sound at all | iOS silent switch, or the tab lost audio permission |
| "No voice is enrolled" | `REQUIRE_VOICE_MATCH=true` with nothing enrolled — enrol, or turn it off |
