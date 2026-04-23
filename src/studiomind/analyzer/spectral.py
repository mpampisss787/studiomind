"""
Audio analysis: spectral profile, LUFS, true peak, masking detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf


@dataclass
class AudioAnalysis:
    """Result of analyzing an audio file."""

    path: str
    sample_rate: int
    duration_s: float
    channels: int
    lufs: float
    true_peak_db: float
    spectral_centroid_hz: float
    spectral_balance: dict[str, float]  # sub/low/mid/high/presence/air in dB
    rms_db: float

    def summary(self) -> str:
        """Human-readable summary for the agent."""
        lines = [
            f"Audio: {Path(self.path).name}",
            f"  Duration: {self.duration_s:.1f}s, {self.sample_rate}Hz, {self.channels}ch",
            f"  LUFS: {self.lufs:.1f}, True Peak: {self.true_peak_db:.1f} dB, RMS: {self.rms_db:.1f} dB",
            f"  Spectral centroid: {self.spectral_centroid_hz:.0f} Hz",
            "  Balance:",
        ]
        for band, db in self.spectral_balance.items():
            lines.append(f"    {band}: {db:.1f} dB")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        # Silent or near-silent audio produces -inf in dB values (log10(0)).
        # JSON can't encode inf/nan, so floor everything at -120 dB.
        import math

        def _safe(v: float, floor: float = -120.0) -> float:
            if v is None or not math.isfinite(v):
                return floor
            return round(v, 1)

        return {
            "path": self.path,
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 2),
            "channels": self.channels,
            "lufs": _safe(self.lufs),
            "true_peak_db": _safe(self.true_peak_db),
            "spectral_centroid_hz": _safe(self.spectral_centroid_hz, floor=0.0),
            "spectral_balance": {k: _safe(v) for k, v in self.spectral_balance.items()},
            "rms_db": _safe(self.rms_db),
        }


# Frequency band definitions (Hz)
BANDS = {
    "sub": (20, 60),
    "low": (60, 250),
    "low_mid": (250, 500),
    "mid": (500, 2000),
    "high_mid": (2000, 4000),
    "presence": (4000, 8000),
    "air": (8000, 20000),
}


def analyze_audio(path: str | Path) -> AudioAnalysis:
    """
    Analyze an audio file and return spectral + loudness metrics.

    Args:
        path: Path to a WAV file

    Returns:
        AudioAnalysis with LUFS, spectral balance, etc.
    """
    path = Path(path)
    audio, sr = sf.read(str(path), dtype="float64")

    # Mono or stereo
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    num_channels = audio.shape[1]
    duration = len(audio) / sr

    # Mix to mono for analysis
    mono = audio.mean(axis=1)

    # True peak (dBFS)
    true_peak = np.max(np.abs(audio))
    true_peak_db = 20 * np.log10(true_peak + 1e-10)

    # RMS (dBFS)
    rms = np.sqrt(np.mean(mono**2))
    rms_db = 20 * np.log10(rms + 1e-10)

    # LUFS (simplified K-weighted loudness)
    lufs = _compute_lufs(audio, sr)

    # FFT for spectral analysis
    fft_result = np.fft.rfft(mono)
    fft_magnitude = np.abs(fft_result)
    fft_freqs = np.fft.rfftfreq(len(mono), 1.0 / sr)

    # Spectral centroid
    total_energy = np.sum(fft_magnitude)
    if total_energy > 0:
        spectral_centroid = np.sum(fft_freqs * fft_magnitude) / total_energy
    else:
        spectral_centroid = 0.0

    # Band energy analysis
    spectral_balance = {}
    for band_name, (f_low, f_high) in BANDS.items():
        mask = (fft_freqs >= f_low) & (fft_freqs < f_high)
        band_energy = np.sum(fft_magnitude[mask] ** 2)
        if band_energy > 0:
            spectral_balance[band_name] = 10 * np.log10(band_energy + 1e-10)
        else:
            spectral_balance[band_name] = -120.0

    return AudioAnalysis(
        path=str(path),
        sample_rate=sr,
        duration_s=duration,
        channels=num_channels,
        lufs=lufs,
        true_peak_db=true_peak_db,
        spectral_centroid_hz=spectral_centroid,
        spectral_balance=spectral_balance,
        rms_db=rms_db,
    )


def _compute_lufs(audio: np.ndarray, sr: int) -> float:
    """
    Simplified integrated LUFS measurement.

    Uses K-weighting filter approximation. For production use,
    switch to pyloudnorm for ITU-R BS.1770 compliance.
    """
    try:
        import pyloudnorm as pyln

        meter = pyln.Meter(sr)
        loudness = meter.integrated_loudness(audio)
        return float(loudness)
    except ImportError:
        # Fallback: simple RMS-based approximation
        rms = np.sqrt(np.mean(audio**2))
        return float(20 * np.log10(rms + 1e-10))


def detect_masking(analyses: list[AudioAnalysis]) -> list[dict]:
    """
    Detect frequency bands where multiple stems compete.

    Args:
        analyses: List of AudioAnalysis for different stems

    Returns:
        List of masking conflicts: {band, stems, severity}
    """
    if len(analyses) < 2:
        return []

    conflicts = []
    for band_name in BANDS:
        # Find stems with significant energy in this band
        loud_stems = []
        for a in analyses:
            db = a.spectral_balance.get(band_name, -120)
            if db > -40:  # Threshold: significant energy
                loud_stems.append({"path": Path(a.path).name, "db": db})

        if len(loud_stems) >= 2:
            # Sort by energy, loudest first
            loud_stems.sort(key=lambda x: x["db"], reverse=True)
            severity = "high" if len(loud_stems) >= 3 else "medium"
            conflicts.append({
                "band": band_name,
                "frequency_range": BANDS[band_name],
                "stems": loud_stems,
                "severity": severity,
            })

    return conflicts
