"""Tests for the training-mode HTTP endpoints (mode lock + approval flow).

The /ws/training websocket is exercised in test_web_training_ws.py;
this file covers the two REST flows that don't need a websocket:

  * GET/POST /api/mode  — process-level mode lock
  * POST /api/training/approve — flips the gate on the active session's
    ApprovalStore using the canonical payload re-derived server-side
  * POST /api/training/reject — wakes any agent waiter with "rejected"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from studiomind.learning.approval_tokens import ApprovalStore


# ───────────────────────────── fixtures ───────────────────────────────


@pytest.fixture
def lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect mode_lock writes to a per-test path so tests don't
    fight over the real ~/StudioMind/state/mode_lock.json."""
    from studiomind.learning import mode_lock

    p = tmp_path / "mode_lock.json"
    monkeypatch.setattr(mode_lock, "LOCK_PATH", p)
    return p


@pytest.fixture
def web_client(lock_path: Path) -> TestClient:
    from studiomind.web import app as app_module
    # Reset module-level state between tests.
    app_module._set_active_training(None)
    return TestClient(app_module.app)


# ───────────────────────────── /api/mode ──────────────────────────────


def test_mode_starts_idle(web_client: TestClient) -> None:
    r = web_client.get("/api/mode")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "idle"
    assert data["pid"] is None
    assert data["this_pid"] > 0


def test_mode_acquire_training(web_client: TestClient) -> None:
    r = web_client.post("/api/mode", json={"mode": "training"})
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "training"
    assert data["pid"] == data["this_pid"]


def test_mode_release_back_to_idle(web_client: TestClient) -> None:
    web_client.post("/api/mode", json={"mode": "training"})
    r = web_client.post("/api/mode", json={"mode": "idle"})
    assert r.status_code == 200
    assert r.json()["mode"] == "idle"


def test_mode_invalid_payload_rejected(web_client: TestClient) -> None:
    r = web_client.post("/api/mode", json={"mode": "haxx"})
    assert r.status_code == 422


def test_mode_conflict_returns_409(
    web_client: TestClient, lock_path: Path,
) -> None:
    """If the lock is held by a different (live) PID, transitioning
    is refused with 409. We simulate by writing a lock entry that
    points at our own PID and a stale value, then asking for a
    different mode — but our PID matches, so it succeeds. Conflict
    happens with a *different* PID."""
    import json
    # Use a synthetic foreign PID guaranteed not to exist (large + odd).
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_pid = 99999999
    # Just to be safe, also test against a clearly-living PID — if the
    # test-runner happens to be that PID we'd get a false negative;
    # 99999999 is well past PID_MAX on Linux, so liveness check returns False.
    lock_path.write_text(json.dumps({
        "mode": "mixing",
        "pid": foreign_pid,
        "acquired_at": 0.0,
    }))
    # The mode_lock layer sees a dead foreign PID and reclaims (not
    # 409). To force a conflict we'd need the foreign PID to be alive.
    # Use os.getppid() — the parent of the test runner — which is very
    # likely alive.
    import os
    parent_pid = os.getppid()
    lock_path.write_text(json.dumps({
        "mode": "mixing",
        "pid": parent_pid,
        "acquired_at": 0.0,
    }))
    r = web_client.post("/api/mode", json={"mode": "training"})
    assert r.status_code == 409
    assert "Cannot acquire" in r.json()["detail"]


# ───────────────────────────── /api/training/approve ──────────────────


def _install_active_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    writes_payload: list[dict[str, Any]] | None = None,
    commit_payload: dict[str, Any] | None = None,
) -> ApprovalStore:
    from studiomind.web import app as app_module
    store = ApprovalStore(ttl_seconds=600.0)
    handle = app_module.TrainingSessionHandle(
        approval_store=store,
        current_writes_payload=lambda: list(writes_payload or []),
        current_commit_payload=lambda: dict(commit_payload) if commit_payload else None,
    )
    app_module._set_active_training(handle)
    return store


def test_approve_without_active_session_returns_404(
    web_client: TestClient,
) -> None:
    r = web_client.post(
        "/api/training/approve",
        json={"token": "x" * 16, "action": "writes"},
    )
    assert r.status_code == 404


def test_approve_writes_flips_the_gate(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [{"path": "src/foo.py", "content": "x = 1\n"}]
    store = _install_active_session(monkeypatch, writes_payload=payload)
    token = store.issue("writes", payload)

    # Pre-approve: consume() refuses because the gate is closed.
    from studiomind.learning.approval_tokens import ApprovalError
    with pytest.raises(ApprovalError, match="not been approved"):
        store.consume(token, "writes", payload)

    r = web_client.post(
        "/api/training/approve",
        json={"token": token, "action": "writes"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Post-approve: consume succeeds.
    store.consume(token, "writes", payload)


def test_approve_with_unknown_token_returns_400(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_active_session(monkeypatch, writes_payload=[])
    r = web_client.post(
        "/api/training/approve",
        json={"token": "zzzzzzzzzzzzzzzz", "action": "writes"},
    )
    assert r.status_code == 400
    assert "Unknown" in r.json()["detail"]


def test_approve_with_mutated_payload_rejected(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the queue mutated between issue and approve — e.g. the
    agent added another write — the canonical payload re-derived
    server-side won't match the token's hash."""
    initial = [{"path": "a.py", "content": "x"}]
    mutated = [{"path": "a.py", "content": "x"}, {"path": "b.py", "content": "y"}]

    state = {"current": initial}

    from studiomind.web import app as app_module
    store = ApprovalStore(ttl_seconds=600.0)
    handle = app_module.TrainingSessionHandle(
        approval_store=store,
        current_writes_payload=lambda: list(state["current"]),
        current_commit_payload=lambda: None,
    )
    app_module._set_active_training(handle)

    # Token issued against initial payload …
    token = store.issue("writes", initial)
    # … but the queue grew before the user clicked Approve.
    state["current"] = mutated

    r = web_client.post(
        "/api/training/approve",
        json={"token": token, "action": "writes"},
    )
    assert r.status_code == 400
    assert "payload hash mismatch" in r.json()["detail"]


def test_approve_commit_with_no_pending_proposal_returns_400(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent must call build_commit_proposal before the UI can
    approve a commit."""
    _install_active_session(monkeypatch, commit_payload=None)

    from studiomind.web import app as app_module
    handle = app_module._get_active_training()
    assert handle is not None
    token = handle.approval_store.issue("commit", {"k": "v"})

    r = web_client.post(
        "/api/training/approve",
        json={"token": token, "action": "commit"},
    )
    assert r.status_code == 400
    assert "No commit proposal" in r.json()["detail"]


def test_approve_commit_uses_live_proposal_payload(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = {
        "skill": "demo",
        "message": "Acquire demo",
        "paths": ["src/studiomind/skills/demo/wrapper.py"],
        "trailers": {"Skill-Name": "demo"},
    }
    store = _install_active_session(monkeypatch, commit_payload=proposal)
    token = store.issue("commit", proposal)

    r = web_client.post(
        "/api/training/approve",
        json={"token": token, "action": "commit"},
    )
    assert r.status_code == 200

    # consume() now succeeds for that token + canonical proposal.
    store.consume(token, "commit", proposal)


# ───────────────────────────── /api/training/reject ──────────────────


def test_reject_without_active_session_returns_404(
    web_client: TestClient,
) -> None:
    r = web_client.post(
        "/api/training/reject", json={"token": "x" * 16},
    )
    assert r.status_code == 404


def test_reject_marks_token_rejected(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [{"path": "a.py", "content": "x"}]
    store = _install_active_session(monkeypatch, writes_payload=payload)
    token = store.issue("writes", payload)

    r = web_client.post(
        "/api/training/reject", json={"token": token},
    )
    assert r.status_code == 200

    from studiomind.learning.approval_tokens import ApprovalError
    with pytest.raises(ApprovalError, match="rejected"):
        store.approve(token, "writes", payload)


def test_reject_unknown_token_is_idempotent(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_active_session(monkeypatch, writes_payload=[])
    r = web_client.post(
        "/api/training/reject", json={"token": "y" * 16},
    )
    # Idempotent: returns 200 even when the token is unknown — the
    # store's reject() is a no-op for unknown tokens.
    assert r.status_code == 200
