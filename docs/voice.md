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

## One screen

There is no voice mode and no text mode any more — just the HUD, with a type bar
under the ring. **The reply comes back the way the question went in:** typed
questions are answered in writing, in a transcript panel that stays hidden until
there is something in it; spoken ones are answered out loud. Reading a reply you
are simultaneously being told is noise, and being talked at after typing is
worse.

The microphone button on the type bar is dictation only — it fills the box and
leaves the sending to you. Talking to the ring is the spoken path, and that one
records real audio for speaker verification.

## Where the panels sit

**Wide (980px and up)** — docked to the left and right edges, widths clamped so
they scale with the display rather than being one number that is cramped on a
laptop and lost on a monitor. Above 1600px they get wider and move further out.
Each panel is positioned individually against the viewport rather than placed in
a grid overlaying the screen: the container's `max-width: 1100px` used to
survive into the wide layout, which pinned the right-hand column 1100px from the
left edge — so on a large display it landed in the middle, on top of the ring.

**Narrow** — stacked under the ring, and the HUD scrolls. Centring a flex column
that overflows clips the top *and* makes it unreachable, so below the breakpoint
the HUD flows from the top instead.

Measured at seven sizes from a 393px phone to a 2560px ultrawide: docked from
iPad-landscape up, never overlapping the ring or the type bar, never off screen.

## Moving the panels

**On a wide screen only.** Drag a panel by its heading and it leaves the layout
and stays where you put it for the rest of the session. **Reset panel layout** in
the menu puts everything back, and so does reloading the page.

Positions are deliberately not remembered across a reload. A dragged panel is a
temporary rearrangement — moved aside to see something behind it — not a
preference, and a layout that persisted would turn one careless drag into a page
that stays wrong until you find the reset button.

On a phone the panels are stacked in normal flow and are not draggable at all —
dragging one out of the stack leaves a hole and drops it over whatever you were
reading, and the `touch-action: none` that dragging needs would eat the scroll
gesture the stacked layout depends on. Rotating or resizing across the
breakpoint moves them back into the stack and returns them to their positions
afterwards; going narrow clears the layout from the page but never from storage.

Panels are clamped inside the viewport on drop and again on every resize — a
layout saved on a laptop and reopened on a phone would otherwise leave panels
off the right-hand edge with no way to reach them except clearing site data.
Dragging works from the heading only: the bodies scroll, and a drag that starts
on content eats every attempt to scroll the inbox.

## The arc reactor

The circular element that pulses with Jarvis's voice. It sits inside the HUD's
ring, and moves into the galaxy while the galaxy is open — one canvas, re-parented,
rather than two that could disagree about what is happening. The ◎ button in the
galaxy's top bar hides it; the choice is remembered.

### Where the pulse comes from

Two sources, picked at runtime, because the browser will not hand over the same
signal in both cases.

**Audio we control** — a TTS service returning an mp3, or any `AudioNode` —
goes through an `AnalyserNode`. `getByteFrequencyData` every frame, averaged
over the bottom third of the spectrum because that is where speech lives:
averaging the whole range buries the voice under empty highs and the core barely
moves. This is real amplitude. `reactorAnalyse(source)` wires it.

**Web Speech synthesis** cannot do this. The browser owns that audio from end to
end and exposes no node to tap — there is no amplitude to read, at all, in any
browser. So the envelope is reconstructed from the utterance: `boundary` events
snap the position to where the engine actually is, and between them it advances
through the real text, rising on vowel runs and dropping at punctuation.

That last part is the difference between this and a timer. It is still a
reconstruction, and worth being clear about — but it is a reconstruction of the
sentence being spoken, so the pauses land where the speech pauses.

**On WebKit, `boundary` frequently never fires at all.** The text-derived
envelope is what carries it there, uncorrected. If the pulse looks slightly out
of step with the voice on Safari, that is why, and it is not fixable from this
side: the event and the audio are both withheld.

### Frame budget

Capped at 30fps, paused entirely when the tab is hidden, and drawn on one canvas
rather than in DOM elements — the galaxy is already using the GPU.

The cap has a 4ms tolerance, without which a display that is not 60Hz halves:
frames arriving every 33.2ms would miss a 33.33ms gate every time and render at
15. Measured at 29fps in a headless Chromium running rAF at 63Hz.

If frame times stay below 20fps for 1.5 seconds it drops to a plain two-ring
version with no gradients or blurs, and returns to the full one after five clean
seconds. The asymmetry is deliberate — quick to protect the frame rate, slow to
trust that the pressure is gone. **iOS Safari throttles `requestAnimationFrame`
hard while audio is playing**, which is exactly when this element matters, so
the trigger is measured frame time rather than a user-agent check.

Slowness is measured in milliseconds, not in frames: counting frames means the
slower the device, the longer it takes to notice it is slow.

### What has not been tested

Everything above was verified in headless Chromium, including the degrade path
(forced with 22× CPU throttling) and the analyser path (fed a real oscillator —
core radius 23px with tone, 19px in silence). **None of it has run on Safari,
on either platform.** The `boundary` behaviour and the rAF throttling are both
WebKit-specific, and both are unverified.

## The cloned voice

Two providers, one door. Set either pair in `.env` and Jarvis speaks in that
voice; set neither and he uses the browser's own voice and says so in the status
line.

| | Key | Voice id | Model |
| --- | --- | --- | --- |
| **Fish Audio** | `FISH_API_KEY` | `FISH_VOICE_ID` — your cloned voice's model id on fish.audio (the SDK calls it `reference_id`) | `FISH_MODEL`, default `s2-pro`; `s1` also current |
| **ElevenLabs** | `ELEVENLABS_API_KEY` | `ELEVENLABS_VOICE_ID` | `ELEVENLABS_MODEL`, default `eleven_turbo_v2_5` |

Fish is preferred when both are configured; `TTS_PROVIDER=fish` or
`=elevenlabs` forces one. Forcing a provider that has no key does **not** fall
through to the other — "I set ElevenLabs and it is still Fish" is the wrong
surprise — it disables the cloned voice and doctor names what is missing.

**The browser never talks to either service.** It asks this server, and the
server holds the key. That matters more than usual here: a TTS key is billed
per character and would be trivially scraped out of anything served to a phone.

**Fish's request shape was taken from its own Python SDK**, not from memory: a
msgpack body, the key as a Bearer token, and the model chosen by a `model`
*header* — put it in the body and Fish silently uses its default. A test pins
each of those.

Switching provider, voice or model re-renders every line: the cache key carries
all three, so a line ElevenLabs said is never played back as if Fish said it.

### How a line gets spoken

    POST /api/voice/speak   {text}          -> {url}          (device token)
    GET  /api/voice/audio/<hash>?t=<ticket> -> audio/mpeg     (ticket only)

Two steps, because of how audio actually plays. The textbook way to start on the
first chunk is to read `response.body` into a MediaSource — and plain MediaSource
does not exist on an iPhone, which rules it out on the device this is mostly used
from. An `<audio>` pointed at a URL plays mp3 progressively everywhere, starting
on the first bytes. But an `<audio>` cannot send an Authorization header, so the
URL carries a signed ticket instead.

The ticket is bound to one clip's hash and expires in five minutes. A leaked URL
replays one line the owner already heard; it cannot be pointed at a different
line, at another endpoint, or at the key.

**Streaming, not batch.** The batch endpoint returns nothing until the whole clip
is rendered — a couple of seconds of silence before he starts. `/stream` with
`optimize_streaming_latency` sends audio as it is produced.

**Cached by hash of the text, voice and model.** The boot greeting and the canned
confirmations are identical every time and are said many times a day; rendering
them again is money spent to receive the same bytes back. Written through a
`.part` file and renamed, so a stream that dies halfway cannot leave a truncated
clip to be served forever after.

**Falling back.** A bad key, exhausted credits, a network blip, a browser that
refuses to play — all of them land on the browser's own voice, and all of them
say why in the status line. A credit or quota failure disables the cloned voice
for the rest of the session rather than adding a doomed round trip to every
sentence.

## Interrupting him

While he speaks the microphone stays open and speech-band energy is watched. When
it stays up for 300ms, playback stops, the download is aborted and recognition
starts — the three together are what makes it feel like interrupting a person.

**Sustained, not peak.** Tested against three recordings played into the browser
as a fake microphone:

| input | peak level | interrupted? |
| --- | --- | --- |
| quiet room | 58 | no |
| coughs and doors (90ms bursts) | **209** | no |
| speech | 134 | yes, 300ms in |

The coughs peak *higher* than the speech and still do not trigger. That is the
whole point of the sustained window.

The threshold is relative to a floor learned from the room, so it does not need
tuning per microphone. The first 400ms after he starts is a calibration window —
an earlier version started the floor at zero and interrupted itself in a silent
room in 745ms.

**Echo cancellation is essential.** The mic is requested with
`echoCancellation`, `noiseSuppression` and `autoGainControl` all on. That is the
browser's own canceller and it is good, but not perfect on a speakerphone at
volume. **If he keeps interrupting himself, use headphones** — that removes the
path entirely rather than relying on cancellation.

## The wake word, and where it is not

**There is no browser wake word, and there cannot be.** It needs an always-open
recogniser, and WebKit requires a fresh user gesture for every `start()`. Safari
cannot do it, and every browser on iOS is WebKit underneath — Chrome included.
So the app says "wake word runs on the Mac, tap to talk here" rather than showing
an indicator that does nothing.

The wake word lives in the Mac agent instead: openWakeWord, locally, free, no
cloud. Say "Jarvis", it records what follows, sends it up the agent's existing
socket, and the answer plays back through the Mac's speakers.

### What leaves the Mac, and when

Nothing, until the word is heard. The model runs locally against a small ONNX
file; the microphone feeds a buffer that is continuously overwritten and never
written to disk. Only after a match does anything get sent — the few seconds of
speech that follow. Silence, background conversation, and everything before the
trigger are discarded in place.

### The mute

A visible indicator shows when the microphone is actually open. `"muted": true`
in `~/.jarvis-agent.json` means the stream is never opened at all — not opened
and ignored. The file is re-read continuously, so it takes effect within a second
and survives a restart.

`voice.mute` acts immediately. **`voice.unmute` does not** — it returns
`pending_confirmation` like any other write, because turning a microphone on from
a remote instruction is exactly what the confirmation gate exists for. Until that
gate is built, unmuting means editing the file on the Mac:

    python3 -c "import json,pathlib; p=pathlib.Path.home()/'.jarvis-agent.json'; d=json.loads(p.read_text()); d['muted']=False; p.write_text(json.dumps(d,indent=2))"
    launchctl kickstart -k gui/$(id -u)/com.jarvis.agent

If the config cannot be read at all, the agent stays muted — being unable to
prove that muting is off is not the same as it being on.
