"""Tests for audio analysis — stereo metrics in particular."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.spectral import AudioAnalysis, analyze_audio


SAMPLE_RATE = 44100
DURATION_S = 0.5  # short — tests should be fast


def _write_wav(path: Path, samples: np.ndarray, sr: int = SAMPLE_RATE) -> None:
    """Write a float array to a WAV file. `samples` shape (N,) or (N, 2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, sr, subtype="PCM_16")


def _stereo_sine(freq_l: float, freq_r: float, duration: float = DURATION_S) -> np.ndarray:
    """Stereo sine: independent frequency per channel."""
    n = int(duration * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    L = 0.3 * np.sin(2 * np.pi * freq_l * t)
    R = 0.3 * np.sin(2 * np.pi * freq_r * t)
    return np.stack([L, R], axis=1)


def _mono_sine(freq: float, duration: float = DURATION_S) -> np.ndarray:
    n = int(duration * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    return 0.3 * np.sin(2 * np.pi * freq * t)


def _centered_stereo(signal: np.ndarray) -> np.ndarray:
    """Same signal on both channels → perfect mono in a stereo container."""
    return np.stack([signal, signal], axis=1)


def _out_of_phase_stereo(signal: np.ndarray) -> np.ndarray:
    """L and inverted R → correlation = -1, phase cancellation risk."""
    return np.stack([signal, -signal], axis=1)


def test_mono_file_has_no_stereo_fields(tmp_path):
    path = tmp_path / "mono.wav"
    _write_wav(path, _mono_sine(440.0))
    a = analyze_audio(path)
    assert a.channels == 1
    assert a.correlation is None
    assert a.side_ratio_db is None
    assert a.side_balance is None
    # to_dict should expose None values
    d = a.to_dict()
    assert d["correlation"] is None
    assert d["side_balance"] is None


def test_centered_stereo_has_correlation_near_one(tmp_path):
    """Identical L and R → correlation = 1.0, side content ~0."""
    path = tmp_path / "centered.wav"
    _write_wav(path, _centered_stereo(_mono_sine(440.0)))
    a = analyze_audio(path)
    assert a.channels == 2
    assert a.correlation is not None
    assert a.correlation > 0.99, f"centered stereo should correlate ~1.0, got {a.correlation}"
    # side = (L-R)/2 = 0 for identical channels → side_ratio_db → -inf
    # After to_dict flooring at -120, it should be -120.
    d = a.to_dict()
    assert d["side_ratio_db"] <= -100


def test_out_of_phase_stereo_has_negative_correlation(tmp_path):
    """L inverted vs R → correlation = -1, all energy in side."""
    path = tmp_path / "oop.wav"
    _write_wav(path, _out_of_phase_stereo(_mono_sine(440.0)))
    a = analyze_audio(path)
    assert a.correlation is not None
    assert a.correlation < -0.99, f"out-of-phase should correlate ~-1.0, got {a.correlation}"
    # mid = (L+R)/2 = 0 → mid_rms ~0 → side_ratio_db → +inf. Floored at -120
    # only for negative; we floor mid_rms at 1e-9. Expect a very positive dB.
    assert a.side_ratio_db > 30


def test_wide_stereo_has_moderate_correlation(tmp_path):
    """Different frequencies on each channel → low correlation, lots of side."""
    path = tmp_path / "wide.wav"
    _write_wav(path, _stereo_sine(440.0, 660.0))
    a = analyze_audio(path)
    assert a.correlation is not None
    assert -0.2 < a.correlation < 0.2, (
        f"uncorrelated tones should give near-zero correlation, got {a.correlation}"
    )
    # side_ratio_db should be close to 0 (roughly equal mid and side for this case)
    assert -10 < a.side_ratio_db < 10


def test_side_balance_uses_same_bands_as_spectral_balance(tmp_path):
    path = tmp_path / "wide.wav"
    _write_wav(path, _stereo_sine(440.0, 660.0))
    a = analyze_audio(path)
    assert a.side_balance is not None
    # Must have the same seven bands as spectral_balance
    assert set(a.side_balance.keys()) == set(a.spectral_balance.keys())


def test_silent_stereo_stays_correlated(tmp_path):
    """Silent on both channels → correlation 1.0 (safe default, no nan)."""
    path = tmp_path / "silence.wav"
    _write_wav(path, np.zeros((int(DURATION_S * SAMPLE_RATE), 2)))
    a = analyze_audio(path)
    assert a.correlation == 1.0  # no nan leak
    assert a.status == "silent"


def test_to_dict_clamps_correlation_to_range(tmp_path):
    """Correlation is mathematically in [-1, 1] but numerical jitter can exceed."""
    a = AudioAnalysis(
        path="/fake",
        sample_rate=44100,
        duration_s=1.0,
        channels=2,
        lufs=-20.0,
        true_peak_db=-6.0,
        spectral_centroid_hz=1000.0,
        spectral_balance={},
        rms_db=-20.0,
        correlation=1.0000001,  # float precision overflow
        side_ratio_db=-15.0,
        side_balance={},
    )
    d = a.to_dict()
    assert -1.0 <= d["correlation"] <= 1.0
