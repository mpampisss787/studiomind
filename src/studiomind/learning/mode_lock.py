"""Process-level mode lock for training vs mixing exclusivity.

One StudioMind process is in exactly one mode at a time: ``mixing``,
``training``, or ``idle``. The lock lives at
``~/StudioMind/state/mode_lock.json`` and stores the active mode plus
the PID of the process that owns it.

Acquiring a mode requires:

  * ``mode == "idle"`` and lock unowned, **or**
  * the recorded PID is no longer running on this host (stale lock —
    reclaim).

Releasing the lock either unsets the mode (back to idle) or hands off
to a different mode in the same process. Failing to release on shutdown
is recoverable on the next acquire via the stale-PID path.

The lock is process-level, not thread-level. Within a process, mode
exclusivity is enforced by the agent loops never instantiating each
other.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Mode = Literal["mixing", "training", "idle"]
VALID_MODES: tuple[str, ...] = ("mixing", "training", "idle")

STATE_DIR: Path = Path.home() / "StudioMind" / "state"
LOCK_PATH: Path = STATE_DIR / "mode_lock.json"


class ModeLockError(Exception):
    """Raised when the requested mode transition isn't permitted."""


@dataclass(frozen=True)
class LockState:
    mode: Mode
    pid: int | None
    acquired_at: float

    def to_json(self) -> str:
        return json.dumps(
            {"mode": self.mode, "pid": self.pid, "acquired_at": self.acquired_at},
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "LockState":
        data = json.loads(text)
        mode = data.get("mode", "idle")
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode in lock file: {mode!r}")
        return cls(
            mode=mode,
            pid=data.get("pid"),
            acquired_at=float(data.get("acquired_at", 0.0)),
        )


def _is_pid_alive(pid: int | None) -> bool:
    """Return True if PID exists in the kernel's process table.

    On POSIX, ``os.kill(pid, 0)`` is the canonical liveness check: it
    raises ``ProcessLookupError`` when the PID is gone, ``PermissionError``
    when the PID exists but isn't ours (still alive), and returns None
    when the PID exists and is ours.
    """
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists, owned by another user — still alive.
        return True
    return True


def read_lock(path: Path = LOCK_PATH) -> LockState:
    """Return the current lock state, or an idle state if no file exists."""
    if not path.exists():
        return LockState(mode="idle", pid=None, acquired_at=0.0)
    try:
        return LockState.from_json(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # Corrupt lock file — refuse to interpret it. The caller can choose
        # to repair via clear_lock(), but we never silently recover by
        # clobbering an unrecognised state.
        raise ModeLockError(f"mode_lock.json is unreadable: {e}") from e


def acquire_mode(
    mode: Mode,
    *,
    pid: int | None = None,
    path: Path = LOCK_PATH,
    now: float | None = None,
) -> LockState:
    """Atomically transition the lock to ``mode``.

    Allowed transitions:
      * idle → mixing
      * idle → training
      * mixing → idle  (release from same PID)
      * training → idle (release from same PID)
      * any → mode  (when the recorded PID is dead — stale reclaim)

    Forbidden:
      * mixing → training while the mixing-mode PID is still alive
      * training → mixing while the training-mode PID is still alive

    Raises ``ModeLockError`` on a forbidden transition.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode!r}")
    pid = pid if pid is not None else os.getpid()
    now = now if now is not None else time.time()

    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_lock(path)

    if current.mode == "idle":
        # Idle → anything is allowed.
        new = LockState(mode=mode, pid=pid if mode != "idle" else None, acquired_at=now)
        _atomic_write(path, new.to_json())
        log.info("Mode lock acquired: %s → %s (pid %s)", current.mode, mode, new.pid)
        return new

    # Same-process release / re-acquire.
    if current.pid == pid:
        new = LockState(mode=mode, pid=pid if mode != "idle" else None, acquired_at=now)
        _atomic_write(path, new.to_json())
        log.info("Mode lock transitioned (same pid %d): %s → %s", pid, current.mode, mode)
        return new

    # Foreign owner — only valid if their PID is dead.
    if not _is_pid_alive(current.pid):
        log.warning(
            "Reclaiming stale mode lock from dead pid %s (was %s, now %s pid %d)",
            current.pid, current.mode, mode, pid,
        )
        new = LockState(mode=mode, pid=pid if mode != "idle" else None, acquired_at=now)
        _atomic_write(path, new.to_json())
        return new

    raise ModeLockError(
        f"Cannot acquire {mode!r}: lock is held by pid {current.pid} in mode "
        f"{current.mode!r} since {current.acquired_at:.0f}"
    )


def release_mode(*, pid: int | None = None, path: Path = LOCK_PATH) -> LockState:
    """Convenience for ``acquire_mode("idle")``. Idempotent — releasing
    when already idle is a no-op."""
    pid = pid if pid is not None else os.getpid()
    current = read_lock(path)
    if current.mode == "idle":
        return current
    if current.pid is not None and current.pid != pid and _is_pid_alive(current.pid):
        raise ModeLockError(
            f"Refusing to release {current.mode!r}: held by another live pid {current.pid}"
        )
    return acquire_mode("idle", pid=pid, path=path)


def clear_lock(path: Path = LOCK_PATH) -> None:
    """Remove the lock file unconditionally. ONLY use to repair a
    corrupt lock — normal release goes through release_mode()."""
    if path.exists():
        path.unlink()


def _atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, then rename. The rename
    is atomic on POSIX, so a reader either sees the old or the new
    file — never a half-written one."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
