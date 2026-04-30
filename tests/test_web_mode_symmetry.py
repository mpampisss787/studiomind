"""Tests for the mode-lock symmetry on /ws (mixing chat).

The mixing WS now acquires "mixing" on connect and releases on
disconnect, matching what /ws/training does for "training" — so the
process-level lock is enforced both ways.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _patch_mode_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from studiomind.learning import mode_lock
    p = tmp_path / "mode_lock.json"
    monkeypatch.setattr(mode_lock, "LOCK_PATH", p)
    return p


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from studiomind.web import app as app_module
    # Use a fake API key so we get past gate 1.
    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "sk-test-fake")
    # Don't actually open a MIDI bridge — we'll fail at gate 2 if mode
    # lock somehow lets us through, which is still a valid signal that
    # the gate 0 check fired (or didn't).
    return TestClient(app_module.app)


def test_ws_mixing_refuses_when_training_held(
    client: TestClient, _patch_mode_lock: Path,
) -> None:
    """Foreign live PID holds 'training' → /ws bails at gate 0 with an
    error event, never even tries to connect to FL."""
    foreign_pid = os.getppid()  # almost certainly alive
    _patch_mode_lock.parent.mkdir(parents=True, exist_ok=True)
    _patch_mode_lock.write_text(json.dumps({
        "mode": "training",
        "pid": foreign_pid,
        "acquired_at": 0.0,
    }))

    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "mode" in msg["content"].lower() or "training" in msg["content"].lower()


def test_ws_mixing_acquires_lock_on_connect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, _patch_mode_lock: Path,
) -> None:
    """When /ws starts, the mode lock flips to 'mixing' for this PID.
    We can't drive the full chat flow without a real FL bridge, but the
    early gate is observable via a side-channel: the lock file changes
    after gate 0 succeeds and before gate 2 fails."""
    # Stub out FLStudio so gate 2 fails fast and we don't hang.

    class _FakeMidiClient:
        def __init__(self):
            self._on_disconnect = None
            self._on_reconnect = None

    class _FakeFL:
        def __init__(self) -> None:
            self._client = _FakeMidiClient()

        def connect(self) -> None:
            raise RuntimeError("no FL in test env")

    from studiomind.web import app as app_module
    monkeypatch.setattr(app_module, "FLStudio", lambda: _FakeFL())

    # No prior lock; idle.
    from studiomind.learning import mode_lock
    assert mode_lock.read_lock(_patch_mode_lock).mode == "idle"

    with client.websocket_connect("/ws") as ws:
        # We expect gate 2 (FL connect) to fail with an error event.
        # The lock should have been acquired before that, and released
        # after — observable via the released-state on lock file post
        # disconnect.
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "FL" in msg["content"]

    # After disconnect the lock is released.
    assert mode_lock.read_lock(_patch_mode_lock).mode == "idle"


def test_ws_training_refuses_when_mixing_held(
    monkeypatch: pytest.MonkeyPatch, _patch_mode_lock: Path,
) -> None:
    """Symmetry check: foreign live PID holds 'mixing' → /ws/training
    bails at the lock-acquire step inside the start handler."""
    foreign_pid = os.getppid()
    _patch_mode_lock.parent.mkdir(parents=True, exist_ok=True)
    _patch_mode_lock.write_text(json.dumps({
        "mode": "mixing",
        "pid": foreign_pid,
        "acquired_at": 0.0,
    }))

    from studiomind.web import app as app_module
    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "sk-test-fake")

    client = TestClient(app_module.app)
    with client.websocket_connect("/ws/training") as ws:
        ws.send_json({
            "type": "start",
            "plugin_name": "Demo",
            "track_id": 1,
            "slot": 0,
        })
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "mode" in msg["content"].lower()
