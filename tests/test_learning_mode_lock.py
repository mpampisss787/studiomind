"""Tests for the training/mixing mode lock primitive."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from studiomind.learning import mode_lock


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "mode_lock.json"


# ───────────────────────────── basic acquire / release ────────────────

def test_initial_state_is_idle(lock_path: Path) -> None:
    state = mode_lock.read_lock(lock_path)
    assert state.mode == "idle"
    assert state.pid is None


def test_idle_to_mixing(lock_path: Path) -> None:
    state = mode_lock.acquire_mode("mixing", path=lock_path, pid=os.getpid())
    assert state.mode == "mixing"
    assert state.pid == os.getpid()
    assert lock_path.exists()


def test_idle_to_training(lock_path: Path) -> None:
    state = mode_lock.acquire_mode("training", path=lock_path, pid=os.getpid())
    assert state.mode == "training"


def test_release_returns_to_idle(lock_path: Path) -> None:
    mode_lock.acquire_mode("mixing", path=lock_path, pid=os.getpid())
    state = mode_lock.release_mode(path=lock_path, pid=os.getpid())
    assert state.mode == "idle"
    assert state.pid is None


def test_release_when_idle_is_noop(lock_path: Path) -> None:
    """Releasing an already-idle lock must not error."""
    state = mode_lock.release_mode(path=lock_path, pid=os.getpid())
    assert state.mode == "idle"


def test_same_pid_can_transition_modes(lock_path: Path) -> None:
    """Within one process, mode flips are allowed (caller-side
    enforcement keeps modes from running concurrently)."""
    pid = os.getpid()
    mode_lock.acquire_mode("mixing", path=lock_path, pid=pid)
    state = mode_lock.acquire_mode("training", path=lock_path, pid=pid)
    assert state.mode == "training"
    assert state.pid == pid


# ───────────────────────────── foreign owner protection ───────────────

def test_foreign_live_pid_blocks_acquire(lock_path: Path) -> None:
    """If the lock is held by a different process and that process is
    alive, a third process trying to acquire must be rejected."""
    foreign_pid = os.getpid()  # any live PID — this test process itself
    mode_lock.acquire_mode("mixing", path=lock_path, pid=foreign_pid)
    with pytest.raises(mode_lock.ModeLockError):
        mode_lock.acquire_mode("training", path=lock_path, pid=foreign_pid + 9_999_999)


def test_stale_pid_can_be_reclaimed(lock_path: Path, monkeypatch) -> None:
    """A lock owned by a dead PID must be reclaimed by the next
    arriving process."""
    fake_dead_pid = 999_999_998
    # Plant the lock as if a now-dead process held it.
    state_text = (
        '{"mode": "mixing", "pid": ' + str(fake_dead_pid) + ', "acquired_at": 0}'
    )
    lock_path.write_text(state_text)

    # Force _is_pid_alive to report False so we don't depend on the OS
    # not assigning that PID to a real process during the test.
    monkeypatch.setattr(mode_lock, "_is_pid_alive", lambda pid: False)

    state = mode_lock.acquire_mode("training", path=lock_path, pid=os.getpid())
    assert state.mode == "training"
    assert state.pid == os.getpid()


def test_release_by_different_live_pid_rejected(lock_path: Path) -> None:
    """Only the owner (or a stale-PID reclaim) can release the lock."""
    mode_lock.acquire_mode("mixing", path=lock_path, pid=os.getpid())
    # Another live PID trying to release isn't allowed.
    with pytest.raises(mode_lock.ModeLockError):
        mode_lock.release_mode(path=lock_path, pid=os.getpid() + 9_999_999)


# ───────────────────────────── corruption + invalid input ─────────────

def test_corrupt_lock_file_raises(lock_path: Path) -> None:
    lock_path.write_text("{not valid json")
    with pytest.raises(mode_lock.ModeLockError):
        mode_lock.read_lock(lock_path)


def test_invalid_mode_in_file_raises(lock_path: Path) -> None:
    lock_path.write_text('{"mode": "haxx", "pid": 1, "acquired_at": 0}')
    with pytest.raises(mode_lock.ModeLockError):
        mode_lock.read_lock(lock_path)


def test_acquire_invalid_mode_raises(lock_path: Path) -> None:
    with pytest.raises(ValueError):
        mode_lock.acquire_mode("haxx", path=lock_path, pid=os.getpid())  # type: ignore[arg-type]


def test_clear_lock_removes_file(lock_path: Path) -> None:
    mode_lock.acquire_mode("training", path=lock_path, pid=os.getpid())
    assert lock_path.exists()
    mode_lock.clear_lock(lock_path)
    assert not lock_path.exists()


# ───────────────────────────── persistence shape ──────────────────────

def test_lock_state_round_trip() -> None:
    state = mode_lock.LockState(mode="training", pid=42, acquired_at=123.456)
    parsed = mode_lock.LockState.from_json(state.to_json())
    assert parsed == state
