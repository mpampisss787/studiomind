"""
Reference matching: compare a track's spectral envelope to a reference's.

The agent calls `compare_to_reference(track_path, reference_path,
analyses_dir)` to get a "where am I hot/shy vs the reference?" diff.

Output is a per-1/3-octave-band delta in dB after loudness-normalising
the two so absolute level differences don't dominate the diff. Reads
both files from the analysis cache; runs an analyse on demand if either
isn't cached yet.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from studiomind.analyzer.cache import read_analysis
from studiomind.analyzer.pipeline import analyze_and_cache


# ISO standard 1/3-octave centers from 20 Hz to 20 kHz.
THIRD_OCTAVE_CENTERS_HZ: tuple[float, ...] = (
    20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160,
    200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600,
    2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000,
    20000,
)


def _band_edges(center: float) -> tuple[float, float]:
    """1/3-octave band edges around `center` (Hz). 2^(±1/6) factor."""
    factor = 2.0 ** (1.0 / 6.0)
    return (center / factor, center * factor)


def _third_octave_envelope(
    stft_mag: np.ndarray, freqs: np.ndarray
) -> np.ndarray:
    """Mean per-1/3-octave-band magnitude (dB) over the full file.

    Returns a `(N_bands,)` array, one dB value per band center.
    Empty/sub-range bands return -120 dB.
    """
    mean_mag = stft_mag.mean(axis=0)
    out = np.full(len(THIRD_OCTAVE_CENTERS_HZ), -120.0, dtype=np.float64)
    for i, center in enumerate(THIRD_OCTAVE_CENTERS_HZ):
        lo, hi = _band_edges(center)
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            continue
        band_energy = float(np.sum(mean_mag[mask] ** 2))
        if band_energy > 0:
            out[i] = 10.0 * math.log10(band_energy + 1e-10)
    return out


def _ensure_cached(path: str | Path, analyses_dir: str | Path):
    """Return a `CachedAnalysis`, or None if the file can't be analysed.

    Runs `analyze_and_cache` on cache miss. Returns None for any of:
    file missing, ffmpeg unavailable for non-native, decode failure.
    """
    cached = read_analysis(path, analyses_dir)
    if cached is not None and cached.stft_mag is not None and cached.stft_params is not None:
        return cached
    try:
        analyze_and_cache(path, analyses_dir)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    except Exception:
        # ffmpeg / decode failures from the ingest layer
        return None
    return read_analysis(path, analyses_dir)


def _envelope_for(path: str | Path, analyses_dir: str | Path) -> np.ndarray | None:
    """Read or build the cached STFT and return its 1/3-octave envelope."""
    cached = _ensure_cached(path, analyses_dir)
    if cached is None or cached.stft_mag is None or cached.stft_params is None:
        return None
    sr = cached.analysis.sample_rate
    n_fft = int(cached.stft_params["n_fft"])
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr).astype(np.float32)
    return _third_octave_envelope(np.asarray(cached.stft_mag, dtype=np.float32), freqs)


def _build_summary(bands: list[dict], top_n: int = 3) -> str:
    """Human-readable one-liner highlighting the largest deltas."""
    sortable = [b for b in bands if b["delta_db"] is not None]
    if not sortable:
        return "No comparable bands found."
    sortable.sort(key=lambda b: abs(b["delta_db"]), reverse=True)
    parts: list[str] = []
    for b in sortable[:top_n]:
        sign = "+" if b["delta_db"] > 0 else ""
        parts.append(f"{int(b['hz'])} Hz {sign}{b['delta_db']:.1f} dB")
    direction_hint = ""
    biggest = sortable[0]
    if biggest["delta_db"] > 0:
        direction_hint = " (track is hot vs reference)"
    elif biggest["delta_db"] < 0:
        direction_hint = " (track is shy vs reference)"
    return f"Largest deltas: {', '.join(parts)}{direction_hint}"


def compare_to_reference(
    track_path: str | Path,
    reference_path: str | Path,
    analyses_dir: str | Path,
) -> dict:
    """Compare `track`'s spectral envelope to `reference`'s.

    Returns:
        {
          "track": <basename>,
          "reference": <basename>,
          "loudness_offset_db": float,   # how much track was shifted to match reference
          "bands": [{hz, track_db, reference_db, delta_db}, ...],  # 31 entries
          "summary": "Largest deltas: 60 Hz +4.2 dB, 200 Hz -3.1 dB, ...",
        }

    `delta_db = track_db - reference_db`, so positive means the track has
    more energy in that band than the reference.
    """
    track_p = Path(track_path)
    ref_p = Path(reference_path)

    track_env = _envelope_for(track_p, analyses_dir)
    ref_env = _envelope_for(ref_p, analyses_dir)

    if track_env is None or ref_env is None:
        missing: list[str] = []
        if track_env is None:
            missing.append(track_p.name)
        if ref_env is None:
            missing.append(ref_p.name)
        return {
            "track": track_p.name,
            "reference": ref_p.name,
            "loudness_offset_db": 0.0,
            "bands": [],
            "summary": (
                f"Could not analyse: {', '.join(missing)}. The watcher caches "
                "every ingested file automatically; verify the files are in the "
                "workspace and decodable."
            ),
            "error": "missing_envelope",
        }

    # Loudness-normalise: shift track so its mean band energy matches the
    # reference's. Compute mean only over non-floor bands so ultra-low /
    # ultra-high silence doesn't bias the offset.
    track_audible = track_env[track_env > -100]
    ref_audible = ref_env[ref_env > -100]
    if len(track_audible) and len(ref_audible):
        offset_db = float(np.mean(ref_audible) - np.mean(track_audible))
    else:
        offset_db = 0.0

    track_norm = track_env + offset_db

    bands: list[dict] = []
    for i, center in enumerate(THIRD_OCTAVE_CENTERS_HZ):
        t_db = float(track_norm[i])
        r_db = float(ref_env[i])
        # If either side is a floor (no energy), the delta is uninformative.
        if t_db <= -100 and r_db <= -100:
            delta = None
        else:
            delta = round(t_db - r_db, 1)
        bands.append({
            "hz": float(center),
            "track_db": round(t_db, 1) if t_db > -100 else None,
            "reference_db": round(r_db, 1) if r_db > -100 else None,
            "delta_db": delta,
        })

    return {
        "track": track_p.name,
        "reference": ref_p.name,
        "loudness_offset_db": round(offset_db, 1),
        "bands": bands,
        "summary": _build_summary(bands),
    }
