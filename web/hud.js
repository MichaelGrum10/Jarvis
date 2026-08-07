/* HUD voice mode: wake word, verified capture, and the Jarvis voice.
 *
 * Why this records raw PCM rather than reusing MediaRecorder: speaker
 * verification needs the actual audio on the server, and the server decodes WAV
 * with the Python standard library. Encoding WAV here avoids putting ffmpeg (or
 * any codec dependency) on the server for the sake of one feature.
 *
 * A consequence worth knowing: when voice matching is on, the browser's own
 * speech recognition can't be used for the request itself, because it hands back
 * text and keeps the audio. Native recognition is still used for the wake word,
 * where no verification is needed and the latency matters.
 */

const WAKE_RESTART_MS = 400;
const MAX_UTTERANCE_MS = 15000;
const SILENCE_MS = 1400;

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;

/* ---------------- WAV capture ---------------- */

export class Recorder {
  constructor() {
    this.context = null;
    this.stream = null;
    this.node = null;
    this.chunks = [];
    this.recording = false;
    this.onLevel = null;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.context = new (window.AudioContext || window.webkitAudioContext)();
    const source = this.context.createMediaStreamSource(this.stream);

    // ScriptProcessor is deprecated in favour of AudioWorklet, but it needs no
    // separate module file and is supported everywhere this app runs. The
    // simplicity is worth more here than the deprecation notice.
    this.node = this.context.createScriptProcessor(4096, 1, 1);
    this.chunks = [];
    this.recording = true;

    this.node.onaudioprocess = (event) => {
      if (!this.recording) return;
      const input = event.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(input));
      if (this.onLevel) {
        let peak = 0;
        for (let i = 0; i < input.length; i += 16) peak = Math.max(peak, Math.abs(input[i]));
        this.onLevel(peak);
      }
    };

    source.connect(this.node);
    this.node.connect(this.context.destination);
    return this.context.sampleRate;
  }

  stop() {
    this.recording = false;
    try { this.node?.disconnect(); } catch { /* already gone */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    const rate = this.context?.sampleRate || 44100;
    this.context?.close().catch(() => {});
    const samples = flatten(this.chunks);
    this.chunks = [];
    return samples.length ? encodeWav(samples, rate) : null;
  }
}

function flatten(chunks) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.length; }
  return out;
}

/** Float32 PCM -> 16-bit mono WAV. */
function encodeWav(samples, rate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const str = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };

  str(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  str(8, 'WAVE');
  str(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);        // PCM
  view.setUint16(22, 1, true);        // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // byte rate
  view.setUint16(32, 2, true);        // block align
  view.setUint16(34, 16, true);       // bits per sample
  str(36, 'data');
  view.setUint32(40, samples.length * 2, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

/* ---------------- wake word ---------------- */

export class WakeListener {
  /* Uses the browser's own recogniser purely to spot the wake word: it's free,
   * runs locally, and never sends audio anywhere. Only once it fires do we start
   * recording for real. */
  constructor(wakeWord, onWake, onError) {
    this.wakeWord = (wakeWord || 'jarvis').toLowerCase();
    this.onWake = onWake;
    this.onError = onError;
    this.recognition = null;
    this.wanted = false;
    this.available = Boolean(SpeechRecognitionImpl);
  }

  start() {
    if (!this.available || this.wanted) return;
    this.wanted = true;
    this._spin();
  }

  stop() {
    this.wanted = false;
    try { this.recognition?.stop(); } catch { /* not running */ }
    this.recognition = null;
  }

  _spin() {
    if (!this.wanted) return;
    const recognition = new SpeechRecognitionImpl();
    this.recognition = recognition;
    recognition.lang = navigator.language || 'en-US';
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const text = event.results[i][0].transcript.toLowerCase();
        if (this._heard(text)) {
          this.stop();
          this.onWake();
          return;
        }
      }
    };

    recognition.onerror = (event) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        this.wanted = false;
        this.onError?.('Microphone blocked — wake word needs mic access.');
      }
    };

    // Browsers end continuous recognition on their own schedule, so keeping a
    // wake word alive means restarting it indefinitely.
    recognition.onend = () => {
      if (this.wanted) setTimeout(() => this._spin(), WAKE_RESTART_MS);
    };

    try {
      recognition.start();
    } catch {
      setTimeout(() => this._spin(), WAKE_RESTART_MS);
    }
  }

  _heard(text) {
    if (text.includes(this.wakeWord)) return true;
    // Short names get mangled constantly — "Jarvis" comes back as "Travis",
    // "Charice", "service". Accept close variants or the wake word is unusable.
    return text.split(/\s+/).some((word) => {
      const clean = word.replace(/[^a-z]/g, '');
      if (Math.abs(clean.length - this.wakeWord.length) > 2) return false;
      const shared = new Set([...clean].filter((c) => this.wakeWord.includes(c)));
      return shared.size >= Math.max(3, this.wakeWord.length - 2);
    });
  }
}

/* ---------------- utterance capture ---------------- */

/** Record until the speaker stops, then return WAV. */
export async function captureUtterance({ onLevel, onStart } = {}) {
  const recorder = new Recorder();
  let lastLoud = Date.now();
  let started = false;

  recorder.onLevel = (peak) => {
    if (peak > 0.045) {
      lastLoud = Date.now();
      if (!started) { started = true; onStart?.(); }
    }
    onLevel?.(peak);
  };

  await recorder.start();
  const began = Date.now();

  await new Promise((resolve) => {
    const check = setInterval(() => {
      const now = Date.now();
      const quietLongEnough = started && now - lastLoud > SILENCE_MS;
      // The no-speech timeout is shorter: if nothing was ever said, there's no
      // point holding the mic open for the full utterance budget.
      const neverSpoke = !started && now - began > 4000;
      if (quietLongEnough || neverSpoke || now - began > MAX_UTTERANCE_MS) {
        clearInterval(check);
        resolve();
      }
    }, 120);
  });

  return { wav: recorder.stop(), spoke: started };
}

/* ---------------- the Jarvis voice ---------------- */

/**
 * Pick the closest available voice to JARVIS: British, male, measured.
 *
 * Being straight about this — it is not the film's voice, and nothing free can
 * be. What the browser offers is a set of system voices, and on Apple hardware
 * "Daniel" (en-GB male) is a genuinely close match in accent and timbre. On
 * other platforms we fall back through the best British male options available.
 */
export function pickJarvisVoice(voices) {
  if (!voices?.length) return null;
  const preferences = [
    (v) => /daniel/i.test(v.name) && /en.GB/i.test(v.lang),
    (v) => /arthur|oliver|george|malcolm/i.test(v.name) && /en.GB/i.test(v.lang),
    (v) => /en.GB/i.test(v.lang) && /male/i.test(v.name),
    (v) => /google uk english male/i.test(v.name),
    (v) => /en.GB/i.test(v.lang),
    (v) => /en.AU/i.test(v.lang),
    (v) => /^en/i.test(v.lang),
  ];
  for (const test of preferences) {
    const hit = voices.find(test);
    if (hit) return hit;
  }
  return voices[0];
}

export class JarvisVoice {
  constructor() {
    this.voice = null;
    this.enabled = true;
    this.speaking = false;
    if ('speechSynthesis' in window) {
      const load = () => {
        const voices = window.speechSynthesis.getVoices();
        // An explicit choice always wins over the heuristic — the heuristic is a
        // starting point, not a correction to be reapplied.
        const chosen = localStorage.getItem('jarvis_voice');
        this.voice = (chosen && voices.find((v) => v.name === chosen)) || pickJarvisVoice(voices);
      };
      load();
      window.speechSynthesis.onvoiceschanged = load;
    }
  }

  get voiceName() {
    return this.voice?.name || 'system default';
  }

  speak(text, { onStart, onEnd } = {}) {
    if (!this.enabled || !('speechSynthesis' in window)) { onEnd?.(); return; }
    const clean = speakable(text);
    if (!clean) { onEnd?.(); return; }

    window.speechSynthesis.cancel();
    const chunks = chunkForSpeech(clean);
    this.speaking = true;
    onStart?.();

    chunks.forEach((chunk, index) => {
      const utterance = new SpeechSynthesisUtterance(chunk);
      if (this.voice) utterance.voice = this.voice;
      // Slightly slow and slightly low: JARVIS is unhurried, never breathless.
      utterance.rate = 0.96;
      utterance.pitch = 0.85;
      if (index === chunks.length - 1) {
        utterance.onend = () => { this.speaking = false; onEnd?.(); };
        utterance.onerror = () => { this.speaking = false; onEnd?.(); };
      }
      window.speechSynthesis.speak(utterance);
    });
  }

  cancel() {
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    this.speaking = false;
  }
}

export function speakable(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, 'a link')
    .replace(/[*_#>]+/g, '')
    .replace(/^\s*[-•]\s*/gm, '')
    .replace(/⚠️|✅|●|🎙|◎/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

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
