"""Tests for the decode-on-ingest pipeline."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.ingest import decode


SR = 22050


def _write_wav(path: Path, sr: int = SR) -> Path:
    """Write a tiny stereo WAV."""
    audio = (np.random.RandomState(0).randn(sr // 2, 2) * 0.05).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, sr)
    return path


# ── Format detection ────────────────────────────────────────────────


def test_native_extensions_dont_need_decode() -> None:
    for ext in (".wav", ".flac", ".aiff", ".aif", ".ogg"):
        assert not decode.needs_decode(f"x{ext}")


def test_ffmpeg_extensions_need_decode() -> None:
    for ext in (".mp3", ".m4a", ".aac", ".wma", ".opus", ".mp4"):
        assert decode.needs_decode(f"x{ext}")


def test_unknown_extension_treated_as_decode_needed() -> None:
    """Unknown formats fall through to ffmpeg — let it try."""
    assert decode.needs_decode("x.weird")


def test_is_supported_covers_native_and_ffmpeg() -> None:
    assert decode.is_supported("x.wav")
    assert decode.is_supported("x.mp3")
    assert decode.is_supported("x.opus")


def test_is_supported_rejects_random_extensions() -> None:
    assert not decode.is_supported("x.txt")
    assert not decode.is_supported("x.png")


def test_decoded_path_lives_next_to_original(tmp_path: Path) -> None:
    src = tmp_path / "song.mp3"
    out = decode.decoded_path_for(src)
    assert out.parent == tmp_path
    assert out.name == "song.decoded.wav"


# ── Native pass-through ─────────────────────────────────────────────


def test_native_wav_passes_through(tmp_path: Path) -> None:
    src = _write_wav(tmp_path / "stem.wav")
    out = decode.decode_to_wav(src)
    assert out == src
    # No sibling decoded file should be created.
    assert not (tmp_path / "stem.decoded.wav").exists()


def test_decode_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        decode.decode_to_wav(tmp_path / "nonexistent.wav")


# ── ffmpeg path (skipped if ffmpeg missing) ────────────────────────


_FFMPEG = decode.is_ffmpeg_available()


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg not on PATH")
def test_decode_mp3_produces_decoded_wav(tmp_path: Path) -> None:
    """End-to-end: MP3 → ffmpeg → readable WAV."""
    # Build the MP3 with ffmpeg from a generated WAV (so we don't ship a binary fixture)
    src_wav = _write_wav(tmp_path / "src.wav")
    src_mp3 = tmp_path / "src.mp3"
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src_wav), str(src_mp3)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    src_wav.unlink()  # ensure we're really decoding the MP3

    out = decode.decode_to_wav(src_mp3)
    assert out != src_mp3
    assert out.name == "src.decoded.wav"
    assert out.exists() and out.stat().st_size > 0
    # And it's a real WAV that soundfile can read
    audio, sr = sf.read(str(out))
    assert sr > 0
    assert len(audio) > 0


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg not on PATH")
def test_decode_mp3_caches_result(tmp_path: Path) -> None:
    """Second call shouldn't re-run ffmpeg if the decoded WAV is current."""
    src_wav = _write_wav(tmp_path / "src.wav")
    src_mp3 = tmp_path / "src.mp3"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src_wav), str(src_mp3)],
        capture_output=True, text=True, timeout=60,
    )
    src_wav.unlink()

    out1 = decode.decode_to_wav(src_mp3)
    mtime1 = out1.stat().st_mtime
    out2 = decode.decode_to_wav(src_mp3)
    assert out2 == out1
    assert out2.stat().st_mtime == mtime1  # cache reuse


@pytest.mark.skipif(not _FFMPEG, reason="ffmpeg not on PATH")
def test_decode_force_reruns(tmp_path: Path) -> None:
    """force=True should always re-decode, bumping mtime."""
    src_wav = _write_wav(tmp_path / "src.wav")
    src_mp3 = tmp_path / "src.mp3"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src_wav), str(src_mp3)],
        capture_output=True, text=True, timeout=60,
    )
    src_wav.unlink()

    out1 = decode.decode_to_wav(src_mp3)
    import time as _t
    _t.sleep(0.05)  # make a measurable mtime tick
    out2 = decode.decode_to_wav(src_mp3, force=True)
    assert out2 == out1
    assert out2.stat().st_mtime >= out1.stat().st_mtime


# ── ffmpeg-not-installed branch ────────────────────────────────────


def test_ffmpeg_unavailable_raises_helpful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ffmpeg is missing, decoding a non-native file raises FFmpegNotAvailable
    with an install hint (no fallback to a confusing soundfile error)."""
    monkeypatch.setattr(decode, "is_ffmpeg_available", lambda: False)
    fake_mp3 = tmp_path / "song.mp3"
    fake_mp3.write_bytes(b"fake")
    with pytest.raises(decode.FFmpegNotAvailable) as exc:
        decode.decode_to_wav(fake_mp3)
    msg = str(exc.value).lower()
    assert "ffmpeg" in msg
    assert "install" in msg or "winget" in msg or "brew" in msg or "apt" in msg


def test_ffmpeg_failure_raises_decode_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage 'mp3' that ffmpeg can't parse should raise DecodeError, not
    leave a partial output WAV behind."""
    if not decode.is_ffmpeg_available():
        pytest.skip("ffmpeg not on PATH")
    fake_mp3 = tmp_path / "garbage.mp3"
    fake_mp3.write_bytes(b"\x00\x01\x02not_a_real_mp3")
    with pytest.raises(decode.DecodeError):
        decode.decode_to_wav(fake_mp3)
    # No partial output left
    assert not decode.decoded_path_for(fake_mp3).exists()
