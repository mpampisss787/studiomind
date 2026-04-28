"""Tests for the new STFT-derived summary fields:
crest_factor_db, lra_lu, transient_density_per_s, top_resonances,
correlation_min, plus the analyze_audio_full() shape contract.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.spectral import (
    AudioAnalysis,
    BANDS,
    _compute_stft,
    _stft_n_fft,
    analyze_audio,
    analyze_audio_full,
)


SAMPLE_RATE = 44100


def _write(path: Path, samples: np.ndarray, sr: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, sr, subtype="PCM_16")
    return path


def _sine(freq: float, duration_s: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    return 0.3 * np.sin(2 * np.pi * freq * t).astype(np.float32)


# ── analyze_audio_full shape contract ───────────────────────────────


def test_analyze_audio_full_returns_triple(tmp_path: Path) -> None:
    p = _write(tmp_path / "sine.wav", _sine(440.0, 1.0))
    a, stft, params = analyze_audio_full(p)
    assert isinstance(a, AudioAnalysis)
    assert stft.ndim == 2  # (frames, bins)
    assert stft.dtype == np.float32
    assert "n_fft" in params and "hop" in params and "window" in params
    assert params["window"] == "hann"
    assert params["hop"] == params["n_fft"] // 2


def test_analyze_audio_returns_just_summary(tmp_path: Path) -> None:
    p = _write(tmp_path / "sine.wav", _sine(440.0, 1.0))
    a = analyze_audio(p)
    assert isinstance(a, AudioAnalysis)


# ── STFT helper ─────────────────────────────────────────────────────


def test_stft_n_fft_targets_46ms_window() -> None:
    # ~46ms target → 1024 @ 22050, 2048 @ 44100, 2048 @ 48000
    assert _stft_n_fft(22050) == 1024
    assert _stft_n_fft(44100) == 2048
    assert _stft_n_fft(48000) == 2048


def test_stft_pads_short_input() -> None:
    sr = 44100
    short = (0.1 * np.random.randn(100)).astype(np.float32)  # < 1 window
    mag, freqs, params = _compute_stft(short, sr)
    assert mag.shape[0] >= 1
    assert mag.shape[1] == params["n_fft"] // 2 + 1
    assert freqs[-1] <= sr / 2 + 1


def test_stft_frame_count_for_long_signal() -> None:
    sr = 44100
    audio = (0.1 * np.random.randn(sr * 5)).astype(np.float32)  # 5 s
    mag, _, params = _compute_stft(audio, sr)
    expected = (sr * 5 - params["n_fft"]) // params["hop"] + 1
    assert mag.shape[0] == expected


# ── crest_factor_db ─────────────────────────────────────────────────


def test_crest_factor_higher_on_transient_than_sustained(tmp_path: Path) -> None:
    sr = SAMPLE_RATE
    duration = 1.0
    n = int(duration * sr)

    # Sustained sine → crest = 20*log10(sqrt(2)) ≈ 3.0 dB
    sine = _sine(440.0, duration, sr)
    sine_path = _write(tmp_path / "sine.wav", sine)
    sine_a = analyze_audio(sine_path)

    # Transient: a single peak at 0.99, mostly silence → crest very high
    transient = np.zeros(n, dtype=np.float32)
    transient[n // 2] = 0.99
    transient[n // 2 + 1] = 0.5
    transient_path = _write(tmp_path / "spike.wav", transient)
    transient_a = analyze_audio(transient_path)

    assert sine_a.crest_factor_db is not None
    assert transient_a.crest_factor_db is not None
    assert 2.5 < sine_a.crest_factor_db < 4.0  # ~3 dB for a sine
    assert transient_a.crest_factor_db > 30.0  # spike vs near-silence


def test_crest_factor_none_on_silence(tmp_path: Path) -> None:
    p = _write(tmp_path / "silence.wav", np.zeros(SAMPLE_RATE, dtype=np.float32))
    a = analyze_audio(p)
    assert a.crest_factor_db is None


# ── lra_lu ──────────────────────────────────────────────────────────


def test_lra_higher_on_dynamic_content(tmp_path: Path) -> None:
    """Two-segment file: -30 dB then -10 dB → high LRA. Constant level → low LRA."""
    sr = SAMPLE_RATE
    seg = 4 * sr  # 4 s per segment

    quiet = _sine(440.0, 4.0, sr) * 0.03   # ~ -30 dB
    loud = _sine(440.0, 4.0, sr) * 0.3     # ~ -10 dB
    dynamic = np.concatenate([quiet, loud])
    flat = _sine(440.0, 8.0, sr) * 0.1     # constant level

    dyn_path = _write(tmp_path / "dynamic.wav", dynamic)
    flat_path = _write(tmp_path / "flat.wav", flat)

    dyn_lra = analyze_audio(dyn_path).lra_lu
    flat_lra = analyze_audio(flat_path).lra_lu

    if dyn_lra is None or flat_lra is None:
        pytest.skip("pyloudnorm not available")
    assert dyn_lra > flat_lra
    assert dyn_lra > 5.0


def test_lra_none_on_short_file(tmp_path: Path) -> None:
    """Files shorter than one short-term window return None for LRA."""
    p = _write(tmp_path / "tiny.wav", _sine(440.0, 0.5))
    a = analyze_audio(p)
    assert a.lra_lu is None


# ── transient_density_per_s ─────────────────────────────────────────


def test_transient_density_higher_for_pulses(tmp_path: Path) -> None:
    """Pulse train → high transient density. Sustained sine → near zero."""
    sr = SAMPLE_RATE
    duration = 4.0
    n = int(duration * sr)

    sine = _sine(440.0, duration, sr)
    sine_path = _write(tmp_path / "sine.wav", sine)

    # 8 sharp transients spread across 4 seconds
    pulses = np.zeros(n, dtype=np.float32)
    for hit_s in np.linspace(0.2, duration - 0.2, 8):
        idx = int(hit_s * sr)
        # Short attack-decay
        decay = (np.exp(-np.arange(0, 0.02 * sr) / (0.005 * sr))).astype(np.float32)
        end = min(n, idx + len(decay))
        pulses[idx:end] += decay[: end - idx] * 0.5
    pulses_path = _write(tmp_path / "pulses.wav", pulses)

    sine_density = analyze_audio(sine_path).transient_density_per_s
    pulse_density = analyze_audio(pulses_path).transient_density_per_s

    assert sine_density is not None
    assert pulse_density is not None
    assert sine_density < 0.5
    assert pulse_density > sine_density
    assert pulse_density >= 1.0  # at least 1 transient/s


def test_transient_density_none_for_very_short(tmp_path: Path) -> None:
    """Files shorter than the rolling window return None."""
    sr = 44100
    short = (0.1 * np.random.randn(int(0.05 * sr))).astype(np.float32)
    p = _write(tmp_path / "short.wav", short, sr)
    a = analyze_audio(p)
    # 0.05 s with 2048-sample n_fft / 1024 hop ≈ 1 frame → not enough for rolling median
    assert a.transient_density_per_s is None


# ── top_resonances ──────────────────────────────────────────────────


def test_top_resonance_finds_sine_frequency(tmp_path: Path) -> None:
    p = _write(tmp_path / "sine.wav", _sine(1000.0, 1.0))
    a = analyze_audio(p)
    assert a.top_resonances
    top = a.top_resonances[0]
    assert abs(top["hz"] - 1000.0) < 50.0
    assert top["prominence_db"] > 6.0
    assert "q_est" in top


def test_top_resonances_capped_at_three(tmp_path: Path) -> None:
    # Mix of several sines — analyzer should return at most 3
    sr = SAMPLE_RATE
    t = np.arange(sr) / sr
    audio = sum(0.1 * np.sin(2 * np.pi * f * t) for f in [200, 500, 1000, 2500, 5000])
    audio = audio.astype(np.float32)
    p = _write(tmp_path / "five_sines.wav", audio)
    a = analyze_audio(p)
    assert a.top_resonances is not None
    assert len(a.top_resonances) <= 3


def test_top_resonances_empty_on_silence(tmp_path: Path) -> None:
    p = _write(tmp_path / "silence.wav", np.zeros(SAMPLE_RATE, dtype=np.float32))
    a = analyze_audio(p)
    # On silence, top_resonances may be [] or None — both are fine.
    assert not a.top_resonances


def test_top_resonances_skip_subaudible(tmp_path: Path) -> None:
    """A sine below 30 Hz should not register as a top resonance peak."""
    p = _write(tmp_path / "sub.wav", _sine(15.0, 1.0))
    a = analyze_audio(p)
    if a.top_resonances:
        # If anything is reported, none of them should be the 15 Hz peak.
        for r in a.top_resonances:
            assert r["hz"] >= 30.0


# ── correlation_min ─────────────────────────────────────────────────


def test_correlation_min_none_on_mono(tmp_path: Path) -> None:
    p = _write(tmp_path / "mono.wav", _sine(440.0, 1.0))
    a = analyze_audio(p)
    assert a.correlation_min is None


def test_correlation_min_near_one_on_centered_stereo(tmp_path: Path) -> None:
    sig = _sine(440.0, 1.0)
    stereo = np.stack([sig, sig], axis=1)
    p = _write(tmp_path / "centered.wav", stereo)
    a = analyze_audio(p)
    assert a.correlation_min is not None
    assert a.correlation_min > 0.99


def test_correlation_min_catches_brief_phase_flip(tmp_path: Path) -> None:
    """A stereo file that's correlated for most of its duration but has a
    brief out-of-phase section should expose the dip via correlation_min,
    even though whole-file correlation looks fine."""
    sr = SAMPLE_RATE
    n = sr * 2  # 2 seconds total
    t = np.arange(n) / sr
    L = 0.3 * np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
    R = L.copy()
    # 100ms of phase inversion in the middle
    flip_start = int(0.95 * sr)
    flip_end = int(1.05 * sr)
    R[flip_start:flip_end] = -L[flip_start:flip_end]
    stereo = np.stack([L, R], axis=1)
    p = _write(tmp_path / "flicker.wav", stereo)
    a = analyze_audio(p)
    assert a.correlation_min is not None
    # Whole-file correlation stays positive; the min frame catches the flip.
    assert a.correlation is not None
    assert a.correlation > 0.5
    assert a.correlation_min < 0.0


# ── existing summary contract preserved ─────────────────────────────


def test_existing_fields_still_populated(tmp_path: Path) -> None:
    """Backward compatibility: every existing summary field is still set."""
    p = _write(tmp_path / "sine.wav", _sine(440.0, 1.0))
    a = analyze_audio(p)
    assert set(a.spectral_balance.keys()) == set(BANDS.keys())
    assert a.lufs is not None
    assert a.true_peak_db is not None
    assert a.spectral_centroid_hz > 0
    assert a.rms_db is not None
    assert a.status in ("ok", "silent")


def test_to_dict_includes_new_fields(tmp_path: Path) -> None:
    p = _write(tmp_path / "sine.wav", _sine(440.0, 1.0))
    a = analyze_audio(p)
    d = a.to_dict()
    for new_field in (
        "crest_factor_db",
        "lra_lu",
        "transient_density_per_s",
        "top_resonances",
        "correlation_min",
    ):
        assert new_field in d
