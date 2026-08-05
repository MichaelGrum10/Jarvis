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

export const voiceSupport = {
  get recognition() { return Boolean(SpeechRecognitionImpl); },
  get recording() { return Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder); },
  get synthesis() { return 'speechSynthesis' in window; },
  get any() { return this.recognition || this.recording; },
};

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
    recognition.continuous = continuous;
    recognition.interimResults = true;
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
      const load = () => { this.voice = pickVoice(); };
      load();
      // Chrome populates the voice list asynchronously after first paint.
      window.speechSynthesis.onvoiceschanged = load;
    }
  }

  speak(text, { onStart, onEnd } = {}) {
    if (!this.enabled || !voiceSupport.synthesis) return;
    const clean = speakable(text);
    if (!clean) return;

    this.cancel();
    // Long replies get chunked at sentence boundaries: Chrome silently truncates
    // utterances past a few hundred characters.
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

function pickVoice() {
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return null;
  const lang = navigator.language || 'en-US';
  // Apple's Siri voices and Google's network voices sound markedly better than
  // the default local ones, so prefer them when present.
  const preferred = [
    (v) => v.lang === lang && /Siri/i.test(v.name),
    (v) => v.lang === lang && /Google/i.test(v.name),
    (v) => v.lang === lang && /Samantha|Daniel|Karen|Moira/i.test(v.name),
    (v) => v.lang === lang && v.localService,
    (v) => v.lang.startsWith(lang.slice(0, 2)),
  ];
  for (const test of preferred) {
    const hit = voices.find(test);
    if (hit) return hit;
  }
  return voices[0];
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

/** Split into utterances Chrome won't truncate, breaking on sentences. */
export function chunkForSpeech(text, limit = 200) {
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
