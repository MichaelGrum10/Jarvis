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
 */

const AUDIO_ID = 'jarvis-voice';

let deps = { api: null, onStatus: null, fallback: null };
let element = null;
let analysed = false;
let disabled = '';        // why the cloned voice is off, '' when it is on
let current = null;       // the utterance in flight

export function initSpeech(options) {
  deps = { ...deps, ...options };
}

/** Is the cloned voice in play, and if not, why not. */
export function speechSource() {
  return disabled ? { source: 'browser', reason: disabled } : { source: 'cloned', reason: '' };
}

export function disableCloned(reason) {
  disabled = reason;
  deps.onStatus?.(reason);
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
    if (/not configured/i.test(err.message)) disableCloned('Cloned voice not configured');
    return browserVoice(line, { onStart, onEnd, why: err.message });
  }

  const audio = audioElement();
  const done = new Promise((resolve) => {
    let settled = false;
    const finish = (failure) => {
      if (settled) return;
      settled = true;
      audio.onplaying = audio.onended = audio.onerror = null;
      resolve(failure);
    };
    audio.onplaying = () => onStart?.({ source: 'cloned' });
    audio.onended = () => finish(null);
    // An <audio> reports a failed stream as an error event with no detail; the
    // status code is on the response we never see. Whatever it was, the answer
    // is the same: say it another way.
    audio.onerror = () => finish('The cloned voice stopped mid-sentence');
  });

  audio.src = prepared.url;
  current = audio;
  try {
    await audio.play();
  } catch (err) {
    current = null;
    return browserVoice(line, { onStart, onEnd, why: `Playback blocked (${err.name})` });
  }

  // Only once the element is playing, and only once ever: a media element can
  // be tapped by exactly one MediaElementSource, and a second attempt throws.
  if (!analysed) {
    analysed = deps.analyse?.(audio) ?? false;
  }

  const failure = await done;
  current = null;
  if (failure) return browserVoice(line, { onStart, onEnd, why: failure });
  onEnd?.();
  return undefined;
}

function browserVoice(line, { onStart, onEnd, why } = {}) {
  if (why && !disabled) deps.onStatus?.(why);
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
