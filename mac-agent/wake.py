"""Wake-word listening, on the Mac, locally.

## Why this lives here and not in the browser

A wake word needs a microphone that is always open. WebKit requires a fresh user
gesture for every single `start()` on its speech recogniser, which makes an
always-open recogniser impossible — and every browser on iOS is WebKit
underneath, Chrome included. So there is no browser on an iPhone, and no Safari
anywhere, that can do this. Rather than ship an indicator that does nothing, the
wake word lives on the Mac and the browser says so.

## What leaves the machine, and when

Nothing, until "Jarvis" is heard.

openWakeWord runs entirely locally against a small ONNX model. The microphone
feeds a ring buffer that is overwritten continuously and never written to disk.
Only once the model scores above the threshold does anything get sent anywhere:
the few seconds of speech that follow the wake word, over the agent's existing
authenticated socket. Silence, background conversation, and everything before
the trigger are discarded in place.

## Muting

`muted` in ~/.jarvis-agent.json stops the microphone being opened at all — not a
flag checked later, but the stream never starting. The file is re-read while
running, so muting takes effect within a second and survives a restart.
"""

from __future__ import annotations

import base64
import io
import logging
import threading
import time
import wave

log = logging.getLogger("jarvis-agent.wake")

SAMPLE_RATE = 16000          # what openWakeWord expects
FRAME = 1280                 # 80ms, its native chunk

# Above this, the model is confident enough to act on. openWakeWord's own
# guidance is 0.5; a little higher trades a few missed wakes for far fewer
# false ones, which is the right way round for something that opens a mic.
THRESHOLD = 0.6
# After firing, ignore the model for a moment so one "Jarvis" cannot trigger
# twice while the word is still in the buffer.
COOLDOWN_S = 2.0

# How long to record after the wake word before giving up on a sentence.
MAX_UTTERANCE_S = 8.0
SILENCE_S = 1.1              # this much quiet ends the utterance
SILENCE_RMS = 380            # int16 RMS below this is "not speech"


class WakeUnavailable(RuntimeError):
    """The wake word cannot run here — missing dependency or no microphone."""


def probe() -> tuple[bool, str]:
    """Can the wake word run? Returns (yes, why not)."""
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        return False, "openwakeword is not installed (pip3 install --user openwakeword)"
    try:
        import sounddevice  # noqa: F401
    except ImportError:
        return False, "sounddevice is not installed (pip3 install --user sounddevice)"
    return True, ""


class WakeListener:
    """Listens for the wake word and hands the following utterance to a callback.

    Runs on its own thread: the audio callback must never wait on a socket, and
    the socket must never wait on audio.
    """

    def __init__(self, on_utterance, model: str = "hey_jarvis", is_muted=None) -> None:
        self.on_utterance = on_utterance
        self.model_name = model
        self.is_muted = is_muted or (lambda: False)
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.listening = False          # is the microphone actually open
        self.last_wake = 0.0
        self.wakes = 0
        self.error = ""

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, name="wake", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.listening = False

    def describe(self) -> dict:
        return {
            "listening": self.listening,
            "muted": bool(self.is_muted()),
            "model": self.model_name,
            "wakes": self.wakes,
            "error": self.error,
        }

    # ---- the loop ---------------------------------------------------------

    def _run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
            from openwakeword.model import Model
        except ImportError as exc:
            self.error = str(exc)
            log.warning("Wake word unavailable: %s", exc)
            return

        try:
            model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
        except Exception as exc:                      # noqa: BLE001
            self.error = f"could not load the wake model: {exc}"
            log.warning("Wake word unavailable: %s", self.error)
            return

        while not self.stop_flag.is_set():
            if self.is_muted():
                # Not a flag checked downstream — the stream is never opened, so
                # the microphone light stays off and there is nothing to leak.
                if self.listening:
                    log.info("Wake word muted; microphone closed")
                self.listening = False
                time.sleep(1.0)
                continue

            try:
                self._listen(model, sd, np)
            except Exception as exc:                  # noqa: BLE001
                self.error = str(exc)
                self.listening = False
                log.warning("Wake listener stopped (%s); retrying in 5s", exc)
                time.sleep(5.0)

    def _listen(self, model, sd, np) -> None:
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME
        ) as mic:
            self.listening = True
            self.error = ""
            log.info("Listening for the wake word")

            while not self.stop_flag.is_set() and not self.is_muted():
                block, _ = mic.read(FRAME)
                audio = np.frombuffer(block, dtype=np.int16)

                scores = model.predict(audio)
                best = max(scores.values()) if scores else 0.0
                if best < THRESHOLD:
                    continue
                if time.time() - self.last_wake < COOLDOWN_S:
                    continue

                self.last_wake = time.time()
                self.wakes += 1
                log.info("Wake word heard (%.2f)", best)
                # Reset, or the word still sitting in its buffer scores again on
                # the next frame and fires a second time.
                model.reset()

                utterance = self._capture(mic, np)
                if utterance is not None:
                    self.on_utterance(utterance)

            self.listening = False

    def _capture(self, mic, np) -> bytes | None:
        """Record until the speaker stops. Returns WAV bytes, or None if silent."""
        frames: list[bytes] = []
        quiet_for = 0.0
        spoke = False
        started = time.time()

        while time.time() - started < MAX_UTTERANCE_S:
            block, _ = mic.read(FRAME)
            frames.append(bytes(block))
            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0

            if rms >= SILENCE_RMS:
                spoke = True
                quiet_for = 0.0
            elif spoke:
                quiet_for += FRAME / SAMPLE_RATE
                if quiet_for >= SILENCE_S:
                    break

        if not spoke:
            log.info("Wake word with nothing after it; discarded")
            return None
        return _wav(b"".join(frames))


def _wav(pcm: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    return buffer.getvalue()


def encode(wav_bytes: bytes) -> str:
    return base64.b64encode(wav_bytes).decode("ascii")
