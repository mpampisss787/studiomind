"""Tests for the analysis cache layer."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer import cache as analysis_cache
from studiomind.analyzer.spectral import analyze_audio


@pytest.fixture
def tmp_wav(tmp_path: Path) -> Path:
    """A short, deterministic stereo WAV for round-tripping."""
    sr = 22050
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal((sr, 2)) * 0.1).astype(np.float32)
    p = tmp_path / "sample.wav"
    sf.write(p, audio, sr)
    return p


@pytest.fixture
def analyses_dir(tmp_path: Path) -> Path:
    return tmp_path / "analyses"


def test_round_trip_summary_only(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    result = analysis_cache.read_analysis(tmp_wav, analyses_dir)
    assert result is not None
    assert result.stft_mag is None
    assert result.stft_params is None
    assert result.analysis.to_dict() == a.to_dict()


def test_round_trip_with_stft(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    stft = np.random.default_rng(1).random((40, 1025)).astype(np.float32)

    analysis_cache.write_analysis(
        tmp_wav,
        analyses_dir,
        a,
        stft_mag=stft,
        stft_params={"window_size": 2048, "hop": 1024, "window": "hann"},
    )

    result = analysis_cache.read_analysis(tmp_wav, analyses_dir)
    assert result is not None
    assert result.stft_mag is not None
    assert result.stft_mag.shape == stft.shape
    assert result.stft_mag.dtype == np.float16
    assert result.stft_params == {"window_size": 2048, "hop": 1024, "window": "hann"}
    # Float16 round-trip introduces ~0.001 error on values in [0, 1]; verify
    # we're within that tolerance, not bit-exact.
    np.testing.assert_allclose(result.stft_mag.astype(np.float32), stft, atol=1e-3)


def test_missing_cache_returns_none(tmp_wav: Path, analyses_dir: Path) -> None:
    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None
    assert not analysis_cache.has_cache(tmp_wav, analyses_dir)


def test_missing_wav_returns_none(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    tmp_wav.unlink()
    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None


def test_invalidates_on_mtime_change(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)
    assert analysis_cache.has_cache(tmp_wav, analyses_dir)

    st = tmp_wav.stat()
    os.utime(tmp_wav, (st.st_atime + 5, st.st_mtime + 5))

    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None


def test_invalidates_on_size_change(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)
    # Append a byte; size changes even if mtime didn't.
    with tmp_wav.open("ab") as f:
        f.write(b"\x00")

    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None


def test_invalidates_on_version_bump(
    tmp_wav: Path, analyses_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    monkeypatch.setattr(analysis_cache, "CACHE_VERSION", "999")
    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None


def test_corrupt_cache_returns_none(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    cache_path = analysis_cache.cache_path_for(tmp_wav, analyses_dir)
    cache_path.write_bytes(b"definitely not an npz")

    assert analysis_cache.read_analysis(tmp_wav, analyses_dir) is None


def test_invalidate_idempotent(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    analysis_cache.invalidate(tmp_wav, analyses_dir)
    assert not analysis_cache.cache_path_for(tmp_wav, analyses_dir).exists()
    # Calling again must not raise.
    analysis_cache.invalidate(tmp_wav, analyses_dir)


def test_atomic_write_does_not_leave_tmp(tmp_wav: Path, analyses_dir: Path) -> None:
    a = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a)

    leftovers = [
        p for p in analyses_dir.iterdir()
        if p.suffix == ".tmp" or ".tmp." in p.name
    ]
    assert leftovers == []


def test_cache_path_keyed_by_stem(tmp_wav: Path, analyses_dir: Path) -> None:
    """Cache key is the WAV's stem so re-rendered files map to the same entry."""
    p = analysis_cache.cache_path_for(tmp_wav, analyses_dir)
    assert p.parent == analyses_dir
    assert p.stem == tmp_wav.stem
    assert p.suffix == ".npz"


def test_overwrite_replaces_entry(tmp_wav: Path, analyses_dir: Path) -> None:
    a1 = analyze_audio(tmp_wav)
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a1)

    # Pretend we re-analyzed and got a different reading.
    a2 = analyze_audio(tmp_wav)
    a2.lufs = -99.0
    analysis_cache.write_analysis(tmp_wav, analyses_dir, a2)

    result = analysis_cache.read_analysis(tmp_wav, analyses_dir)
    assert result is not None
    assert result[0].lufs == -99.0
