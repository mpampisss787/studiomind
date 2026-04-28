"""Tests for the watcher's auto-ingest scan: any WAV that lands in any
ingest folder gets analyzed and cached, regardless of how it got there.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studiomind.analyzer.cache import has_cache
from studiomind.analyzer.pipeline import analyze_and_cache, is_cached
from studiomind.workspace import WorkspaceSession, open_project


SR = 22050


def _real_wav(path: Path, duration_s: float = 0.5) -> Path:
    """Write a real (sf-readable) WAV so analyze_audio_full can read it."""
    n = int(SR * duration_s)
    audio = (np.random.RandomState(0).randn(n, 2) * 0.05).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, SR)
    return path


class _FakeFL:
    def read_mixer_track(self, _: int) -> dict:
        return {"index": 0, "name": "Master"}

    def solo_track(self, _: int, solo: bool = True) -> dict:
        return {"ok": True}

    def read_project_state(self) -> dict:
        return {"mixer_tracks": []}


@pytest.fixture
def session(tmp_path: Path) -> WorkspaceSession:
    project = open_project("TestProject", root=tmp_path)
    s = WorkspaceSession(_FakeFL(), project)
    yield s
    s.stop()


def _wait_for(predicate, timeout_s: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# ── analyze_and_cache: cache-warm semantics ─────────────────────────


def test_analyze_and_cache_cache_hit_on_second_call(tmp_path: Path) -> None:
    wav = _real_wav(tmp_path / "x.wav")
    analyses_dir = tmp_path / "analyses"

    a1 = analyze_and_cache(wav, analyses_dir)
    assert has_cache(wav, analyses_dir)

    # Second call should hit the cache. Cached `to_dict()` rounds to 1 dp, so
    # round-trip can drop the trailing decimals — verify they agree to that.
    a2 = analyze_and_cache(wav, analyses_dir)
    assert abs(a2.lufs - a1.lufs) < 0.1


def test_analyze_and_cache_force_recomputes(tmp_path: Path) -> None:
    wav = _real_wav(tmp_path / "x.wav")
    analyses_dir = tmp_path / "analyses"

    analyze_and_cache(wav, analyses_dir)
    assert is_cached(wav, analyses_dir)
    # force=True should re-analyze even though cache exists
    a = analyze_and_cache(wav, analyses_dir, force=True)
    assert a.lufs is not None


# ── Watcher auto-ingest ─────────────────────────────────────────────


def test_drop_in_references_gets_cached(session: WorkspaceSession) -> None:
    session.start()
    p = session.project.references_dir / "ref_track.wav"
    _real_wav(p)
    assert _wait_for(lambda: is_cached(p, session.project.analyses_dir)), (
        "reference drop wasn't auto-cached"
    )


def test_drop_in_drops_gets_cached(session: WorkspaceSession) -> None:
    session.start()
    p = session.project.drops_dir / "mystery.wav"
    _real_wav(p)
    assert _wait_for(lambda: is_cached(p, session.project.analyses_dir))


def test_drop_in_masters_gets_cached(session: WorkspaceSession) -> None:
    session.start()
    p = session.project.masters_dir / "old_bounce.wav"
    _real_wav(p)
    assert _wait_for(lambda: is_cached(p, session.project.analyses_dir))


def test_unsupported_file_skipped(session: WorkspaceSession) -> None:
    """A .txt file in drops/ should not show up as 'cached' nor crash the watcher."""
    session.start()
    p = session.project.drops_dir / "notes.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not audio")
    # Watcher should run a few ticks without crashing
    time.sleep(1.5)
    # And no .npz should have been created for it
    cache_path = session.project.analyses_dir / "notes.npz"
    assert not cache_path.exists()


def test_decoded_wav_outputs_not_double_analyzed(session: WorkspaceSession) -> None:
    """Files we wrote ourselves (`*.decoded.wav`) shouldn't trigger their own
    cache entry — they're cached through the original (non-WAV) filename.
    """
    session.start()
    p = session.project.drops_dir / "song.decoded.wav"
    _real_wav(p)
    time.sleep(1.5)
    cache_path = session.project.analyses_dir / "song.decoded.npz"
    assert not cache_path.exists()


def test_attempt_limit_blocks_repeat_failures(
    session: WorkspaceSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a file consistently fails to analyze, the watcher stops retrying it
    after _INGEST_MAX_ATTEMPTS instead of burning CPU each tick."""
    # Drop a file that looks supported but is unreadable. analyze_and_cache
    # will raise; we want the watcher to back off after a few attempts.
    p = session.project.drops_dir / "broken.wav"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not_a_real_wav_payload" * 10)

    call_count = {"n": 0}
    real_analyze_and_cache = __import__(
        "studiomind.analyzer.pipeline", fromlist=["analyze_and_cache"]
    ).analyze_and_cache

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return real_analyze_and_cache(*args, **kwargs)

    import studiomind.workspace as ws_mod
    monkeypatch.setattr(
        "studiomind.analyzer.pipeline.analyze_and_cache", counting
    )

    session.start()
    # Give the watcher enough ticks that it hits the attempt limit.
    time.sleep(2.5)

    assert call_count["n"] <= ws_mod.WorkspaceSession._INGEST_MAX_ATTEMPTS + 1, (
        f"watcher kept retrying broken file: {call_count['n']} calls"
    )
