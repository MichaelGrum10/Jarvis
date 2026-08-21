/* Interrupting Jarvis mid-sentence.
 *
 * While he is talking the microphone stays open and the input level is watched.
 * When speech-band energy stays up for long enough, playback stops, the audio
 * download is aborted, and recognition starts — all three together, which is
 * what makes it feel like interrupting a person rather than pressing a stop
 * button and then starting again.
 *
 * ## Sustained, not peak
 *
 * A single loud frame is a cough, a door, a chair. Requiring the energy to stay
 * up for ~300ms is the difference between interrupting him and having him cut
 * out every time something falls over.
 *
 * ## The threshold is relative
 *
 * A fixed number is wrong on every microphone but the one it was tuned on. The
 * floor is tracked continuously — it settles to whatever the room sounds like —
 * and the trigger is a margin above it. A quiet room and a noisy café end up
 * needing the same amount of *extra* sound.
 *
 * ## Echo
 *
 * Without echo cancellation his own voice comes back through the microphone and
 * interrupts him, every time, which reads as the feature being broken. The mic
 * is requested with echoCancellation, noiseSuppression and autoGainControl all
 * on. That is the browser's own canceller, and it is good but not perfect on
 * speakerphone at volume — headphones are the fix if it still loops.
 */

const SUSTAIN_MS = 300;        // how long speech has to persist to count
const GRACE_MS = 400;          // ignore the first moment, while his voice starts
const MARGIN = 22;             // how far above the room's floor counts as speech
const FLOOR_CALIBRATE = 0.25;  // during the opening window, learn the room fast
const FLOOR_RISE = 0.002;      // afterwards the floor climbs slowly...
const FLOOR_FALL = 0.05;       // ...and drops quickly, so it tracks a room going quiet
const RELEASE_MS = 120;        // brief dips inside a sentence don't reset the timer

// Speech energy lives low. Including the whole spectrum buries a voice under
// empty high bins and nothing ever crosses the threshold.
const SPEECH_LOW_HZ = 85;
const SPEECH_HIGH_HZ = 3000;

let stream = null;
let analyser = null;
let bins = null;
let raf = 0;
let floor = 0;
let loudSince = 0;
let quietSince = 0;
let armedAt = 0;
let onInterrupt = null;
let onLevel = null;
let lowBin = 0;
let highBin = 0;

export function bargeInActive() {
  return Boolean(stream);
}

/**
 * Open the mic and watch for an interruption.
 *
 * `context` is the shared AudioContext — iOS is stingy about how many a page
 * may have, and the reactor already owns one.
 */
export async function armBargeIn(context, { interrupt, level } = {}) {
  if (stream || !context) return false;
  if (!navigator.mediaDevices?.getUserMedia) return false;

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch {
    // Denied, or no microphone. Speaking still works; only interrupting is lost.
    stream = null;
    return false;
  }

  onInterrupt = interrupt;
  onLevel = level;

  const source = context.createMediaStreamSource(stream);
  analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  // Some smoothing so a single frame cannot swing the reading, but not so much
  // that the 300ms window is really measuring the smoothing.
  analyser.smoothingTimeConstant = 0.4;
  bins = new Uint8Array(analyser.frequencyBinCount);
  source.connect(analyser);
  // Deliberately not connected to the destination: routing the microphone to
  // the speakers is a feedback loop.

  const perBin = context.sampleRate / 2 / analyser.frequencyBinCount;
  lowBin = Math.max(1, Math.floor(SPEECH_LOW_HZ / perBin));
  highBin = Math.min(analyser.frequencyBinCount - 1, Math.ceil(SPEECH_HIGH_HZ / perBin));

  floor = 0;
  loudSince = 0;
  quietSince = 0;
  armedAt = performance.now();
  raf = requestAnimationFrame(watch);
  return true;
}

export function disarmBargeIn() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  analyser = null;
  bins = null;
  onInterrupt = null;
}

/** Current speech-band energy, 0-255ish. Exported for the mic-armed indicator. */
export function bargeInLevel() {
  return analyser ? read() : 0;
}

function read() {
  analyser.getByteFrequencyData(bins);
  let sum = 0;
  for (let i = lowBin; i <= highBin; i += 1) sum += bins[i];
  return sum / (highBin - lowBin + 1);
}

function watch(now) {
  if (!analyser) return;
  raf = requestAnimationFrame(watch);

  const energy = read();
  onLevel?.(energy);

  // The opening window is a calibration, not just a wait. Starting the floor at
  // zero and creeping up at the running rate meant *any* sound cleared
  // floor + margin the moment the grace expired — a silent room triggered an
  // interrupt in 745ms. It has to learn what the room sounds like first, and
  // his own voice starting is part of what it needs to learn.
  if (now - armedAt < GRACE_MS) {
    floor = floor === 0 ? energy : floor + (energy - floor) * FLOOR_CALIBRATE;
    return;
  }

  // Asymmetric on purpose: the floor should follow a room that goes quiet
  // quickly, and resist being dragged up by the speech we are trying to detect.
  const rate = energy > floor ? FLOOR_RISE : FLOOR_FALL;
  floor += (energy - floor) * rate;

  if (energy > floor + MARGIN) {
    quietSince = 0;
    if (!loudSince) loudSince = now;
    else if (now - loudSince >= SUSTAIN_MS) {
      const fire = onInterrupt;
      disarmBargeIn();
      fire?.();
    }
    return;
  }

  // A gap between words is not the end of an interruption. Only a real pause
  // resets the clock.
  if (!quietSince) quietSince = now;
  else if (now - quietSince > RELEASE_MS) loudSince = 0;
}

/* Exported for tests: the decision, separated from the microphone. */
export const __test = { SUSTAIN_MS, MARGIN, GRACE_MS };
