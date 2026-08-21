/* The arc reactor: a circular HUD that pulses with Jarvis's voice.
 *
 * Drawn on a 2D canvas over the galaxy, deliberately not in DOM elements — a
 * dozen divs with animated box-shadows would fight the WebGL graph for the
 * compositor, and the whole point of capping this at 30fps is to leave the GPU
 * to the stars.
 *
 * ## Where the pulse comes from
 *
 * Two sources, chosen at runtime, because the browser will not give us the same
 * signal in both cases:
 *
 * 1. **Audio we own** (a fetched buffer, e.g. a TTS service returning mp3):
 *    routed through an AnalyserNode and read every frame. This is real
 *    amplitude — the core moves because the sound moves.
 *
 * 2. **Web Speech synthesis**: the browser owns that audio end to end and
 *    exposes no node to tap. There is no amplitude to read, at all. So the
 *    envelope is reconstructed from the utterance itself: `boundary` events
 *    snap the position to where the engine actually is, and between them the
 *    envelope advances through the real text, rising on vowels and falling at
 *    punctuation. It tracks the words being spoken rather than a clock, which
 *    is the difference between "close enough" and the three-second tell.
 *
 *    On WebKit `boundary` frequently never fires. The text-derived envelope is
 *    what carries it there, and it is honestly a reconstruction — but it is a
 *    reconstruction of *this sentence*, not a sine wave.
 *
 * ## States
 *
 *   idle       slow breathing, cyan
 *   listening  ring contracts, colour shifts amber
 *   thinking   rings counter-rotate, faster
 *   speaking   core tracks amplitude
 */

const CANVAS_ID = 'reactor';
const HIDDEN_KEY = 'jarvis-reactor-hidden';

const FRAME_MS = 1000 / 30;          // 30fps ceiling; see module docstring
// Without this, a display that is not 60Hz halves. rAF arriving every 33.2ms on
// a 30Hz panel would miss a 33.33ms gate every time and render at 15.
const FRAME_SLOP = 4;
const SLOW_FPS = 20;                 // below this we drop to the simple ring
// Measured in milliseconds of sustained slowness, not in frames. Counting
// frames means the slower the device, the longer it takes to notice it is
// slow — which is exactly backwards.
const SLOW_MS = 1500;
// Longer than SLOW_MS on purpose: quick to protect the frame rate, slow to
// trust that the pressure is gone.
const RECOVER_MS = 5000;
const DPR_CAP = 2;                   // retina is plenty; 3x is just overdraw

// How fast the core follows the signal. Attack is quick so a consonant lands;
// release is slow so the core doesn't strobe between syllables.
const ATTACK = 0.45;
const RELEASE = 0.12;

const PALETTE = {
  idle:      { core: [190, 235, 255], ring: [90, 190, 230] },
  listening: { core: [255, 214, 150], ring: [230, 170, 70] },
  thinking:  { core: [190, 235, 255], ring: [120, 210, 245] },
  speaking:  { core: [225, 250, 255], ring: [110, 220, 255] },
  denied:    { core: [255, 180, 180], ring: [220, 90, 90] },
};

let canvas = null;
let ctx = null;
let running = false;
let frame = 0;
let state = 'idle';
let hidden = false;
let simple = false;              // the degraded two-ring version
let slowStreak = 0;              // timestamp the slow run began, 0 when keeping up
let fastStreak = 0;              // the same, for deciding it is safe to un-degrade
let lastDraw = 0;
let spin = 0;
let level = 0;                   // smoothed 0..1, what the core actually draws
let target = 0;                  // where the current source says it should be

let audio = null;                // AudioContext, created on a gesture
let analyser = null;
let bins = null;
let envelope = null;             // the Web Speech reconstruction, when active

/* ---------------- the two amplitude sources ---------------- */

/** Reconstructs a speech envelope from the utterance text and its boundaries.
 *
 * Position advances at the engine's apparent rate and is corrected whenever a
 * `boundary` event arrives. Loudness comes from the characters around that
 * position: vowels carry the energy in speech, punctuation is where it stops.
 */
class TextEnvelope {
  constructor(text, rate = 1) {
    this.text = String(text || '');
    // Roughly 14 characters a second at rate 1 — close enough that boundary
    // events only ever nudge it, and the sole input available when they don't.
    this.perMs = (14 * rate) / 1000;
    this.start = performance.now();
    this.charAt = 0;
    this.corrected = false;
  }

  /** A real boundary from the engine: snap to the truth. */
  boundary(charIndex) {
    if (typeof charIndex === 'number' && charIndex >= 0) {
      this.charAt = charIndex;
      this.start = performance.now();
      this.corrected = true;
    }
  }

  read() {
    const elapsed = performance.now() - this.start;
    const at = Math.floor(this.charAt + elapsed * this.perMs);
    if (at >= this.text.length) return 0;

    const window_ = this.text.slice(Math.max(0, at - 2), at + 3);
    if (!window_) return 0;
    // A pause at punctuation is the most audible feature of speech rhythm, and
    // the easiest to get wrong by ignoring.
    if (/[.,;:!?—]/.test(window_)) return 0.15;
    if (/\s/.test(this.text[at] || '')) return 0.3;

    const vowels = (window_.match(/[aeiouy]/gi) || []).length;
    return Math.min(1, 0.42 + vowels * 0.16);
  }
}

/** Real amplitude, for audio we control. */
function readAnalyser() {
  if (!analyser || !bins) return 0;
  analyser.getByteFrequencyData(bins);
  // Speech lives low. Averaging the whole spectrum buries it under empty highs
  // and the core barely moves.
  const top = Math.floor(bins.length * 0.35);
  let sum = 0;
  for (let i = 0; i < top; i += 1) sum += bins[i];
  return Math.min(1, sum / top / 140);
}

/* ---------------- public API ---------------- */

/** Create the AudioContext. Must be called from inside a user gesture: iOS
 *  refuses to start one otherwise, and refuses silently. */
export function reactorUnlock() {
  if (audio) {
    if (audio.state === 'suspended') audio.resume().catch(() => {});
    return audio;
  }
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try {
    audio = new Ctor();
  } catch {
    audio = null;
  }
  return audio;
}

/** Route audio we own through an analyser — the real-amplitude path.
 *  Pass an <audio>/<video> element or an AudioBufferSourceNode. */
export function reactorAnalyse(source) {
  const context = reactorUnlock();
  if (!context || !source) return false;
  try {
    analyser = context.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.7;
    bins = new Uint8Array(analyser.frequencyBinCount);

    const node = source instanceof HTMLMediaElement
      ? context.createMediaElementSource(source)
      : source;
    node.connect(analyser);
    // Still has to reach the speakers: an analyser is a tap, not a sink.
    analyser.connect(context.destination);
    envelope = null;
    return true;
  } catch {
    // A media element can only be tapped once per context; a second attempt
    // throws. Falling back to the envelope is better than losing the audio.
    analyser = null;
    bins = null;
    return false;
  }
}

/** Start the Web Speech reconstruction for one utterance. */
export function reactorSpeaking(text, rate = 1) {
  envelope = new TextEnvelope(text, rate);
  analyser = null;
  setState('speaking');
}

/** A `boundary` event arrived — correct the reconstruction. */
export function reactorBoundary(charIndex) {
  envelope?.boundary(charIndex);
}

export function reactorSilent() {
  envelope = null;
  analyser = null;
  target = 0;
}

export function setState(next) {
  state = next;
  if (next !== 'speaking') reactorSilent();
}

export function reactorHidden() {
  return hidden;
}

export function toggleReactor(force) {
  hidden = force === undefined ? !hidden : Boolean(force);
  try {
    localStorage.setItem(HIDDEN_KEY, hidden ? '1' : '0');
  } catch { /* private mode; the toggle just won't be remembered */ }
  if (canvas) canvas.classList.toggle('hidden', hidden);
  if (hidden) stop(); else start();
  return hidden;
}

export function initReactor(host) {
  if (canvas) return canvas;
  canvas = document.createElement('canvas');
  canvas.id = CANVAS_ID;
  canvas.className = 'reactor';
  // Decoration, not furniture: it must never intercept a tap meant for a star.
  canvas.setAttribute('aria-hidden', 'true');
  host.appendChild(canvas);
  ctx = canvas.getContext('2d');

  try {
    hidden = localStorage.getItem(HIDDEN_KEY) === '1';
  } catch { hidden = false; }
  canvas.classList.toggle('hidden', hidden);

  resize();
  window.addEventListener('resize', resize);
  // A loop nobody can see is pure heat. Stopping on hide also means iOS does
  // not have to throttle us — we are already gone.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop(); else start();
  });
  if (!hidden) start();
  return canvas;
}

/* ---------------- the loop ---------------- */

function start() {
  if (running || hidden || document.hidden || !canvas) return;
  running = true;
  lastDraw = 0;
  slowStreak = 0;
  fastStreak = 0;
  frame = requestAnimationFrame(tick);
}

function stop() {
  running = false;
  if (frame) cancelAnimationFrame(frame);
  frame = 0;
}

function tick(now) {
  if (!running) return;
  frame = requestAnimationFrame(tick);

  const since = now - lastDraw;
  if (since < FRAME_MS - FRAME_SLOP) return;      // the 30fps ceiling
  lastDraw = now;

  // Measured from real frame times, not from a device sniff. iOS throttles rAF
  // hard while audio is playing — which is exactly when this element matters —
  // and no amount of user-agent checking would predict when.
  if (since > 1000 / SLOW_FPS) {
    fastStreak = 0;
    if (!slowStreak) slowStreak = now;
    else if (now - slowStreak > SLOW_MS) simple = true;
  } else {
    slowStreak = 0;
    // And back again, but only after a long clean run. iOS throttles during
    // audio playback and stops when it ends, so without recovery one spoken
    // sentence would leave the reactor simplified for the rest of the session
    // — and the asymmetry (1.5s down, 5s up) is what stops it flapping between
    // the two on a device sitting near the threshold.
    if (simple) {
      if (!fastStreak) fastStreak = now;
      else if (now - fastStreak > RECOVER_MS) { simple = false; fastStreak = 0; }
    }
  }

  target = analyser ? readAnalyser() : (envelope ? envelope.read() : 0);
  const rate = target > level ? ATTACK : RELEASE;
  level += (target - level) * rate;

  draw(now);
}

/* ---------------- drawing ---------------- */

function resize() {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
  const size = Math.min(window.innerWidth, window.innerHeight) * 0.42;
  canvas.style.width = `${size}px`;
  canvas.style.height = `${size}px`;
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);
  ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  canvas._size = size;
}

function rgba(triplet, alpha) {
  return `rgba(${triplet[0]}, ${triplet[1]}, ${triplet[2]}, ${alpha})`;
}

function draw(now) {
  const size = canvas._size || 240;
  const half = size / 2;
  const colours = PALETTE[state] || PALETTE.idle;
  ctx.clearRect(0, 0, size, size);

  // Breathing when idle, amplitude when speaking. Listening pulls in; thinking
  // sits steady and lets the counter-rotation carry the motion.
  const breath = (Math.sin(now / 1400) + 1) / 2;
  let energy = 0;
  if (state === 'speaking') energy = level;
  else if (state === 'idle') energy = breath * 0.28;
  else if (state === 'listening') energy = 0.12 + breath * 0.1;
  else if (state === 'thinking') energy = 0.3;

  const contract = state === 'listening' ? 0.86 : 1;
  const outer = half * 0.94 * contract;
  const core = half * (0.2 + energy * 0.26) * contract;

  spin += state === 'thinking' ? 0.011 : 0.0035;

  ctx.save();
  ctx.translate(half, half);

  if (simple) {
    // The degraded version: two rings, flat strokes, no gradients or shadows.
    // Everything dropped here is a per-frame allocation or a blur.
    ring(outer, colours.ring, 0.5, 2);
    ring(core * 1.6, colours.core, 0.75, 3);
    ctx.restore();
    return;
  }

  // Outer ring: constant slow rotation, with a gap so the motion reads.
  ctx.save();
  ctx.rotate(spin);
  ctx.strokeStyle = rgba(colours.ring, 0.55);
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(0, 0, outer, 0.22, Math.PI * 2 - 0.22);
  ctx.stroke();
  // Ticks, so rotation is visible on a ring that is otherwise a circle.
  ctx.strokeStyle = rgba(colours.ring, 0.8);
  for (let i = 0; i < 12; i += 1) {
    const angle = (i / 12) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(Math.cos(angle) * (outer - 8), Math.sin(angle) * (outer - 8));
    ctx.lineTo(Math.cos(angle) * (outer - 2), Math.sin(angle) * (outer - 2));
    ctx.stroke();
  }
  ctx.restore();

  // Middle ring, counter-rotating. In thinking state the two speeds are what
  // says "working" without any text.
  ctx.save();
  ctx.rotate(-spin * (state === 'thinking' ? 1.7 : 0.6));
  ctx.strokeStyle = rgba(colours.ring, 0.4);
  ctx.lineWidth = 6;
  ctx.beginPath();
  ctx.arc(0, 0, outer * 0.74, 0.6, Math.PI * 1.1);
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(0, 0, outer * 0.74, Math.PI * 1.3, Math.PI * 1.85);
  ctx.stroke();
  ctx.restore();

  // Inner ring, steady, the frame the core sits in.
  ring(outer * 0.5, colours.ring, 0.35, 1);

  // The core. A radial gradient carries the glow rather than shadowBlur, which
  // is a full-canvas blur per frame and the single most expensive thing here.
  // Tight, and thin at the edges. A wide gradient at moderate alpha covered the
  // whole disc and washed the starfield out of the middle of the element — the
  // one thing it is supposed to let through.
  const reach = core * 1.9;
  const glow = ctx.createRadialGradient(0, 0, core * 0.6, 0, 0, reach);
  glow.addColorStop(0, rgba(colours.core, 0.55 + energy * 0.25));
  glow.addColorStop(0.45, rgba(colours.core, 0.16 + energy * 0.14));
  glow.addColorStop(1, rgba(colours.core, 0));
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(0, 0, reach, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = rgba([255, 255, 255], 0.72 + energy * 0.25);
  ctx.beginPath();
  ctx.arc(0, 0, core, 0, Math.PI * 2);
  ctx.fill();

  ctx.restore();
}

function ring(radius, colour, alpha, width) {
  ctx.strokeStyle = rgba(colour, alpha);
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.arc(0, 0, radius, 0, Math.PI * 2);
  ctx.stroke();
}

/* Exposed for tests: the envelope is the half that has no browser to lean on. */
export const __test = { TextEnvelope };
