"""
Hand-rolled YIN pitch detection (de Cheveigné & Kawahara, 2002).

Two functions:
  - `yin_pitch(frame, sr, ...)` — single-frame fundamental + confidence
  - `estimate_fundamental(audio, sr, ...)` — per-frame YIN over the whole
    file, returning `(median_pitch_hz, voicing_ratio)`

Stays in pure numpy so we don't pay numba's install + JIT-warmup cost on
Win11 ARM64 + Prism (the target platform). Probabilistic-YIN's voicing
nuance is approximated by a hard CMND threshold + median aggregation.

See ADR `Projects/StudioMind/decisions.md` (2026-04-27 — Hand-rolled YIN
over librosa.pyin).
"""

from __future__ import annotations

import numpy as np


def yin_pitch(
    frame: np.ndarray,
    sr: int,
    *,
    fmin: float = 50.0,
    fmax: float = 1000.0,
    threshold: float = 0.15,
) -> tuple[float, float]:
    """Single-frame YIN.

    Returns `(pitch_hz, confidence)`. Confidence is in [0, 1] and is
    `1 - cmnd_at_chosen_tau`. Returns `(0.0, 0.0)` for unvoiced frames.
    """
    n = len(frame)
    if n < 4:
        return 0.0, 0.0

    tau_min = max(2, int(round(sr / fmax)))
    tau_max = min(n // 2, int(round(sr / fmin)))
    if tau_max <= tau_min + 1:
        return 0.0, 0.0

    x = np.asarray(frame, dtype=np.float64)
    if not np.any(x):
        return 0.0, 0.0

    # Difference function via autocorrelation:
    #   d(τ) = E_left(τ) + E_right(τ) - 2 * autocorr(τ)
    nfft = 1 << ((2 * n - 1).bit_length())
    X = np.fft.rfft(x, n=nfft)
    autocorr = np.fft.irfft(X * np.conj(X), n=nfft)[: tau_max + 1]

    sq = x * x
    total_sq = float(sq.sum())
    cum_sq = np.cumsum(sq)
    # cum_sq[k] = sum_{j=0..k} x[j]^2

    taus = np.arange(0, tau_max + 1)
    e_left = np.array([
        float(cum_sq[n - t - 1]) if n - t - 1 >= 0 else 0.0 for t in taus
    ])
    e_right = np.array([
        float(total_sq - (cum_sq[t - 1] if t > 0 else 0.0)) for t in taus
    ])
    d = e_left + e_right - 2.0 * autocorr
    d = np.maximum(d, 0.0)

    # Cumulative mean normalized difference (CMND):
    #   cmnd(0) = 1
    #   cmnd(τ) = d(τ) * τ / sum_{i=1..τ} d(i)
    cmnd = np.ones(tau_max + 1, dtype=np.float64)
    cum_d = np.cumsum(d[1:])
    for t in range(1, tau_max + 1):
        denom = cum_d[t - 1]
        cmnd[t] = (d[t] * t / denom) if denom > 1e-12 else 1.0

    # First τ in [tau_min, tau_max-1] where cmnd dips below threshold AND
    # is a local minimum.
    selected = -1
    for t in range(tau_min, tau_max):
        if cmnd[t] < threshold and cmnd[t] < cmnd[t - 1] and cmnd[t] <= cmnd[t + 1]:
            selected = t
            break

    if selected < 0:
        return 0.0, 0.0

    # Parabolic interpolation around the chosen τ.
    s0, s1, s2 = cmnd[selected - 1], cmnd[selected], cmnd[selected + 1]
    denom = 2.0 * (s0 - 2.0 * s1 + s2)
    offset = (s0 - s2) / denom if abs(denom) > 1e-12 else 0.0
    t_refined = float(selected) + float(offset)

    if t_refined <= 0:
        return 0.0, 0.0

    pitch_hz = float(sr) / t_refined
    confidence = float(max(0.0, 1.0 - cmnd[selected]))
    return pitch_hz, confidence


def estimate_fundamental(
    audio: np.ndarray,
    sr: int,
    *,
    fmin: float = 50.0,
    fmax: float = 1000.0,
    threshold: float = 0.15,
    frame_size: int | None = None,
    hop: int | None = None,
    voicing_confidence: float = 0.5,
) -> tuple[float | None, float]:
    """Per-frame YIN over `audio`, returning `(pitch_hz, voicing_ratio)`.

    `pitch_hz` is the median fundamental across voiced frames (None if
    nothing met the voicing threshold). `voicing_ratio` is the fraction
    of frames that voted as voiced — useful as a "is this a tonal stem?"
    signal: bass/vocals/leads voice >0.6, kicks/hats/noise <0.2.

    Uses a 50 ms / 50 % overlap window by default.
    """
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    if frame_size is None:
        # ~50 ms window, similar to STFT but slightly larger for low-pitch reach.
        frame_size = max(512, int(2 ** round(np.log2(sr * 0.05))))
    if hop is None:
        hop = frame_size // 2

    if len(audio) < frame_size:
        return None, 0.0

    n_frames = (len(audio) - frame_size) // hop + 1
    if n_frames < 1:
        return None, 0.0

    pitches: list[float] = []
    voiced = 0
    for i in range(n_frames):
        frame = audio[i * hop : i * hop + frame_size]
        pitch, conf = yin_pitch(
            frame, sr, fmin=fmin, fmax=fmax, threshold=threshold,
        )
        if pitch > 0.0 and conf >= voicing_confidence:
            pitches.append(pitch)
            voiced += 1

    voicing_ratio = voiced / n_frames if n_frames else 0.0
    if not pitches:
        return None, voicing_ratio
    return float(np.median(pitches)), voicing_ratio
