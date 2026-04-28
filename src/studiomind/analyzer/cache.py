"""
Analysis cache: persists STFT-derived analysis artifacts per WAV so the
agent never has to re-render to inspect a file.

Layout: one `<wav_stem>.npz` per cached analysis under the workspace's
`.studiomind/analyses/` directory. Each entry holds:

  - `meta_json`: a JSON-serialised dict with the cache version, the source
    WAV's fingerprint (size + mtime), the STFT params used to compute it,
    and the full `AudioAnalysis.to_dict()` summary.
  - `stft_mag` (optional): float16 magnitude matrix `(frames, bins)` from
    the STFT pass. Float16 halves the on-disk footprint at the cost of
    ~0.001 dB resolution — well below any audible threshold.

Cache is invalidated automatically when the WAV's size or mtime changes,
or when the cache version is bumped (e.g. analyzer-format change).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from studiomind.analyzer.spectral import AudioAnalysis


class CachedAnalysis(NamedTuple):
    """A cache hit: summary + (optional) STFT magnitudes + STFT params.

    Behaves as a tuple `(analysis, stft_mag, stft_params)` for callers that
    just want unpacking, and exposes `.analysis / .stft_mag / .stft_params`
    for clarity at call sites.
    """

    analysis: AudioAnalysis
    stft_mag: np.ndarray | None
    stft_params: dict | None

# Bump this when the cache schema or the AudioAnalysis fields change in a
# way that makes prior entries unreadable / wrong.
CACHE_VERSION = "1"


def cache_path_for(wav_path: str | Path, analyses_dir: str | Path) -> Path:
    """The cache .npz path for a given WAV.

    Keyed by the WAV's stem so the same filename always maps to the same
    cache entry. The fingerprint check below ensures we don't hand back a
    stale entry if the WAV was rewritten.
    """
    return Path(analyses_dir) / f"{Path(wav_path).stem}.npz"


def _wav_fingerprint(wav_path: Path) -> dict[str, int]:
    st = wav_path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _atomic_save(path: Path, **arrays: np.ndarray) -> None:
    """Write an .npz to `path` atomically (tmp + replace).

    Avoids a partial cache file if the writer is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        np.savez_compressed(tmp_path, **arrays)
        # np.savez_compressed adds .npz unless the path already ends in it.
        # tempfile gave us a path that ends in .tmp so np added .npz → .tmp.npz
        actual = tmp_path.with_suffix(tmp_path.suffix + ".npz")
        if actual.exists():
            os.replace(actual, path)
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        else:
            os.replace(tmp_path, path)
    except Exception:
        for p in (tmp_path, tmp_path.with_suffix(tmp_path.suffix + ".npz")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        raise


def write_analysis(
    wav_path: str | Path,
    analyses_dir: str | Path,
    analysis: AudioAnalysis,
    *,
    stft_mag: np.ndarray | None = None,
    stft_params: dict[str, Any] | None = None,
) -> Path:
    """Persist `analysis` (and optionally its STFT magnitude matrix) for `wav_path`.

    Returns the final cache file path.
    """
    wav_path = Path(wav_path)
    cache_path = cache_path_for(wav_path, analyses_dir)
    meta = {
        "version": CACHE_VERSION,
        "wav_path": str(wav_path),
        "fingerprint": _wav_fingerprint(wav_path),
        "stft_params": stft_params,
        "summary": analysis.to_dict(),
    }
    arrays: dict[str, np.ndarray] = {
        # Plain numpy unicode array — no pickle on read.
        "meta_json": np.array(json.dumps(meta)),
    }
    if stft_mag is not None:
        arrays["stft_mag"] = np.asarray(stft_mag, dtype=np.float16)
    _atomic_save(cache_path, **arrays)
    return cache_path


def read_analysis(
    wav_path: str | Path,
    analyses_dir: str | Path,
) -> CachedAnalysis | None:
    """Return a `CachedAnalysis` if a current cache entry exists, else None.

    Iterates as `(analysis, stft_mag, stft_params)` for tuple-unpacking
    callers and exposes named attributes for clarity. `stft_mag` /
    `stft_params` are None when the cache was written without an STFT pass.

    Returns None on any of: missing cache, missing WAV, version mismatch,
    fingerprint drift (size/mtime change), corrupt file.
    """
    wav_path = Path(wav_path)
    cache_path = cache_path_for(wav_path, analyses_dir)
    if not cache_path.exists() or not wav_path.exists():
        return None
    try:
        with np.load(cache_path, allow_pickle=False) as data:
            meta_raw = data["meta_json"]
            meta = json.loads(str(meta_raw.item() if meta_raw.shape == () else meta_raw))
            if meta.get("version") != CACHE_VERSION:
                return None
            if meta.get("fingerprint") != _wav_fingerprint(wav_path):
                return None
            analysis = _analysis_from_dict(meta["summary"])
            stft_mag = (
                np.asarray(data["stft_mag"]) if "stft_mag" in data.files else None
            )
            stft_params = meta.get("stft_params")
        return CachedAnalysis(analysis=analysis, stft_mag=stft_mag, stft_params=stft_params)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def has_cache(wav_path: str | Path, analyses_dir: str | Path) -> bool:
    """True if a current (fingerprint-matching, version-matching) cache exists."""
    return read_analysis(wav_path, analyses_dir) is not None


def invalidate(wav_path: str | Path, analyses_dir: str | Path) -> None:
    """Remove the cache entry for `wav_path` if it exists. Idempotent."""
    cache_path = cache_path_for(wav_path, analyses_dir)
    try:
        cache_path.unlink()
    except FileNotFoundError:
        pass


def _analysis_from_dict(d: dict[str, Any]) -> AudioAnalysis:
    """Reconstruct an AudioAnalysis from its `to_dict()` form.

    Tolerates new optional fields by defaulting them; legacy entries that
    don't have stereo fields round-trip with correlation/side_* = None.
    """
    return AudioAnalysis(
        path=d["path"],
        sample_rate=d["sample_rate"],
        duration_s=d["duration_s"],
        channels=d["channels"],
        lufs=d["lufs"],
        true_peak_db=d["true_peak_db"],
        spectral_centroid_hz=d["spectral_centroid_hz"],
        spectral_balance=d["spectral_balance"],
        rms_db=d["rms_db"],
        status=d.get("status", "ok"),
        correlation=d.get("correlation"),
        side_ratio_db=d.get("side_ratio_db"),
        side_balance=d.get("side_balance"),
        crest_factor_db=d.get("crest_factor_db"),
        lra_lu=d.get("lra_lu"),
        transient_density_per_s=d.get("transient_density_per_s"),
        top_resonances=d.get("top_resonances"),
        correlation_min=d.get("correlation_min"),
        fundamental_hz=d.get("fundamental_hz"),
        voicing_ratio=d.get("voicing_ratio"),
    )
