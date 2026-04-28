"""Tests for the drill-down tools (analyze_section, find_resonances,
compare_stems). All three read the analysis cache directly — no
re-render needed."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.drilldown import (
    analyze_section,
    compare_stems,
    find_resonances,
)
from studiomind.analyzer.pipeline import analyze_and_cache


SR = 22050


def _write(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, SR)
    return path


def _sine(freq: float, duration_s: float, amp: float = 0.3) -> np.ndarray:
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


# ── analyze_section ─────────────────────────────────────────────────


def test_analyze_section_returns_summary_with_section_metadata(tmp_path: Path) -> None:
    wav = _write(tmp_path / "x.wav", _sine(440.0, 4.0))
    out = analyze_section(wav, tmp_path / "analyses", start_s=1.0, end_s=2.5)
    assert "section" in out
    assert out["section"]["start_s"] == 1.0
    assert out["section"]["end_s"] == 2.5
    assert out["duration_s"] == pytest.approx(1.5, abs=0.05)
    assert out["sample_rate"] == SR
    assert "spectral_balance" in out


def test_analyze_section_rejects_invalid_window(tmp_path: Path) -> None:
    wav = _write(tmp_path / "x.wav", _sine(440.0, 1.0))
    with pytest.raises(ValueError):
        analyze_section(wav, tmp_path / "analyses", start_s=0.5, end_s=0.5)
    with pytest.raises(ValueError):
        analyze_section(wav, tmp_path / "analyses", start_s=-1.0, end_s=0.5)


def test_analyze_section_rejects_out_of_range(tmp_path: Path) -> None:
    """A section beyond the file end produces an empty slice → ValueError."""
    wav = _write(tmp_path / "x.wav", _sine(440.0, 1.0))
    with pytest.raises(ValueError):
        analyze_section(wav, tmp_path / "analyses", start_s=2.0, end_s=3.0)


def test_analyze_section_clamps_to_file_end(tmp_path: Path) -> None:
    """end_s past file end is clamped, not an error, as long as the slice is non-empty."""
    wav = _write(tmp_path / "x.wav", _sine(440.0, 1.0))
    out = analyze_section(wav, tmp_path / "analyses", start_s=0.5, end_s=10.0)
    assert out["duration_s"] == pytest.approx(0.5, abs=0.05)


# ── find_resonances ─────────────────────────────────────────────────


def test_find_resonances_returns_more_than_summary_top_3(tmp_path: Path) -> None:
    """Five-tone mix; default summary returns top 3, find_resonances can return up to top_n."""
    sig = sum(0.1 * _sine(f, 2.0) for f in [200, 500, 1000, 2500, 5000])
    wav = _write(tmp_path / "five.wav", sig.astype(np.float32))
    analyses = tmp_path / "analyses"
    analyze_and_cache(wav, analyses)

    peaks = find_resonances(wav, analyses, top_n=5)
    assert len(peaks) >= 3
    # Each peak has the expected fields
    for p in peaks:
        assert {"hz", "db", "q_est", "prominence_db"} <= set(p)


def test_find_resonances_threshold_filters_peaks(tmp_path: Path) -> None:
    sig = sum(0.1 * _sine(f, 2.0) for f in [200, 500, 1000, 2500, 5000])
    wav = _write(tmp_path / "five.wav", sig.astype(np.float32))
    analyses = tmp_path / "analyses"
    analyze_and_cache(wav, analyses)

    loose = find_resonances(wav, analyses, min_prominence_db=6.0, top_n=10)
    strict = find_resonances(wav, analyses, min_prominence_db=120.0, top_n=10)
    assert len(strict) <= len(loose)


def test_find_resonances_empty_for_uncached(tmp_path: Path) -> None:
    wav = _write(tmp_path / "x.wav", _sine(440.0, 1.0))
    analyses = tmp_path / "analyses"
    # Don't analyze first → no cache → no STFT → empty
    assert find_resonances(wav, analyses) == []


# ── compare_stems ───────────────────────────────────────────────────


def test_compare_stems_flags_simultaneous_band_overlap(tmp_path: Path) -> None:
    sig = _sine(1000.0, 4.0)
    wav_a = _write(tmp_path / "a.wav", sig)
    wav_b = _write(tmp_path / "b.wav", sig.copy())
    analyses = tmp_path / "analyses"
    analyze_and_cache(wav_a, analyses)
    analyze_and_cache(wav_b, analyses)

    out = compare_stems(wav_a, wav_b, analyses)
    assert out["stems"] == ["a.wav", "b.wav"]
    assert "conflicts" in out
    assert any(c["band"] == "mid" for c in out["conflicts"])


def test_compare_stems_no_conflict_for_disjoint_time(tmp_path: Path) -> None:
    a_audio = np.concatenate([_sine(1000.0, 2.0), _silence(2.0)])
    b_audio = np.concatenate([_silence(2.0), _sine(1000.0, 2.0)])
    wav_a = _write(tmp_path / "a.wav", a_audio)
    wav_b = _write(tmp_path / "b.wav", b_audio)
    analyses = tmp_path / "analyses"
    analyze_and_cache(wav_a, analyses)
    analyze_and_cache(wav_b, analyses)

    out = compare_stems(wav_a, wav_b, analyses)
    assert out["conflicts"] == []


def test_compare_stems_reports_missing_cache(tmp_path: Path) -> None:
    """When neither stem is cached, return an error message — not a crash."""
    wav_a = _write(tmp_path / "a.wav", _sine(1000.0, 1.0))
    wav_b = _write(tmp_path / "b.wav", _sine(1000.0, 1.0))
    analyses = tmp_path / "analyses"
    out = compare_stems(wav_a, wav_b, analyses)
    assert "error" in out
    assert out["conflicts"] == []
    assert "a.wav" in out["error"] and "b.wav" in out["error"]


def test_compare_stems_threshold_knob_passed_through(tmp_path: Path) -> None:
    """Strict threshold should suppress conflicts the default would flag."""
    sig = _sine(1000.0, 4.0, amp=0.05)  # ~+30 dB band energy
    wav_a = _write(tmp_path / "a.wav", sig)
    wav_b = _write(tmp_path / "b.wav", sig.copy())
    analyses = tmp_path / "analyses"
    analyze_and_cache(wav_a, analyses)
    analyze_and_cache(wav_b, analyses)

    default = compare_stems(wav_a, wav_b, analyses)
    strict = compare_stems(wav_a, wav_b, analyses, threshold_db=40.0)
    assert any(c["band"] == "mid" for c in default["conflicts"])
    assert strict["conflicts"] == []
