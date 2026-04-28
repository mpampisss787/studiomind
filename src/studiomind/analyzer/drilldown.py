"""
Drill-down tools that read the analysis cache instead of re-rendering.

Three operations the agent can call when the summary in
`AudioAnalysis.to_dict()` isn't precise enough:

  - `analyze_section(path, analyses_dir, start_s, end_s)` — analysis for
    a time window of an already-ingested file.
  - `find_resonances(path, analyses_dir, threshold_db, top_n)` — peak
    detection on the cached STFT with custom prominence threshold and
    arbitrary top-N (vs the summary's hardcoded top-3 at 6 dB).
  - `compare_stems(path_a, path_b, analyses_dir, ...)` — frame-overlap
    masking detection between two cached stems.

All three avoid any re-render. Section analysis re-reads the WAV and
re-runs the analyzer on the slice, but the cache provides quick path
resolution. Resonance + masking work entirely from the cached STFT.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from studiomind.analyzer.cache import read_analysis
from studiomind.analyzer.masking import (
    StemFrames,
    detect_time_aware_masking,
)
from studiomind.analyzer.spectral import (
    AudioAnalysis,
    _top_resonances,
    analyze_array,
)
from studiomind.ingest.decode import decode_to_wav


def _load_cached_stem_frames(
    wav_path: str | Path,
    analyses_dir: str | Path,
    name: str | None = None,
) -> StemFrames | None:
    """Read the cache and rebuild a `StemFrames` for masking / drill-down.

    Returns None if no cache, no STFT data in cache, or no STFT params
    (older entries).
    """
    cached = read_analysis(wav_path, analyses_dir)
    if cached is None or cached.stft_mag is None or cached.stft_params is None:
        return None
    sr = cached.analysis.sample_rate
    n_fft = int(cached.stft_params["n_fft"])
    hop = int(cached.stft_params["hop"])
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr).astype(np.float32)
    return StemFrames(
        name=name or Path(wav_path).name,
        stft_mag=np.asarray(cached.stft_mag, dtype=np.float32),
        freqs=freqs,
        sample_rate=int(sr),
        hop=hop,
    )


def analyze_section(
    path: str | Path,
    analyses_dir: str | Path,
    start_s: float,
    end_s: float,
) -> dict:
    """Run the analyzer on a time slice of `path`.

    Decode-if-needed first, slice in samples, run `analyze_array`. Doesn't
    write to cache (sections aren't keyed for re-lookup).
    """
    src = Path(path)
    wav = decode_to_wav(src)

    if start_s < 0 or end_s <= start_s:
        raise ValueError(
            f"Invalid section: start_s={start_s}, end_s={end_s} (need 0 ≤ start < end)"
        )

    audio, sr = sf.read(str(wav), dtype="float64")
    n = len(audio)
    s_start = max(0, int(start_s * sr))
    s_end = min(n, int(end_s * sr))
    if s_end <= s_start:
        raise ValueError(
            f"Section [{start_s:.2f}s, {end_s:.2f}s] is empty in a "
            f"{n / sr:.2f}s file."
        )

    section = audio[s_start:s_end]
    analysis, _stft_mag, _params = analyze_array(
        section, int(sr), source_path=f"{src.name}#section({start_s:.2f}-{end_s:.2f})",
    )
    out = analysis.to_dict()
    out["section"] = {"start_s": float(start_s), "end_s": float(end_s)}
    return out


def find_resonances(
    path: str | Path,
    analyses_dir: str | Path,
    *,
    min_prominence_db: float = 6.0,
    top_n: int = 5,
) -> list[dict]:
    """Return up to `top_n` spectral peaks for the cached file.

    Differs from the `top_resonances` summary field in two ways: caller
    controls both the prominence threshold (default 6 dB matches the
    summary) and `top_n` (default 5 here vs 3 in the summary). Empty list
    if no peaks meet the threshold or the file isn't cached with STFT.
    """
    cached = read_analysis(path, analyses_dir)
    if cached is None or cached.stft_mag is None or cached.stft_params is None:
        return []
    sr = cached.analysis.sample_rate
    n_fft = int(cached.stft_params["n_fft"])
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr).astype(np.float32)
    stft_mag = np.asarray(cached.stft_mag, dtype=np.float32)

    # Reuse the underlying peak finder — it already supports prominence and
    # configurable top-n.
    peaks = _top_resonances(stft_mag, freqs, n=top_n)
    return [p for p in peaks if p["prominence_db"] >= min_prominence_db]


def compare_stems(
    path_a: str | Path,
    path_b: str | Path,
    analyses_dir: str | Path,
    *,
    threshold_db: float = -40.0,
    min_overlap_s: float = 0.5,
) -> dict:
    """Time-aware masking comparison between two cached stems.

    Returns `{stems: [name_a, name_b], conflicts: [...]}`. Conflicts list
    has the same shape as `detect_time_aware_masking` output. Returns an
    empty conflicts list (and an `error` field) if either stem isn't
    cached with STFT data.
    """
    a = _load_cached_stem_frames(path_a, analyses_dir, name=Path(path_a).name)
    b = _load_cached_stem_frames(path_b, analyses_dir, name=Path(path_b).name)

    if a is None or b is None:
        missing: list[str] = []
        if a is None:
            missing.append(str(Path(path_a).name))
        if b is None:
            missing.append(str(Path(path_b).name))
        return {
            "stems": [Path(path_a).name, Path(path_b).name],
            "conflicts": [],
            "error": (
                f"No cached STFT for: {', '.join(missing)}. The watcher caches "
                "every ingested file automatically; if the file is in the "
                "workspace, give the watcher a moment, then retry."
            ),
        }

    conflicts = detect_time_aware_masking(
        [a, b],
        threshold_db=threshold_db,
        min_overlap_s=min_overlap_s,
    )
    return {
        "stems": [a.name, b.name],
        "conflicts": conflicts,
    }
