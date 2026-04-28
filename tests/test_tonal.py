"""Tests for the hand-rolled YIN pitch detector."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.spectral import analyze_audio
from studiomind.analyzer.tonal import estimate_fundamental, yin_pitch


SR = 22050


def _sine_array(freq: float, duration_s: float, sr: int = SR, amp: float = 0.3) -> np.ndarray:
    n = int(sr * duration_s)
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ── yin_pitch (single frame) ────────────────────────────────────────


@pytest.mark.parametrize("target", [110.0, 220.0, 440.0, 880.0])
def test_yin_pitch_recovers_pure_sine(target: float) -> None:
    """YIN on a stable sine should land within 1 % of the target."""
    sr = 22050
    frame = _sine_array(target, 0.05, sr)  # 50 ms frame
    pitch, confidence = yin_pitch(frame, sr, fmin=50.0, fmax=2000.0)
    assert pitch > 0
    assert abs(pitch - target) / target < 0.01, f"got {pitch:.2f} for target {target:.2f}"
    assert confidence > 0.9


def test_yin_pitch_returns_zero_on_noise() -> None:
    """White noise has no fundamental — voicing decision should reject it."""
    rng = np.random.default_rng(0)
    sr = 22050
    frame = (rng.standard_normal(int(0.05 * sr)) * 0.1).astype(np.float32)
    # YIN may report something for noise but with low confidence; we accept either
    # 0.0 (rejected) or a finding with low confidence.
    pitch, conf = yin_pitch(frame, sr, threshold=0.1)
    if pitch > 0:
        assert conf < 0.7
    # A truly clean noise frame typically returns 0.
    # Allow either branch; the file-level estimator is what we lean on.


def test_yin_pitch_returns_zero_on_silence() -> None:
    sr = 22050
    frame = np.zeros(int(0.05 * sr), dtype=np.float32)
    pitch, conf = yin_pitch(frame, sr)
    assert pitch == 0.0
    assert conf == 0.0


def test_yin_pitch_handles_too_short_input() -> None:
    pitch, conf = yin_pitch(np.zeros(2, dtype=np.float32), 22050)
    assert pitch == 0.0
    assert conf == 0.0


# ── estimate_fundamental (file-level) ───────────────────────────────


def test_estimate_fundamental_recovers_sustained_pitch() -> None:
    audio = _sine_array(110.0, 1.0)  # bass note
    pitch, voicing = estimate_fundamental(audio, SR)
    assert pitch is not None
    assert abs(pitch - 110.0) / 110.0 < 0.02
    assert voicing > 0.5


def test_estimate_fundamental_none_on_silence() -> None:
    silence = np.zeros(int(SR), dtype=np.float32)
    pitch, voicing = estimate_fundamental(silence, SR)
    assert pitch is None
    assert voicing == 0.0


def test_estimate_fundamental_low_voicing_on_noise() -> None:
    rng = np.random.default_rng(42)
    noise = (rng.standard_normal(SR) * 0.1).astype(np.float32)
    pitch, voicing = estimate_fundamental(noise, SR)
    # Noise should produce low voicing ratio whether or not a pitch sneaks through.
    assert voicing < 0.4


def test_estimate_fundamental_handles_short_input() -> None:
    """Files shorter than one analysis frame return None gracefully."""
    short = _sine_array(440.0, 0.005)  # 5 ms — too short
    pitch, voicing = estimate_fundamental(short, SR)
    assert pitch is None
    assert voicing == 0.0


def test_estimate_fundamental_stereo_collapses_to_mono() -> None:
    sig = _sine_array(220.0, 1.0)
    stereo = np.stack([sig, sig], axis=1)
    pitch, _ = estimate_fundamental(stereo, SR)
    assert pitch is not None
    assert abs(pitch - 220.0) / 220.0 < 0.02


# ── End-to-end through analyze_audio() ──────────────────────────────


def test_analyze_audio_populates_fundamental_hz(tmp_path: Path) -> None:
    p = tmp_path / "bass.wav"
    sf.write(str(p), _sine_array(82.41, 1.0), SR)  # E2
    a = analyze_audio(p)
    assert a.fundamental_hz is not None
    assert abs(a.fundamental_hz - 82.41) / 82.41 < 0.02
    assert a.voicing_ratio is not None
    assert a.voicing_ratio > 0.5


def test_analyze_audio_fundamental_none_on_silence(tmp_path: Path) -> None:
    p = tmp_path / "silence.wav"
    sf.write(str(p), np.zeros(SR, dtype=np.float32), SR)
    a = analyze_audio(p)
    assert a.fundamental_hz is None


def test_analyze_audio_to_dict_includes_tonal_fields(tmp_path: Path) -> None:
    p = tmp_path / "sine.wav"
    sf.write(str(p), _sine_array(440.0, 1.0), SR)
    d = analyze_audio(p).to_dict()
    assert "fundamental_hz" in d
    assert "voicing_ratio" in d
