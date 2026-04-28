"""
Time-aware masking detection.

The whole-file `detect_masking` in spectral.py flags any band where two
stems both have significant energy — but it can't tell whether they
actually play *at the same time*. Verse-only and chorus-only instruments
that share a band get falsely flagged.

This module compares stems frame-by-frame. A conflict is only reported
when both stems are loud in the same band *in the same frames*, for at
least `MIN_OVERLAP_SECONDS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from studiomind.analyzer.spectral import BANDS

# A frame is "loud in this band" if its band energy is above this dB level
# (relative to the band's full FFT magnitude floor — see _band_energy_db).
LOUDNESS_THRESHOLD_DB = -40.0

# Minimum simultaneous-loud time before a conflict is reported.
MIN_OVERLAP_SECONDS = 0.5


@dataclass
class StemFrames:
    """An entry to feed into time-aware masking detection."""

    name: str
    stft_mag: np.ndarray   # (n_frames, n_bins) magnitude
    freqs: np.ndarray      # (n_bins,) — Hz of each bin
    sample_rate: int
    hop: int               # samples between frames

    @property
    def frame_period_s(self) -> float:
        return self.hop / float(self.sample_rate)


def _band_loudness_db(stem: StemFrames, band: tuple[float, float]) -> np.ndarray:
    """Per-frame band energy in dB. Empty bands return -120 dB.

    `band` is `(f_low, f_high)` in Hz. Sums squared magnitudes across the
    in-band bins per frame, then 10*log10.
    """
    f_low, f_high = band
    mask = (stem.freqs >= f_low) & (stem.freqs < f_high)
    if not np.any(mask):
        return np.full(stem.stft_mag.shape[0], -120.0, dtype=np.float32)
    band_mag = stem.stft_mag[:, mask]
    energy = np.sum(band_mag.astype(np.float32) ** 2, axis=1)
    out = np.full(energy.shape, -120.0, dtype=np.float32)
    nz = energy > 0
    out[nz] = 10.0 * np.log10(energy[nz] + 1e-10)
    return out


def detect_time_aware_masking(
    stems: list[StemFrames],
    *,
    threshold_db: float = LOUDNESS_THRESHOLD_DB,
    min_overlap_s: float = MIN_OVERLAP_SECONDS,
) -> list[dict]:
    """Return masking conflicts that survive a time-overlap check.

    Each conflict: `{band, frequency_range, stems[{name, loud_seconds}],
    overlap_seconds, total_seconds, severity}`.

    Severity:
      * "high"   — overlap > 50 % of the shorter stem's loud time
      * "medium" — overlap > 25 %
      * "low"    — meets `min_overlap_s` but below 25 %
    """
    if len(stems) < 2:
        return []

    # Pre-compute "loud-frame" boolean masks per stem per band.
    per_stem_band_loud: dict[str, dict[str, np.ndarray]] = {}
    per_stem_loud_seconds: dict[str, dict[str, float]] = {}
    per_stem_total_seconds: dict[str, float] = {}

    for stem in stems:
        per_stem_band_loud[stem.name] = {}
        per_stem_loud_seconds[stem.name] = {}
        per_stem_total_seconds[stem.name] = (
            stem.stft_mag.shape[0] * stem.frame_period_s
        )
        for band_name, band in BANDS.items():
            band_db = _band_loudness_db(stem, band)
            loud = band_db > threshold_db
            per_stem_band_loud[stem.name][band_name] = loud
            per_stem_loud_seconds[stem.name][band_name] = float(
                loud.sum() * stem.frame_period_s
            )

    conflicts: list[dict] = []
    for band_name, band in BANDS.items():
        # All pairs of stems that are individually loud in this band
        loud_in_band = [
            stem for stem in stems
            if per_stem_loud_seconds[stem.name][band_name] >= min_overlap_s
        ]
        if len(loud_in_band) < 2:
            continue

        # For frame-overlap, all stems involved must share a frame rate.
        # Truncate to the shortest length and compute the boolean AND.
        min_frames = min(
            per_stem_band_loud[s.name][band_name].shape[0] for s in loud_in_band
        )
        if min_frames == 0:
            continue
        frame_period_s = loud_in_band[0].frame_period_s

        all_loud = np.ones(min_frames, dtype=bool)
        for s in loud_in_band:
            all_loud &= per_stem_band_loud[s.name][band_name][:min_frames]
        overlap_frames = int(all_loud.sum())
        overlap_seconds = overlap_frames * frame_period_s
        if overlap_seconds < min_overlap_s:
            continue

        # Severity = overlap_seconds vs the LEAST-loud stem in this band
        # (so a long-playing pad doesn't dominate the ratio when the kick
        # only hits 4 times).
        ref_loud_s = min(
            per_stem_loud_seconds[s.name][band_name] for s in loud_in_band
        )
        ratio = overlap_seconds / ref_loud_s if ref_loud_s > 0 else 0.0
        if ratio > 0.50:
            severity = "high"
        elif ratio > 0.25:
            severity = "medium"
        else:
            severity = "low"

        total_seconds = max(per_stem_total_seconds[s.name] for s in loud_in_band)

        conflicts.append({
            "band": band_name,
            "frequency_range": list(band),
            "stems": [
                {
                    "name": Path(s.name).name,
                    "loud_seconds": round(per_stem_loud_seconds[s.name][band_name], 2),
                }
                for s in loud_in_band
            ],
            "overlap_seconds": round(overlap_seconds, 2),
            "total_seconds": round(total_seconds, 2),
            "ratio": round(ratio, 2),
            "severity": severity,
        })

    # Sort high → medium → low, then by overlap_seconds desc.
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    conflicts.sort(key=lambda c: (severity_rank[c["severity"]], -c["overlap_seconds"]))
    return conflicts
