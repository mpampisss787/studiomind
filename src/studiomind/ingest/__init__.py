"""Audio ingestion: format detection, decode-on-ingest, classification."""

from studiomind.ingest.decode import (
    DecodeError,
    FFmpegNotAvailable,
    NATIVE_EXTS,
    SUPPORTED_EXTS,
    decode_to_wav,
    decoded_path_for,
    is_ffmpeg_available,
    is_supported,
    needs_decode,
)

__all__ = [
    "DecodeError",
    "FFmpegNotAvailable",
    "NATIVE_EXTS",
    "SUPPORTED_EXTS",
    "decode_to_wav",
    "decoded_path_for",
    "is_ffmpeg_available",
    "is_supported",
    "needs_decode",
]
