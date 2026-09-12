/* Speaking out loud: the cloned voice first, the browser's own as the fallback.
 *
 * ## Why an <audio> element and not a fetch stream
 *
 * The textbook way to start playing on the first chunk is to read
 * `response.body` and push it into a MediaSource. That does not work on an
 * iPhone — plain MediaSource is not available there, which rules it out on the
 * device this is mostly used from. An <audio> pointed at a URL plays mp3
 * progressively in every browser here, starting on the first bytes, and gets
 * the same result with none of the fragility.
 *
 * The cost is that an <audio> cannot send an Authorization header, which is why
 * the server hands back a URL carrying a signed ticket for that one line.
 *
 * ## Falling back
 *
 * Anything can fail — the key, the quota, the network, the browser refusing to
 * play. Every failure lands on the browser's own voice rather than on silence,
 * and says why in the status line, because a voice that suddenly changes with
 * no explanation reads as a bug.
 *
 * A quota failure disables the cloned voice for the rest of the session. It is
 * not going to start working again this month, and retrying it before every
 * sentence adds a round trip to each one.
 *
 * ## Unlocking on iOS
 *
 * Safari on iOS only lets a media element play if play() was first called on
 * *that element* inside a user gesture. Every line here is played after a
 * network wait — ask the server, get a URL, then play — so the gesture is long
 * gone by the time play() runs, and it is refused with NotAllowedError. That
 * refusal used to land on the fallback, so the phone always spoke in the
 * device's voice and never said the real reason. unlockAudio() plays a tenth
 * of a second of silence through the element on the first tap, which is the
 * permission the later plays inherit.
 */

const AUDIO_ID = 'jarvis-voice';

let deps = { api: null, onStatus: null, fallback: null };
let element = null;
let analysed = false;
let unlocked = false;
let disabled = '';        // why the cloned voice is off, '' when it is on
let current = null;       // the utterance in flight

export function initSpeech(options) {
  deps = { ...deps, ...options };
}

/** A tenth of a second of silence, as a WAV the element can play at once.
 * Built here rather than shipped as a file: 44 bytes of header and zeros. */
function silentClip() {
  const rate = 8000;
  const samples = rate / 10;
  const buffer = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(buffer);
  const tag = (at, text) => { for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i)); };
  tag(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); tag(8, 'WAVE');
  tag(12, 'fmt '); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  tag(36, 'data'); view.setUint32(40, samples * 2, true);
  return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
}

/**
 * Call from inside a user gesture, once. Plays silence through the element so
 * that later, gesture-less play() calls are allowed. Harmless anywhere else.
 */
export function unlockAudio() {
  if (unlocked) return true;
  const audio = audioElement();
  try {
    audio.src = silentClip();
    const attempt = audio.play();
    // Refusal here is the pre-unlock state, not a failure worth reporting; the
    // next gesture tries again. A resolved play is the unlock.
    if (attempt?.then) attempt.then(() => { unlocked = true; }).catch(() => {});
    else unlocked = true;
  } catch { /* try again on the next gesture */ }
  return unlocked;
}

export const audioUnlocked = () => unlocked;

/** Is the cloned voice in play, and if not, why not. */
export function speechSource() {
  return disabled ? { source: 'browser', reason: disabled } : { source: 'cloned', reason: '' };
}

export function disableCloned(reason) {
  disabled = String(reason || 'off').replace(/\.$/, '');
  deps.onStatus?.(disabled);
}

function audioElement() {
  if (element) return element;
  element = document.createElement('audio');
  element.id = AUDIO_ID;
  element.preload = 'auto';
  // Never shown, but a real element in the document: a detached one is allowed
  // to be discarded mid-playback.
  element.style.display = 'none';
  document.body.appendChild(element);
  return element;
}

/**
 * Say something, and resolve when it has finished (or failed over).
 *
 * `onStart` fires when audio actually begins, not when it was requested —
 * anything keying off it (the reactor, the status line) would otherwise light
 * up during the silence before the first byte.
 */
export async function speakOut(text, { onStart, onEnd } = {}) {
  const line = String(text || '').trim();
  if (!line) { onEnd?.(); return; }

  stopSpeaking();

  if (disabled) return browserVoice(line, { onStart, onEnd });

  let prepared;
  try {
    prepared = await deps.api('/api/voice/speak', {
      method: 'POST',
      body: JSON.stringify({ text: line }),
    });
  } catch (err) {
    // 503 with nothing configured is a settled fact; a network blip is not.
    if (/not configured/i.test(err.message)) disableCloned('Fish Audio voice not configured');
    return browserVoice(line, { onStart, onEnd, why: err.message });
  }

  const audio = audioElement();
  let started = false;
  const done = new Promise((resolve) => {
    let settled = false;
    const finish = (failure) => {
      if (settled) return;
      settled = true;
      audio.onplaying = audio.onended = audio.onerror = null;
      resolve(failure);
    };
    audio.onplaying = () => { started = true; onStart?.({ source: 'cloned' }); };
    audio.onended = () => finish(null);
    // An <audio> reports a failed stream as an error event with no detail; the
    // status code is on the response we never see. explainFailure() asks the
    // server what it knows before this is shown to anyone.
    audio.onerror = () => finish(started ? 'The cloned voice stopped mid-sentence' : 'The clip could not be played');
  });

  audio.src = prepared.url;
  current = audio;
  try {
    await audio.play();
  } catch (err) {
    current = null;
    // NotAllowedError is the iOS unlock rule above, in words a person can act
    // on: tap once and the next line will be his. Anything else is most often
    // the server refusing the clip, and the server knows why.
    const why = err?.name === 'NotAllowedError'
      ? 'The phone blocked playback until you tap the screen'
      : await explainFailure(`Playback blocked (${err?.name || 'error'})`);
    return browserVoice(line, { onStart, onEnd, why });
  }

  // Only once the element is playing, and only once ever: a media element can
  // be tapped by exactly one MediaElementSource, and a second attempt throws.
  if (!analysed) {
    analysed = deps.analyse?.(audio) ?? false;
  }

  const failure = await done;
  current = null;
  if (failure) {
    const why = started ? failure : await explainFailure(failure);
    return browserVoice(line, { onStart, onEnd, why });
  }
  onEnd?.();
  return undefined;
}

/**
 * The element only ever says "not supported"; the reason — a rejected key, a
 * voice id that does not exist, no credit left — was in a 503 body it threw
 * away. Ask the server, which checks with Fish without rendering anything. A
 * settled reason (one that will not fix itself this session) also switches the
 * cloned voice off, so every later line is not a doomed round trip.
 */
async function explainFailure(fallbackWhy) {
  try {
    const status = await deps.api('/api/voice/speech-status?verify=1');
    const check = status?.check;
    if (check && !check.ok && check.error) {
      const why = String(check.error).replace(/\.$/, '');
      if (/rejected|does not exist|credits|failed to train|not configured/i.test(why)) disableCloned(why);
      return why;
    }
  } catch { /* fall through to the generic reason */ }
  return fallbackWhy;
}

function browserVoice(line, { onStart, onEnd, why } = {}) {
  // Reasons are sentences from the server and end in a full stop; the status
  // line adds its own punctuation.
  if (why && !disabled) deps.onStatus?.(String(why).replace(/\.$/, ''));
  return new Promise((resolve) => {
    deps.fallback?.(line, {
      onStart: () => onStart?.({ source: 'browser' }),
      onEnd: () => { onEnd?.(); resolve(); },
    }) ?? resolve();
  });
}

/** Stop immediately, and abort whatever is still downloading.
 *
 * Pausing alone leaves the request running, so the rest of a long sentence keeps
 * arriving after an interruption — billed, and ready to resume if the element is
 * ever played again. Clearing the source and reloading cancels it outright.
 */
export function stopSpeaking() {
  if (element) {
    element.pause();
    element.removeAttribute('src');
    try { element.load(); } catch { /* already torn down */ }
  }
  current = null;
}

export function isSpeaking() {
  return Boolean(current) && !current.paused;
}
