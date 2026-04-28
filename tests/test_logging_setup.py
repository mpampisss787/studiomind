"""Tests for the always-on file logging + debug-bundle workflow.

The logging setup is small but load-bearing — every Windows test
session will rely on the bundled log to debug remotely. These tests
guard against accidental regressions in the file-handler installation
and the bundle copy logic."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_user_logs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect ~/StudioMind/logs/ to a tmp dir for the test, and reset
    the root logger's handlers so each test starts clean."""
    from studiomind import logging_setup

    monkeypatch.setattr(logging_setup, "USER_LOGS_DIR", tmp_path / "logs")

    # Snapshot + clear any handlers that previous tests installed
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = [
        h for h in saved_handlers
        if not getattr(h, logging_setup._FILE_HANDLER_INSTALLED, False)
    ]

    yield

    # Restore (drop any new file handlers our test added)
    root.handlers = [
        h for h in root.handlers
        if not getattr(h, logging_setup._FILE_HANDLER_INSTALLED, False)
    ]
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)


def test_configure_creates_log_file(tmp_path: Path):
    from studiomind.logging_setup import configure_session_logging, USER_LOGS_DIR

    log_path = configure_session_logging()
    assert log_path is not None
    assert log_path.exists()
    assert log_path.parent == USER_LOGS_DIR


def test_configure_is_idempotent():
    from studiomind.logging_setup import configure_session_logging

    p1 = configure_session_logging()
    p2 = configure_session_logging()
    assert p1 == p2

    # Exactly one file handler should be installed.
    from studiomind.logging_setup import _FILE_HANDLER_INSTALLED
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers
        if getattr(h, _FILE_HANDLER_INSTALLED, False)
    ]
    assert len(file_handlers) == 1


def test_log_writes_actually_land_in_file():
    from studiomind.logging_setup import configure_session_logging

    log_path = configure_session_logging()
    assert log_path is not None

    logging.getLogger("studiomind.test").debug("debug-marker-7f3a")
    logging.getLogger("studiomind.test").info("info-marker-abc1")

    # Flush all file handlers so the marker reaches disk.
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            h.flush()

    text = log_path.read_text()
    assert "debug-marker-7f3a" in text
    assert "info-marker-abc1" in text


def test_console_stays_at_info_by_default():
    """Without STUDIOMIND_DEBUG, console handler stays at INFO so the
    user's terminal doesn't get flooded with DEBUG noise."""
    from studiomind.logging_setup import configure_session_logging

    # Make sure env var is unset for this test
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("STUDIOMIND_DEBUG", None)

        # Pre-install a stream handler that mimics the CLI's basicConfig
        stream = logging.StreamHandler()
        stream.setLevel(logging.NOTSET)  # would default to WARNING normally
        logging.getLogger().addHandler(stream)

        configure_session_logging()

        # The stream handler should now be at INFO
        assert stream.level == logging.INFO


def test_debug_env_var_promotes_console_to_debug():
    from studiomind.logging_setup import configure_session_logging

    with patch.dict(os.environ, {"STUDIOMIND_DEBUG": "1"}):
        stream = logging.StreamHandler()
        stream.setLevel(logging.WARNING)
        logging.getLogger().addHandler(stream)

        configure_session_logging()
        assert stream.level == logging.DEBUG


def test_bundle_logs_copies_into_dest(tmp_path: Path, monkeypatch):
    """Bundle should copy a PREVIOUS session's log, not the current process's own."""
    from studiomind import logging_setup

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(logging_setup, "USER_LOGS_DIR", log_dir)

    # Write a fake "previous session" log (not via configure_session_logging so
    # it won't be the current process's own handler path).
    prev_log = log_dir / "session-20260101-120000.log"
    prev_log.write_text("previous-session-marker\n", encoding="utf-8")

    # Install the current session's handler pointing elsewhere so it's excluded.
    logging_setup.configure_session_logging()

    dest = tmp_path / "out-debug"
    copied = logging_setup.bundle_logs(last_n=1, dest_dir=dest)

    assert len(copied) == 1
    assert copied[0].name == prev_log.name
    assert "previous-session-marker" in copied[0].read_text()


def test_bundle_logs_returns_empty_when_no_logs(tmp_path: Path, monkeypatch):
    """Calling bundle before any session has logged shouldn't crash."""
    from studiomind import logging_setup

    monkeypatch.setattr(logging_setup, "USER_LOGS_DIR", tmp_path / "no-logs")
    out = logging_setup.bundle_logs(last_n=1, dest_dir=tmp_path / "dest")
    assert out == []


def test_latest_logs_orders_by_name(tmp_path: Path, monkeypatch):
    from studiomind import logging_setup

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "session-20260101-000000.log").write_text("old\n")
    (log_dir / "session-20260601-120000.log").write_text("new\n")
    monkeypatch.setattr(logging_setup, "USER_LOGS_DIR", log_dir)

    last1 = logging_setup.latest_logs(1)
    assert len(last1) == 1
    assert last1[0].name == "session-20260601-120000.log"

    last2 = logging_setup.latest_logs(2)
    assert len(last2) == 2
    # Newest last
    assert last2[-1].name == "session-20260601-120000.log"
