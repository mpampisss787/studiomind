"""Tests for time-aware masking detection.

Time-aware masking only flags a band when two stems are loud in the
*same* frames — it must NOT flag stems that play in disjoint sections
(e.g., a verse-only piano vs a chorus-only synth).
"""

from __future__ import annotations

import numpy as np

from studiomind.analyzer.masking import StemFrames, detect_time_aware_masking
from studiomind.analyzer.spectral import _compute_stft


SR = 44100


def _stft_of(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    return _compute_stft(audio.astype(np.float32), SR)


def _sine(freq: float, duration_s: float, amp: float = 0.3) -> np.ndarray:
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silent(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


def _stem_from_audio(name: str, audio: np.ndarray) -> StemFrames:
    mag, freqs, params = _stft_of(audio)
    return StemFrames(
        name=name,
        stft_mag=mag,
        freqs=freqs,
        sample_rate=SR,
        hop=params["hop"],
    )


# ── Empty / degenerate inputs ────────────────────────────────────────


def test_empty_input_returns_empty() -> None:
    assert detect_time_aware_masking([]) == []


def test_single_stem_returns_empty() -> None:
    s = _stem_from_audio("solo.wav", _sine(1000.0, 2.0))
    assert detect_time_aware_masking([s]) == []


# ── Disjoint-time stems should NOT be flagged ────────────────────────


def test_non_overlapping_in_time_not_flagged() -> None:
    """Stem A plays seconds 0-2, stem B plays seconds 2-4. Same band but
    they never overlap in time — must not flag a conflict."""
    a_audio = np.concatenate([_sine(1000.0, 2.0), _silent(2.0)])
    b_audio = np.concatenate([_silent(2.0), _sine(1000.0, 2.0)])
    a = _stem_from_audio("verse_lead.wav", a_audio)
    b = _stem_from_audio("chorus_lead.wav", b_audio)
    conflicts = detect_time_aware_masking([a, b])
    # No conflict should be reported in any band.
    assert conflicts == []


# ── Same-time stems should be flagged ───────────────────────────────


def test_simultaneous_loud_in_same_band_flagged() -> None:
    """Two stems both loud at 1 kHz throughout — should flag the mid band."""
    sig = _sine(1000.0, 4.0)
    a = _stem_from_audio("pad_a.wav", sig)
    b = _stem_from_audio("pad_b.wav", sig.copy())
    conflicts = detect_time_aware_masking([a, b])
    assert len(conflicts) >= 1
    # At least one conflict should cover the mid band (500-2000)
    bands = [c["band"] for c in conflicts]
    assert "mid" in bands
    mid_conflict = next(c for c in conflicts if c["band"] == "mid")
    assert mid_conflict["overlap_seconds"] >= 3.0
    assert mid_conflict["severity"] == "high"


# ── Partial overlap → medium / low severity ─────────────────────────


def test_partial_overlap_lower_severity() -> None:
    """Both stems play 2s but only overlap for 0.6s in the same band."""
    a_audio = np.concatenate([_sine(1000.0, 2.0), _silent(1.4)])  # 0.0 – 2.0
    b_audio = np.concatenate([_silent(1.4), _sine(1000.0, 2.0)])  # 1.4 – 3.4
    a = _stem_from_audio("a.wav", a_audio)
    b = _stem_from_audio("b.wav", b_audio)
    conflicts = detect_time_aware_masking([a, b])
    assert any(c["band"] == "mid" for c in conflicts)
    mid = next(c for c in conflicts if c["band"] == "mid")
    # 0.6 s overlap of a 2 s loud window → ratio ~0.3 → medium
    assert 0.4 <= mid["overlap_seconds"] <= 0.9
    assert mid["severity"] in ("medium", "low")


# ── Different bands → no conflict ───────────────────────────────────


def test_different_bands_not_flagged() -> None:
    """Stem A in sub band, stem B in air band. Both loud throughout but
    in different bands — no conflict."""
    a = _stem_from_audio("sub_bass.wav", _sine(40.0, 4.0))
    b = _stem_from_audio("hihat.wav", _sine(10000.0, 4.0))
    conflicts = detect_time_aware_masking([a, b])
    assert conflicts == []


# ── Quiet stems below threshold not counted ─────────────────────────


def test_below_threshold_not_flagged() -> None:
    """Very quiet stems (band energy below -40 dB) shouldn't trigger conflicts."""
    quiet_a = _sine(1000.0, 4.0, amp=0.000001)  # ~-64 dB band energy
    quiet_b = _sine(1000.0, 4.0, amp=0.000001)
    a = _stem_from_audio("noise_a.wav", quiet_a)
    b = _stem_from_audio("noise_b.wav", quiet_b)
    conflicts = detect_time_aware_masking([a, b])
    assert conflicts == []


# ── Three stems, all simultaneous → still flagged ───────────────────


def test_three_stems_all_simultaneous() -> None:
    sig = _sine(1000.0, 3.0)
    stems = [_stem_from_audio(f"pad_{i}.wav", sig.copy()) for i in range(3)]
    conflicts = detect_time_aware_masking(stems)
    assert any(c["band"] == "mid" for c in conflicts)
    mid = next(c for c in conflicts if c["band"] == "mid")
    assert len(mid["stems"]) == 3


# ── min_overlap_s knob respected ────────────────────────────────────


def test_min_overlap_threshold_can_suppress() -> None:
    """Raising min_overlap_s above the actual overlap duration should
    suppress the conflict."""
    sig = _sine(1000.0, 4.0)
    a = _stem_from_audio("a.wav", sig)
    b = _stem_from_audio("b.wav", sig.copy())
    conflicts_loose = detect_time_aware_masking([a, b], min_overlap_s=0.5)
    conflicts_strict = detect_time_aware_masking([a, b], min_overlap_s=10.0)
    assert any(c["band"] == "mid" for c in conflicts_loose)
    assert conflicts_strict == []


# ── threshold_db knob respected ─────────────────────────────────────


def test_threshold_db_knob_can_suppress() -> None:
    """Raising the loudness threshold should leave the moderate signal below it.
    A 0.05-amplitude sine produces ~+30 dB band energy in mid; threshold_db=40
    is above that so the conflict gets suppressed."""
    sig = _sine(1000.0, 4.0, amp=0.05)
    a = _stem_from_audio("a.wav", sig)
    b = _stem_from_audio("b.wav", sig.copy())
    conflicts_default = detect_time_aware_masking([a, b])
    conflicts_strict = detect_time_aware_masking([a, b], threshold_db=40.0)
    assert any(c["band"] == "mid" for c in conflicts_default)
    assert conflicts_strict == []
