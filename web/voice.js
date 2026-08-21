/* Voice: speech in, speech out.
 *
 * Input has two engines and picks per browser:
 *   - Web Speech API where it exists (Chrome, Edge, Safari). No round trip, and
 *     it streams interim results so you can see words appear as you talk.
 *   - MediaRecorder → /api/voice/transcribe → Groq Whisper everywhere else
 *     (Firefox, older iOS). Slower by a second, but far more accurate and it
 *     works in any browser that can record.
 *
 * Output is always the browser's speechSynthesis: free, instant, offline, and
 * no audio ever leaves the device on the way out.
 */

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;

/* Chrome on iOS is Safari underneath — Apple mandates WebKit for every browser
 * engine on the platform — so every quirk handled below applies on iOS no matter
 * which browser is installed. Testing in Chrome on a Mac tells you nothing about
 * Chrome on an iPhone. */
export const IS_IOS =
  /iPhone|iPod|iPad/.test(navigator.userAgent) ||
  (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
export const IS_WEBKIT =
  IS_IOS || (/Safari/.test(navigator.userAgent) && !/Chrome|Chromium|Edg/.test(navigator.userAgent));

export const voiceSupport = {
  get recognition() { return Boolean(SpeechRecognitionImpl); },
  get recording() { return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder); },
  get synthesis() { return 'speechSynthesis' in window; },
  get any() { return this.recognition || this.recording; },
  /* Both the microphone and speechSynthesis require a secure context. Served
   * from a remote host over plain HTTP, both fail *silently* — no exception, no
   * console error, simply nothing happens. Checking explicitly is the only way
   * to tell the user why. */
  get secure() { return window.isSecureContext === true; },
};

/* ---------------- audio unlock (Safari) ---------------- */

let unlocked = false;

/**
 * Unlock speechSynthesis from inside a user gesture.
 *
 * Safari refuses speak() unless the call chain traces back to a real gesture.
 * Our answers arrive from an async fetch, so by the time there is something to
 * say the gesture is long gone and Safari does nothing at all — no error, no
 * sound. Speaking a single space at volume 0 during the first tap marks the
 * audio session as user-initiated, and every later programmatic speak() works
 * for the rest of the page's life.
 *
 * resume() is here because Safari sometimes starts the engine paused; on a
 * paused engine speak() queues silently and never plays.
 *
 * Safe to call repeatedly — it does its work once.
 */
export function unlockSpeech() {
  if (unlocked || !voiceSupport.synthesis) return unlocked;
  try {
    const silent = new SpeechSynthesisUtterance(' ');
    silent.volume = 0;
    window.speechSynthesis.speak(silent);
    window.speechSynthesis.resume();
    unlocked = true;
  } catch {
    // Leave the flag down so the next gesture tries again.
  }
  return unlocked;
}

export const speechUnlocked = () => unlocked;

/**
 * Call before every speak(), not just the first.
 *
 * unlockSpeech() runs once by design — the silent utterance only needs saying
 * once. resume() is different: Safari pauses the synthesis engine when the tab
 * is backgrounded, when a call or another audio session interrupts, and
 * sometimes for no reason it shares. A paused engine accepts speak() and queues
 * it forever, with no error and no sound, which presents as "it worked once and
 * then went quiet". Resuming unconditionally costs nothing on an engine that is
 * already running.
 */
export function primeSpeech() {
  if (!voiceSupport.synthesis) return;
  try {
    window.speechSynthesis.resume();
  } catch { /* not fatal; the speak below may still work */ }
  unlockSpeech();
}

/* ---------------- voice list ---------------- */

let voicesPromise = null;

/**
 * The voice list, waited for properly.
 *
 * Safari returns [] from getVoices() on the first call and fires voiceschanged
 * unreliably — sometimes late, sometimes never. Waiting only on the event hangs
 * forever; reading only the array gets nothing. So: resolve on whichever of the
 * event or a one-second timeout arrives first, and re-read the array on the
 * timeout path, because by then it is usually populated even though nothing
 * announced it.
 */
export function getVoicesAsync(timeout = 1000) {
  if (voicesPromise) return voicesPromise;
  if (!voiceSupport.synthesis) return Promise.resolve([]);

  voicesPromise = new Promise((resolve) => {
    const ready = window.speechSynthesis.getVoices();
    if (ready.length) return resolve(ready);

    let settled = false;
    const finish = (voices) => {
      if (settled) return;
      settled = true;
      window.speechSynthesis.removeEventListener('voiceschanged', onChange);
      clearTimeout(timer);
      resolve(voices);
    };
    // addEventListener, never onvoiceschanged =. Assignment means whichever
    // consumer is constructed last silently destroys the other's handler, and
    // the loser never gets a voice at all.
    const onChange = () => finish(window.speechSynthesis.getVoices());
    const timer = setTimeout(() => finish(window.speechSynthesis.getVoices()), timeout);
    window.speechSynthesis.addEventListener('voiceschanged', onChange);
  });
  return voicesPromise;
}

/**
 * A British voice if one exists, and never null.
 *
 * The fallback chain matters on iOS: a null utterance.voice is handled poorly
 * there — sometimes silent, sometimes the wrong language — so anything is
 * better than nothing.
 */
export function pickBritishVoice(voices) {
  if (!voices?.length) return null;
  const chain = [
    (v) => v.lang === 'en-GB' && /Daniel|Serena|Kate|Oliver|Arthur|Martha/i.test(v.name),
    (v) => v.lang === 'en-GB' && /Siri/i.test(v.name),
    (v) => v.lang === 'en-GB',
    (v) => v.lang?.startsWith('en'),
    (v) => v.default,
  ];
  for (const test of chain) {
    const hit = voices.find(test);
    if (hit) return hit;
  }
  return voices[0];      // never leave it unset
}


/* ---------------- speech in ---------------- */

export class Listener {
  /* handlers: { onInterim, onFinal, onStart, onStop, onError } */
  constructor(handlers = {}) {
    this.h = handlers;
    this.active = false;
    this.mode = voiceSupport.recognition ? 'native' : (voiceSupport.recording ? 'whisper' : 'none');
    this.recognition = null;
    this.recorder = null;
    this.chunks = [];
    this.stream = null;
    this.silenceTimer = null;
  }

  async start({ continuous = false } = {}) {
    if (this.active) return;
    // On iOS, starting recognition while synthesis is still speaking tears down
    // the shared audio session: the mic opens, hears nothing, and ends. Killing
    // playback first is the whole fix, and it has to happen for every entry
    // point rather than only the one the HUD uses.
    if (voiceSupport.synthesis) window.speechSynthesis.cancel();
    this.active = true;
    this.h.onStart?.();
    try {
      if (this.mode === 'native') this._startNative(continuous);
      else if (this.mode === 'whisper') await this._startWhisper();
      else throw new Error('This browser cannot capture speech.');
    } catch (err) {
      this.active = false;
      this.h.onError?.(err.message || String(err));
      this.h.onStop?.();
    }
  }

  stop() {
    if (!this.active) return;
    this.active = false;
    clearTimeout(this.silenceTimer);
    try {
      if (this.mode === 'native') this.recognition?.stop();
      else if (this.recorder?.state === 'recording') this.recorder.stop();
    } catch { /* already stopped */ }
  }

  _startNative(continuous) {
    const recognition = new SpeechRecognitionImpl();
    this.recognition = recognition;
    recognition.lang = navigator.language || 'en-US';
    // Safari's support for continuous and interim results is poor: continuous
    // mode ends the session at unpredictable moments, and interim results
    // arrive malformed or not at all. Chrome keeps both, because the live
    // transcript is most of what makes dictation feel responsive.
    recognition.continuous = IS_WEBKIT ? false : continuous;
    recognition.interimResults = !IS_WEBKIT;
    recognition.maxAlternatives = 1;

    let finalText = '';

    recognition.onresult = (event) => {
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      this.h.onInterim?.((finalText + interim).trim());

      // In continuous mode the engine never decides you're done, so treat a
      // pause after real speech as the end of the utterance.
      if (continuous && finalText.trim()) {
        clearTimeout(this.silenceTimer);
        this.silenceTimer = setTimeout(() => this.stop(), 1400);
      }
    };

    recognition.onerror = (event) => {
      // 'aborted'/'no-speech' are normal end-of-turn noise, not failures.
      if (event.error !== 'aborted' && event.error !== 'no-speech') {
        this.h.onError?.(friendlyRecognitionError(event.error));
      }
    };

    recognition.onend = () => {
      this.active = false;
      clearTimeout(this.silenceTimer);
      const text = finalText.trim();
      if (text) this.h.onFinal?.(text);
      this.h.onStop?.();
    };

    recognition.start();
  }

  async _startWhisper() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    this.chunks = [];

    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg']
      .find((t) => MediaRecorder.isTypeSupported(t)) || '';
    this.recorder = new MediaRecorder(this.stream, mime ? { mimeType: mime } : undefined);

    this.recorder.ondataavailable = (e) => { if (e.data.size) this.chunks.push(e.data); };
    this.recorder.onstop = async () => {
      this.stream?.getTracks().forEach((t) => t.stop());
      this.active = false;
      const blob = new Blob(this.chunks, { type: mime || 'audio/webm' });
      // Anything this short is a mis-tap, not speech.
      if (blob.size < 1200) { this.h.onStop?.(); return; }
      this.h.onInterim?.('Transcribing…');
      try {
        const text = await transcribe(blob);
        if (text) this.h.onFinal?.(text);
        else this.h.onError?.('Nothing recognised.');
      } catch (err) {
        this.h.onError?.(err.message);
      } finally {
        this.h.onStop?.();
      }
    };

    this.recorder.start();
  }
}

function friendlyRecognitionError(code) {
  return {
    'not-allowed': 'Microphone access was blocked. Allow it in your browser settings.',
    'service-not-allowed': 'Microphone access was blocked by policy.',
    'audio-capture': 'No microphone found.',
    network: 'Speech recognition lost its network connection.',
  }[code] || `Speech recognition error: ${code}`;
}

async function transcribe(blob) {
  const form = new FormData();
  form.append('audio', blob, 'speech.webm');
  const lang = (navigator.language || '').slice(0, 2);
  if (lang) form.append('language', lang);

  const res = await fetch('/api/voice/transcribe', {
    method: 'POST',
    headers: { Authorization: `Bearer ${localStorage.getItem('jarvis_token')}` },
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Transcription failed (${res.status})`);
  }
  return (await res.json()).text.trim();
}

/* ---------------- speech out ---------------- */

export class Speaker {
  constructor() {
    this.enabled = true;
    this.voice = null;
    this.speaking = false;
    if (voiceSupport.synthesis) {
      // Shared, awaited, and cached — see getVoicesAsync. The previous version
      // assigned onvoiceschanged here and JarvisVoice assigned it again, so
      // whichever was constructed second destroyed the other's handler and one
      // of them never got a voice on Safari.
      getVoicesAsync().then((voices) => { this.voice = pickBritishVoice(voices); });
    }
  }

  speak(text, { onStart, onEnd } = {}) {
    if (!this.enabled || !voiceSupport.synthesis) return;
    const clean = speakable(text);
    if (!clean) return;

    this.cancel();
    // Every time: resume() revives an engine Safari paused behind our back,
    // which is the difference between "works once" and "keeps working".
    primeSpeech();
    // Long replies get chunked at sentence boundaries: Safari stops at roughly
    // 200-250 characters and Chrome truncates too, both without an error.
    const chunks = chunkForSpeech(clean);
    this.speaking = true;
    onStart?.();

    chunks.forEach((chunk, index) => {
      const utterance = new SpeechSynthesisUtterance(chunk);
      if (this.voice) utterance.voice = this.voice;
      utterance.rate = 1.03;
      utterance.pitch = 1.0;
      if (index === chunks.length - 1) {
        utterance.onend = () => { this.speaking = false; onEnd?.(); };
        utterance.onerror = () => { this.speaking = false; onEnd?.(); };
      }
      window.speechSynthesis.speak(utterance);
    });
  }

  cancel() {
    if (!voiceSupport.synthesis) return;
    window.speechSynthesis.cancel();
    this.speaking = false;
  }
}

/** Strip anything that reads badly out loud. */
export function speakable(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' code block ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, 'link')
    .replace(/[*_#>]+/g, '')
    .replace(/^\s*[-•]\s*/gm, '')
    .replace(/⚠️|✅|●/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

/** Split into utterances the engine won't truncate, breaking on sentences.
 *
 * Safari stops somewhere around 200-250 characters and gives no indication it
 * did — the sentence simply ends. 200 sits right on that edge, so WebKit gets
 * 180 and the margin. */
export function chunkForSpeech(text, limit = IS_WEBKIT ? 180 : 200) {
  const sentences = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const chunks = [];
  let current = '';
  for (const sentence of sentences) {
    if ((current + sentence).length > limit && current) {
      chunks.push(current.trim());
      current = sentence;
    } else {
      current += sentence;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

/* ---------------- mode ---------------- */

export const isMobile = () =>
  /Mobi|Android|iPhone|iPod/.test(navigator.userAgent) ||
  (/iPad|Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);

/**
 * Phones default to text, desktops default to voice — but the choice is yours on
 * either, and once you pick, that sticks.
 */
export function defaultMode() {
  const saved = localStorage.getItem('jarvis_mode');
  if (saved === 'text' || saved === 'voice') return saved;
  if (!voiceSupport.any) return 'text';
  return isMobile() ? 'text' : 'voice';
}

export function saveMode(mode) {
  localStorage.setItem('jarvis_mode', mode);
}
