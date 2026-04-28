"""
Canonical analysis pipeline: decode-if-needed → analyze → cache.

Use `analyze_and_cache(path, analyses_dir)` from any caller that wants a
full analysis with the cache warm. Cache hit returns instantly; cache
miss runs the STFT analyzer and persists the result for subsequent
drill-down tools.

The decode step is a no-op for native formats (WAV/FLAC/AIFF/OGG) and a
ffmpeg transcode for everything else. The cache is keyed by the WAV
file's stem (decoded or otherwise), so non-native files end up with a
`<stem>.decoded.npz` cache entry sitting next to the analysis dir.
"""

from __future__ import annotations

import logging
from pathlib import Path

from studiomind.analyzer.cache import (
    has_cache,
    read_analysis,
    write_analysis,
)
from studiomind.analyzer.spectral import AudioAnalysis, analyze_audio_full
from studiomind.ingest.decode import decode_to_wav

logger = logging.getLogger(__name__)


def analyze_and_cache(
    path: str | Path,
    analyses_dir: str | Path,
    *,
    force: bool = False,
) -> AudioAnalysis:
    """Decode-if-needed, analyze, write cache, return the analysis.

    On cache hit, returns the cached `AudioAnalysis` without recomputing.
    On cache miss (or `force=True`), runs the STFT analyzer and persists.

    Raises:
      FileNotFoundError if `path` doesn't exist.
      FFmpegNotAvailable if the file needs transcoding and ffmpeg is missing.
      DecodeError if ffmpeg fails on a transcodable file.
      Whatever soundfile raises if the file is unreadable.
    """
    path = Path(path)
    analyses_dir = Path(analyses_dir)

    wav_path = decode_to_wav(path)

    if not force:
        cached = read_analysis(wav_path, analyses_dir)
        if cached is not None:
            return cached.analysis

    analysis, stft_mag, stft_params = analyze_audio_full(wav_path)
    try:
        write_analysis(
            wav_path,
            analyses_dir,
            analysis,
            stft_mag=stft_mag,
            stft_params=stft_params,
        )
    except Exception as e:
        # Cache failure should never break the caller — analysis is the
        # value, the cache is a perf optimization.
        logger.warning("Failed to cache analysis for %s: %s", wav_path.name, e)

    return analysis


def is_cached(path: str | Path, analyses_dir: str | Path) -> bool:
    """True if a current cache entry exists for `path` (or its decoded form)."""
    path = Path(path)
    if not path.exists():
        return False
    # For native files the cache key is the file's own stem; for decoded
    # files it's the decoded WAV's stem. Look up via the decoded path
    # without actually transcoding (use the predicted path).
    from studiomind.ingest.decode import decoded_path_for, needs_decode
    target = decoded_path_for(path) if needs_decode(path) else path
    if not target.exists():
        return False
    return has_cache(target, analyses_dir)
