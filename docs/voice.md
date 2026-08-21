# Voice and text modes

Jarvis opens in whichever mode suits the device, and you can switch on either.

| Device | Opens in | Why |
|---|---|---|
| iPhone, iPad, Android | **Text** | Usually in public, one hand, quicker to type |
| Mac, PC, Linux desktop | **Voice** | Sat down, mic already there, hands on other things |

The **⌨ / 🎙** button in the top bar flips between them. Your choice is remembered
per device and beats the default from then on — a phone you set to voice stays in
voice.

## Text mode

The normal composer, plus a **🎙** dictate button beside the input. Tap it, speak,
and the words land in the text box for you to edit before sending. Nothing is
spoken back at you.

Useful when you want voice input without voice output — dictating a long message
on a train, for instance.

## Voice mode

The composer is replaced by a tap-to-talk orb.

1. Tap the orb. It ripples while listening, and your words appear above it as you speak.
2. Stop talking. On desktop it detects the pause and sends automatically; tap again to send early.
3. The reply is spoken aloud, and also appears in the transcript with the usual cards.

**Barge-in works.** Tapping the orb while Jarvis is talking cuts it off and starts
listening — you don't have to wait for it to finish a long answer.

**■ Stop speaking** silences the current reply without starting a new one.
**⌨ Type instead** drops you back to text mode.

## How it works

**Speech in** has two engines, chosen per browser:

- **Web Speech API** — Chrome, Edge and Safari. Runs on-device, no round trip,
  and streams interim words as you talk. This is the fast path.
- **Groq Whisper** — everywhere else (Firefox, older iOS). The browser records,
  posts to `/api/voice/transcribe`, and the server hands it to
  `whisper-large-v3-turbo` on the same free API key. About a second slower, but
  noticeably more accurate, especially with names and background noise.

The client picks automatically; the hint under the orb tells you which one you got
("Tap to speak" vs "Tap to record, tap again to send").

**Speech out** is always the browser's own `speechSynthesis`. Free, instant,
works offline, and no audio ever leaves your device on the way out. Jarvis prefers
Siri and Google voices where they exist and falls back to the system default.

Replies are cleaned before speaking — code blocks, markdown and raw URLs read
terribly out loud, so they're stripped or replaced with "link". Long answers are
split at sentence boundaries because Chrome silently truncates long utterances.

## Requirements

**HTTPS is mandatory.** Browsers refuse microphone access over plain HTTP, exactly
as they refuse geolocation. If you followed `docs/setup.md` you already have it.

**Microphone permission** is asked for once per device, on the first tap. If you
dismiss it, the hint under the orb tells you what happened and how to re-enable it.

Installing to the Home Screen keeps the permission — you won't be asked again each
session.

## If speech isn't available

Some browsers can neither record nor recognise. Jarvis detects this at startup,
falls back to text mode, hides the mic button, and says so under the orb rather
than presenting a control that does nothing.

## Cost

Nothing. Synthesis is built into the browser. Recognition is either built into the
browser or runs on Groq's free tier alongside the chat model. Whisper transcription
draws on the same free quota, and a spoken sentence is a rounding error against it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Mic button missing | Browser supports neither recognition nor recording |
| "Microphone access was blocked" | Denied at the OS or browser level. iOS: Settings → Safari → Microphone |
| Nothing happens on tap | Not on HTTPS |
| Recognition stops mid-sentence | Normal for the Web Speech API on long pauses — it sends what it heard |
| No voice on replies | You're in text mode; switch with the top-bar button |
| Robotic voice | No premium voice installed. macOS: System Settings → Accessibility → Spoken Content → System Voice |
| Transcription 429 | Groq rate limit; retry shortly |

---

## Safari, and why it needs its own section

Safari is the harder target and almost all of its failures are **silent** — no
exception, no console warning, simply nothing happens. Chrome on iOS is Safari
underneath (Apple mandates WebKit for every engine on the platform), so all of
this applies on an iPhone regardless of which browser is installed.

**Audio unlock.** Safari refuses `speechSynthesis.speak()` unless the call chain
traces back to a user gesture. Answers here arrive from an async fetch, so by
the time there is something to say the gesture is long gone. On the first
interaction of any kind, `unlockSpeech()` speaks a single space at volume 0 and
calls `resume()` — Safari sometimes starts the engine paused, and on a paused
engine `speak()` queues silently forever. That one utterance marks the audio
session user-initiated for the rest of the page's life.

**Tap to wake.** Because of the above, the page cannot greet you on load. A
full-screen prompt asks for that one tap, unlocks inside the gesture, and only
then speaks. Once per session, tracked in `sessionStorage`.

**The voice list.** `getVoices()` returns `[]` on Safari's first call and
`voiceschanged` fires unreliably. `getVoicesAsync()` resolves on whichever of
the event or a one-second timeout comes first, and re-reads the array on the
timeout path — by then it is usually populated even though nothing announced
it. The result is cached and shared.

Note the helper uses `addEventListener`, not `onvoiceschanged =`. An earlier
version assigned the handler in two places, so whichever consumer was
constructed second destroyed the other's, and one of them stayed voiceless on
Safari forever.

`utterance.voice` is never left null: iOS handles a null voice poorly. The chain
is a named British voice → any `en-GB` → any `en-*` → the default → the first
voice that exists.

**Recognition.** `continuous` and `interimResults` are both forced off on
WebKit: continuous mode ends the session at unpredictable moments and interim
results arrive malformed or not at all. Chrome keeps both, because the live
transcript is most of what makes dictation feel responsive.

**One gesture per listen.** WebKit requires a fresh gesture for *every*
`recognition.start()`. Nothing restarts the mic programmatically — the button is
tapped once per turn, deliberately. This is also why the **wake word cannot work
on Safari or on any iOS browser**: keeping it alive means restarting recognition
from a timer with no gesture behind it, so `WakeListener` reports itself
unavailable there rather than failing quietly.

**Speak before listen.** On iOS, starting recognition while synthesis is still
speaking tears down the shared audio session — the mic opens, hears nothing, and
ends. `Listener.start()` cancels any in-progress speech first, for every entry
point rather than just the one the HUD uses.

**Long answers.** Safari stops at roughly 200-250 characters. Replies are split
on sentence boundaries and queued as separate utterances.

**Secure context.** Both the microphone and synthesis need one. Over plain HTTP
they fail silently in both browsers, so `window.isSecureContext` is checked
explicitly and a banner explains that HTTPS or an SSH tunnel is required.

## Where audio goes

Recognition and synthesis run **entirely in your browser**. Neither the
microphone stream nor the synthesised audio ever reaches the server.

One honest exception: browsers with no `SpeechRecognition` at all (Firefox, very
old iOS) fall back to `MediaRecorder` → `/api/voice/transcribe` → Groq Whisper,
which does upload the recording. That path never runs on Safari or Chrome, both
of which have the native API. If you want it gone absolutely, delete
`_startWhisper` and the `voice` router.
