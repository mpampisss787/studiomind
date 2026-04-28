"""
Decode-on-ingest: any audio file the user drops in becomes a WAV on disk
that the analyzer can read. Native formats (WAV/FLAC/AIFF/OGG) pass
through unchanged; everything else (MP3/M4A/WMA/AAC/OPUS/...) is
transcoded via `ffmpeg`.

ffmpeg is the universal codec backend (see vault ADR
`Projects/StudioMind/decisions.md` — "Universal audio format support
via decode-on-ingest"). Centralising format-handling at the boundary
keeps the analyzer WAV-only and avoids a thicket of pure-Python codec
libs.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class DecodeError(Exception):
    """ffmpeg ran but produced no usable output, or returned non-zero."""


class FFmpegNotAvailable(Exception):
    """ffmpeg is not on PATH and we need it to decode this file."""


# Formats `soundfile` (libsndfile) handles directly — no transcoding needed.
NATIVE_EXTS: frozenset[str] = frozenset({
    ".wav", ".flac", ".aiff", ".aif", ".ogg", ".oga",
})

# Common formats that need ffmpeg for decoding.
FFMPEG_EXTS: frozenset[str] = frozenset({
    ".mp3", ".m4a", ".aac", ".wma", ".opus", ".webm",
    ".mp4", ".mov", ".mkv", ".alac", ".ape", ".wv",
})

SUPPORTED_EXTS: frozenset[str] = NATIVE_EXTS | FFMPEG_EXTS


def is_supported(path: str | Path) -> bool:
    """True if this file's extension is something we can decode."""
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def needs_decode(path: str | Path) -> bool:
    """True if this file requires ffmpeg transcoding to be analyzable."""
    suffix = Path(path).suffix.lower()
    return suffix in FFMPEG_EXTS or (suffix not in NATIVE_EXTS and suffix not in FFMPEG_EXTS)


def decoded_path_for(path: str | Path) -> Path:
    """Where the decoded WAV lives.

    Sits next to the original with a `.decoded.wav` suffix so the original
    file is preserved (the user dropped it intentionally) and the watcher
    doesn't re-trigger on what we wrote.
    """
    p = Path(path)
    return p.with_name(p.stem + ".decoded.wav")


def is_ffmpeg_available() -> bool:
    """Quick PATH lookup. Returns False without running ffmpeg."""
    return shutil.which("ffmpeg") is not None


def decode_to_wav(
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Return the WAV path to analyze.

    For native formats (WAV/FLAC/AIFF/OGG) returns `path` unchanged.
    For ffmpeg-needing formats (MP3/M4A/...) transcodes into
    `<stem>.decoded.wav` next to the original.

    Caching: if the `.decoded.wav` already exists and is newer than the
    source, it is returned without re-running ffmpeg. Pass `force=True`
    to override.
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(src)

    if not needs_decode(src):
        return src

    out = decoded_path_for(src)

    # Cache hit: decoded copy is current.
    if not force and out.exists():
        try:
            if out.stat().st_mtime >= src.stat().st_mtime:
                return out
        except OSError:
            pass  # fall through to re-decode

    if not is_ffmpeg_available():
        raise FFmpegNotAvailable(
            f"ffmpeg is required to decode {src.suffix} files. "
            "Install: `winget install ffmpeg` (Win) / "
            "`brew install ffmpeg` (macOS) / `apt install ffmpeg` (Linux)."
        )

    # 16-bit PCM is plenty for analysis; analyzer reads as float64 anyway.
    # `-vn` strips video for container formats (MP4/MOV/MKV).
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "pcm_s16le",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        # Best-effort cleanup of a partial output
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        raise DecodeError(f"ffmpeg timed out after 5 minutes on {src.name}") from e

    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        raise DecodeError(
            f"ffmpeg failed on {src.name} (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )

    logger.info("Decoded %s → %s (%d bytes)", src.name, out.name, out.stat().st_size)
    return out


def warn_if_ffmpeg_missing() -> None:
    """One-shot startup warning if ffmpeg is missing.

    Call from CLI / web-server entry points so the user finds out at
    launch, not the first time they drop an MP3.
    """
    if not is_ffmpeg_available():
        logger.warning(
            "ffmpeg not on PATH — non-native audio formats (MP3/M4A/WMA/AAC) "
            "won't decode. Install via your platform's package manager."
        )
