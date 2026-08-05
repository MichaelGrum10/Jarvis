"""Speaker verification: the maths, and the enrol/verify/reject flow.

Synthetic speakers stand in for real ones — a glottal buzz shaped by different
formant sets. Crude, but it exercises exactly what the embedding is supposed to
key on (vocal tract shape) rather than what it must ignore (level, noise, mic).
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from jarvis.voiceprint import (
    Voiceprint,
    VoiceprintError,
    decode_wav,
    embed,
    embed_wav,
    similarity,
)

RATE = 16_000

SPEAKER_A = [(730, 50, 1.0), (1090, 70, 0.7), (2440, 110, 0.4)]
SPEAKER_B = [(270, 50, 1.0), (2290, 70, 0.7), (3010, 110, 0.4)]
SPEAKER_C = [(530, 50, 1.0), (1840, 70, 0.7), (2480, 110, 0.4)]


def synth(f0, formants, seconds=2.0, seed=0, noise=0.01, gain=1.0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * seconds)) / RATE
    buzz = np.zeros_like(t)
    for harmonic in range(1, 25):
        buzz += (1.0 / harmonic) * np.sin(
            2 * np.pi * f0 * harmonic * t + rng.uniform(0, 2 * np.pi)
        )
    shaped = np.zeros_like(t)
    for centre, bandwidth, amp in formants:
        envelope = amp * np.exp(-bandwidth * np.abs(np.sin(2 * np.pi * centre * t / 2)))
        shaped += envelope * np.sin(2 * np.pi * centre * t + rng.uniform(0, 2 * np.pi))
    signal = 0.6 * buzz / np.max(np.abs(buzz)) + 0.8 * shaped / max(np.max(np.abs(shaped)), 1e-9)
    signal *= 0.6 + 0.4 * np.sin(2 * np.pi * 3.5 * t)          # syllable envelope
    signal += noise * rng.standard_normal(t.size)
    return (gain * signal / np.max(np.abs(signal))).astype(np.float32)


def to_wav(samples, rate=RATE):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


# ---------------------------------------------------------------- maths


def test_same_speaker_scores_far_above_impostor():
    """The property the whole feature rests on. If this margin collapses the
    threshold is meaningless, so assert the gap, not just the ordering."""
    a1 = embed(synth(120, SPEAKER_A, seed=1), RATE)
    a2 = embed(synth(122, SPEAKER_A, seed=2), RATE)
    b1 = embed(synth(210, SPEAKER_B, seed=3), RATE)

    genuine, impostor = similarity(a1, a2), similarity(a1, b1)
    assert genuine > impostor + 0.2, f"margin too small: {genuine:.3f} vs {impostor:.3f}"


@pytest.mark.parametrize(
    "label,kwargs",
    [
        ("quiet", {"gain": 0.3}),
        ("noisy", {"noise": 0.05}),
        ("short", {"seconds": 0.9}),
    ],
)
def test_genuine_speaker_survives_real_world_variation(label, kwargs):
    """Enrolment happens once, in one setting; verification happens everywhere.
    Level, noise and utterance length must not push a genuine speaker below the
    default threshold."""
    profile = Voiceprint(
        [embed(synth(118 + i, SPEAKER_A, seed=i), RATE) for i in range(3)]
    )
    score = profile.score(embed(synth(121, SPEAKER_A, seed=99, **kwargs), RATE))
    assert score >= 0.70, f"{label}: genuine speaker scored {score:.3f}"


def test_impostors_score_well_below_genuine_speakers():
    """The honest claim, measured rather than assumed.

    Genuine and impostor score distributions overlap — on synthetic speakers,
    genuine bottoms out around 0.71 while the closest impostor reaches 0.84. No
    threshold separates them perfectly, which is exactly why this feature is
    documented as a filter rather than a lock. What must hold is a large gap
    between the *populations*, so the default threshold is a sensible trade
    rather than a coin toss.
    """
    profile = Voiceprint([embed(synth(118 + i, SPEAKER_A, seed=i), RATE) for i in range(3)])

    genuine = [
        profile.score(embed(synth(118 + (s % 6), SPEAKER_A, seed=s), RATE))
        for s in range(20, 32)
    ]
    impostor = [
        profile.score(embed(synth(f0, formants, seed=s), RATE))
        for formants, f0 in ((SPEAKER_B, 210), (SPEAKER_C, 165))
        for s in range(50, 62)
    ]

    assert np.mean(genuine) > np.mean(impostor) + 0.2
    # At the shipped default, most impostors are turned away.
    assert np.mean([s < 0.78 for s in impostor]) > 0.8


def test_c0_is_excluded_from_the_embedding():
    """c0 is frame energy at ~10x the magnitude of everything else; including it
    swamps the cosine and collapses speaker separation. Guard against it
    silently coming back."""
    loud = embed(synth(120, SPEAKER_A, seed=1, gain=1.0), RATE)
    quiet = embed(synth(120, SPEAKER_A, seed=1, gain=0.2), RATE)
    assert similarity(loud, quiet) > 0.95, "loudness is leaking into the voiceprint"


def test_embedding_is_unit_length():
    vector = embed(synth(120, SPEAKER_A, seed=1), RATE)
    assert np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5)


# ---------------------------------------------------------------- io


def test_wav_round_trip():
    samples = synth(120, SPEAKER_A, seed=1)
    decoded, rate = decode_wav(to_wav(samples))
    assert rate == RATE
    assert np.allclose(decoded, samples, atol=2e-4)


def test_embed_wav_matches_embed():
    samples = synth(120, SPEAKER_A, seed=1)
    assert similarity(embed_wav(to_wav(samples)), embed(samples, RATE)) > 0.999


def test_resampled_audio_still_matches():
    """Devices record at whatever rate they like; a phone at 48kHz must match an
    enrolment made at 16kHz."""
    samples = synth(120, SPEAKER_A, seed=1)
    at_48k = np.interp(
        np.linspace(0, samples.size - 1, int(samples.size * 3)),
        np.arange(samples.size),
        samples,
    ).astype(np.float32)
    assert similarity(embed_wav(to_wav(samples)), embed_wav(to_wav(at_48k, 48_000))) > 0.9


def test_too_short_is_refused_not_guessed():
    with pytest.raises(VoiceprintError, match="at least"):
        embed(synth(120, SPEAKER_A, seconds=0.2), RATE)


def test_silence_is_refused():
    """Silence yields near-constant cepstra, which standardise into a stable
    embedding that would happily match other silence. It has to be refused."""
    with pytest.raises(VoiceprintError, match="silent"):
        embed(np.zeros(RATE * 2, dtype=np.float32), RATE)


def test_garbage_bytes_rejected_cleanly():
    with pytest.raises(VoiceprintError):
        embed_wav(b"this is not a wav file")


# ---------------------------------------------------------------- profile


def test_profile_serialisation_round_trip():
    profile = Voiceprint([embed(synth(118 + i, SPEAKER_A, seed=i), RATE) for i in range(3)])
    restored = Voiceprint.from_bytes(profile.to_bytes())
    assert len(restored.vectors) == 3
    assert np.allclose(profile.centroid, restored.centroid)


def test_cohesion_flags_inconsistent_enrolment():
    """Enrolling two different people should look obviously wrong, so the UI can
    say so rather than building a profile that matches neither well."""
    consistent = Voiceprint([embed(synth(118 + i, SPEAKER_A, seed=i), RATE) for i in range(3)])
    mixed = Voiceprint(
        [
            embed(synth(120, SPEAKER_A, seed=1), RATE),
            embed(synth(210, SPEAKER_B, seed=2), RATE),
            embed(synth(165, SPEAKER_C, seed=3), RATE),
        ]
    )
    assert consistent.cohesion > mixed.cohesion + 0.15


def test_empty_profile_refuses_to_score():
    with pytest.raises(VoiceprintError):
        _ = Voiceprint([]).centroid
