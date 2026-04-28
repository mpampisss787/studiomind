"""
Always-on file logging + a `debug-bundle` CLI command that copies the
latest session log into the repo's ``debug/`` folder so it can be
``git push``-ed for review.

Logs land in ``~/StudioMind/logs/session-<YYYYMMDD-HHMMSS>.log`` by
default — outside the repo so they don't pollute `git status`. The
bundle command pulls the most recent into ``<repo>/debug/`` for a
clean push.

Logging policy:
  - Console (stderr): INFO and up — same as before, doesn't flood
    the user's terminal.
  - File: DEBUG and up — every tool call, every MIDI exchange,
    every classifier decision, every workspace state update.
  - ``STUDIOMIND_DEBUG=1`` env var promotes the console handler to
    DEBUG too (useful for local development, not normally needed).

The file handler is set up once per CLI invocation, when
``configure_session_logging`` is called from ``main()``. Subsequent
calls are no-ops so re-importing or running tests doesn't fork the
log into a new file.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

# User-level log dir, parallel to ~/StudioMind/projects/.
USER_LOGS_DIR = Path.home() / "StudioMind" / "logs"

# Marker so we don't double-install handlers.
_FILE_HANDLER_INSTALLED = "_studiomind_file_handler"


def configure_session_logging() -> Path | None:
    """Add a DEBUG-level file handler to the root logger and return the
    log path. Idempotent: re-calling within the same process returns the
    existing path without adding a second handler."""
    root = logging.getLogger()

    # Already installed? Return the existing path.
    for h in root.handlers:
        if getattr(h, _FILE_HANDLER_INSTALLED, False):
            return Path(getattr(h, "baseFilename", ""))

    USER_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = USER_LOGS_DIR / f"session-{ts}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    setattr(fh, _FILE_HANDLER_INSTALLED, True)

    # Root must be at DEBUG so DEBUG records reach the file handler.
    if root.level > logging.DEBUG or root.level == logging.NOTSET:
        root.setLevel(logging.DEBUG)

    # Existing console handlers: keep them at INFO unless STUDIOMIND_DEBUG is set,
    # so we don't flood the user's terminal.
    console_level = logging.DEBUG if os.environ.get("STUDIOMIND_DEBUG") else logging.INFO
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(console_level)

    root.addHandler(fh)

    # Banner so the user can see file logging is active.
    logging.getLogger("studiomind").info("File logging enabled → %s", log_path)
    return log_path


def find_repo_root() -> Path | None:
    """Locate the studiomind repo root from the package install location.

    Works for editable installs (``pip install -e .``) where the package
    lives at ``<repo>/src/studiomind/``. Returns None for non-editable
    installs where the repo isn't on disk."""
    here = Path(__file__).resolve()
    # .../src/studiomind/logging_setup.py → repo at parents[2]
    candidate = here.parents[2]
    if (candidate / "pyproject.toml").exists() and (candidate / "src" / "studiomind").exists():
        return candidate
    return None


def _current_log_path() -> Path | None:
    """Return the file path this process is currently writing to, if any."""
    for h in logging.getLogger().handlers:
        if getattr(h, _FILE_HANDLER_INSTALLED, False) and hasattr(h, "baseFilename"):
            return Path(h.baseFilename)
    return None


def latest_logs(n: int = 1) -> list[Path]:
    """Return the most recent ``n`` session logs (newest last), excluding the
    current process's own log file so ``debug-bundle`` never bundles its own
    startup-banner-only entry."""
    if not USER_LOGS_DIR.exists():
        return []
    current = _current_log_path()
    logs = [
        p for p in sorted(USER_LOGS_DIR.glob("session-*.log"))
        if current is None or p.resolve() != current.resolve()
    ]
    return logs[-n:]


def bundle_logs(last_n: int = 1, dest_dir: Path | None = None) -> list[Path]:
    """Copy the last ``n`` session logs into ``<repo>/debug/``. Returns
    the destination paths. Raises FileNotFoundError if the repo can't be
    located when no explicit ``dest_dir`` is given."""
    logs = latest_logs(last_n)
    if not logs:
        return []

    if dest_dir is None:
        repo = find_repo_root()
        if repo is None:
            raise FileNotFoundError(
                "Couldn't locate the studiomind repo. Pass dest_dir, or run "
                "from a `pip install -e .` checkout."
            )
        dest_dir = repo / "debug"

    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for src in logs:
        dst = dest_dir / src.name
        shutil.copy2(src, dst)
        copied.append(dst)
    return copied
