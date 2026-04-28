"""Tests for `compare_to_reference` — 1/3-octave spectral envelope diff."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.pipeline import analyze_and_cache
from studiomind.analyzer.reference import (
    THIRD_OCTAVE_CENTERS_HZ,
    compare_to_reference,
)


SR = 22050


def _write(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, SR)
    return path


def _sine(freq: float, duration_s: float, amp: float = 0.3) -> np.ndarray:
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _pink_ish(duration_s: float, amp: float = 0.1) -> np.ndarray:
    """Crude pink-ish noise (white noise filtered with 1/f bias) for full-spectrum tests."""
    rng = np.random.default_rng(0)
    n = int(duration_s * SR)
    white = rng.standard_normal(n).astype(np.float32)
    # Apply 1/f-ish weighting via FFT
    spec = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    weights = np.where(freqs > 0, 1.0 / np.sqrt(freqs + 1e-3), 1.0)
    spec *= weights
    out = np.fft.irfft(spec, n=n).astype(np.float32)
    out *= amp / (np.max(np.abs(out)) + 1e-12)
    return out


# ── Shape contract ──────────────────────────────────────────────────


def test_returns_one_band_per_third_octave_center(tmp_path: Path) -> None:
    track = _write(tmp_path / "track.wav", _pink_ish(1.0))
    ref = _write(tmp_path / "ref.wav", _pink_ish(1.0))
    analyses = tmp_path / "analyses"
    out = compare_to_reference(track, ref, analyses)
    assert "bands" in out
    assert len(out["bands"]) == len(THIRD_OCTAVE_CENTERS_HZ)
    for band in out["bands"]:
        assert {"hz", "track_db", "reference_db", "delta_db"} <= set(band)


def test_loudness_offset_neutralises_overall_level(tmp_path: Path) -> None:
    """Same spectral shape, different overall amplitude → small per-band deltas
    after loudness normalisation."""
    base = _pink_ish(1.0, amp=0.05)
    track = _write(tmp_path / "track.wav", base)
    ref = _write(tmp_path / "ref.wav", base * 4.0)  # +12 dB overall
    analyses = tmp_path / "analyses"

    out = compare_to_reference(track, ref, analyses)
    # Loudness offset should compensate for the 4× amplitude gap (~+12 dB).
    assert abs(out["loudness_offset_db"] - 12.0) < 2.0
    # After normalisation, per-band deltas should be small.
    deltas = [b["delta_db"] for b in out["bands"] if b["delta_db"] is not None]
    assert max(abs(d) for d in deltas) < 4.0


def test_band_with_extra_energy_shows_positive_delta(tmp_path: Path) -> None:
    """Track has extra 1 kHz energy on top of base noise → positive delta around 1 kHz."""
    base = _pink_ish(1.0, amp=0.05)
    track = _write(tmp_path / "track.wav", base + _sine(1000.0, 1.0, amp=0.3))
    ref = _write(tmp_path / "ref.wav", base.copy())
    analyses = tmp_path / "analyses"

    out = compare_to_reference(track, ref, analyses)
    # Find the band closest to 1 kHz
    near_1k = min(out["bands"], key=lambda b: abs(b["hz"] - 1000.0))
    assert near_1k["delta_db"] is not None
    assert near_1k["delta_db"] > 3.0  # track is hotter at 1 kHz


def test_summary_mentions_largest_deltas(tmp_path: Path) -> None:
    base = _pink_ish(1.0, amp=0.05)
    track = _write(tmp_path / "track.wav", base + _sine(1000.0, 1.0, amp=0.3))
    ref = _write(tmp_path / "ref.wav", base.copy())
    analyses = tmp_path / "analyses"

    out = compare_to_reference(track, ref, analyses)
    assert "summary" in out
    assert "Hz" in out["summary"]
    # Should mention the 1 kHz hotspot we created
    assert "1000" in out["summary"] or "800" in out["summary"] or "1250" in out["summary"]


# ── Caching: comparison is fast on second call ──────────────────────


def test_uses_cached_analysis_on_second_compare(tmp_path: Path) -> None:
    track = _write(tmp_path / "track.wav", _pink_ish(1.0))
    ref = _write(tmp_path / "ref.wav", _pink_ish(1.0))
    analyses = tmp_path / "analyses"

    # Warm both caches first
    analyze_and_cache(track, analyses)
    analyze_and_cache(ref, analyses)

    out1 = compare_to_reference(track, ref, analyses)
    out2 = compare_to_reference(track, ref, analyses)
    # Idempotent
    assert out1["bands"] == out2["bands"]


def test_compare_self_yields_near_zero_deltas(tmp_path: Path) -> None:
    audio = _pink_ish(1.0)
    track = _write(tmp_path / "same.wav", audio)
    analyses = tmp_path / "analyses"
    out = compare_to_reference(track, track, analyses)
    deltas = [b["delta_db"] for b in out["bands"] if b["delta_db"] is not None]
    # Comparing a file to itself: zero offset, zero deltas.
    assert abs(out["loudness_offset_db"]) < 0.01
    assert max(abs(d) for d in deltas) < 0.01


# ── Error path ──────────────────────────────────────────────────────


def test_missing_file_returns_error_payload(tmp_path: Path) -> None:
    analyses = tmp_path / "analyses"
    out = compare_to_reference(
        tmp_path / "no_track.wav",
        tmp_path / "no_ref.wav",
        analyses,
    )
    assert out["bands"] == []
    assert "error" in out
    assert "no_track.wav" in out["summary"] or "no_ref.wav" in out["summary"]
