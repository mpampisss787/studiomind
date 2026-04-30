"""Tests for the one-shot approval-token store."""

from __future__ import annotations

import threading
import time

import pytest

from studiomind.learning.approval_tokens import (
    ApprovalError,
    ApprovalStore,
)


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore(ttl_seconds=600.0)


# ───────────────────────────── happy path ─────────────────────────────

def test_issue_approve_and_consume_succeeds(store: ApprovalStore) -> None:
    payload = {"path": "src/foo.py", "content": "x = 1\n"}
    token = store.issue("writes", payload)
    store.approve(token, "writes", payload)
    store.consume(token, "writes", payload)


def test_token_is_url_safe_and_long(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    # secrets.token_urlsafe(32) yields 43-char strings.
    assert len(token) >= 40
    assert " " not in token


# ───────────────────────────── approval gate ──────────────────────────

def test_consume_without_approve_rejected(store: ApprovalStore) -> None:
    """The headline gate: an issued-but-not-approved token cannot be
    consumed. This is the prompt-injection defence — the agent can mint
    tokens via request_*_approval but only the UI can flip the gate."""
    payload = {"k": "v"}
    token = store.issue("writes", payload)
    with pytest.raises(ApprovalError, match="not been approved"):
        store.consume(token, "writes", payload)


def test_approve_is_idempotent(store: ApprovalStore) -> None:
    payload = {"k": "v"}
    token = store.issue("writes", payload)
    store.approve(token, "writes", payload)
    store.approve(token, "writes", payload)  # no error
    store.consume(token, "writes", payload)


def test_approve_validates_action(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    with pytest.raises(ApprovalError, match="action mismatch"):
        store.approve(token, "commit", {"k": "v"})


def test_approve_validates_payload(store: ApprovalStore) -> None:
    token = store.issue("writes", {"path": "a.py", "content": "x = 1"})
    with pytest.raises(ApprovalError, match="payload hash mismatch"):
        store.approve(token, "writes", {"path": "a.py", "content": "x = 2"})


def test_approve_rejects_expired(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    with pytest.raises(ApprovalError, match="expired"):
        store.approve(token, "writes", {"k": "v"}, now=2000.0)


# ───────────────────────────── replay rejection ───────────────────────

def test_token_cannot_be_consumed_twice(store: ApprovalStore) -> None:
    payload = {"k": "v"}
    token = store.issue("writes", payload)
    store.approve(token, "writes", payload)
    store.consume(token, "writes", payload)
    with pytest.raises(ApprovalError, match="already consumed"):
        store.consume(token, "writes", payload)


def test_unknown_token_rejected(store: ApprovalStore) -> None:
    with pytest.raises(ApprovalError, match="Unknown approval token"):
        store.consume("does-not-exist", "writes", {"k": "v"})


# ───────────────────────────── TTL ────────────────────────────────────

def test_expired_token_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    store.approve(token, "writes", {"k": "v"}, now=1100.0)
    with pytest.raises(ApprovalError, match="expired"):
        store.consume(token, "writes", {"k": "v"}, now=2000.0)


def test_token_just_inside_ttl_succeeds() -> None:
    store = ApprovalStore(ttl_seconds=600.0)
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    store.approve(token, "writes", {"k": "v"}, now=1100.0)
    # 599 seconds later — still valid.
    store.consume(token, "writes", {"k": "v"}, now=1599.0)


def test_token_exactly_at_ttl_succeeds() -> None:
    """Boundary check: now == expires_at is allowed (the > comparison
    rejects strictly later times only)."""
    store = ApprovalStore(ttl_seconds=600.0)
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    store.approve(token, "writes", {"k": "v"}, now=1100.0)
    store.consume(token, "writes", {"k": "v"}, now=1600.0)


# ───────────────────────────── action scoping ─────────────────────────

def test_action_mismatch_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.approve(token, "writes", {"k": "v"})
    with pytest.raises(ApprovalError, match="action mismatch"):
        store.consume(token, "commit", {"k": "v"})


def test_invalid_action_at_issue_raises(store: ApprovalStore) -> None:
    with pytest.raises(ValueError):
        store.issue("haxx", {"k": "v"})  # type: ignore[arg-type]


# ───────────────────────────── payload binding ────────────────────────

def test_payload_hash_mismatch_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"path": "a.py", "content": "x = 1"})
    store.approve(token, "writes", {"path": "a.py", "content": "x = 1"})
    with pytest.raises(ApprovalError, match="payload hash mismatch"):
        store.consume(token, "writes", {"path": "a.py", "content": "x = 2"})


def test_payload_canonicalisation_is_key_order_insensitive(store: ApprovalStore) -> None:
    """Reordering the dict keys must NOT invalidate the token —
    canonical hash is computed with sort_keys=True."""
    issued_payload = {"path": "a.py", "content": "x"}
    consumed_payload = {"content": "x", "path": "a.py"}
    token = store.issue("writes", issued_payload)
    store.approve(token, "writes", issued_payload)
    store.consume(token, "writes", consumed_payload)


# ───────────────────────────── revoke + reject + gc ───────────────────

def test_revoke_drops_token(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.revoke(token)
    with pytest.raises(ApprovalError, match="Unknown approval token"):
        store.consume(token, "writes", {"k": "v"})


def test_revoke_unknown_is_noop(store: ApprovalStore) -> None:
    store.revoke("does-not-exist")  # no error


def test_reject_blocks_consume(store: ApprovalStore) -> None:
    """A rejected token can no longer be approved or consumed."""
    payload = {"k": "v"}
    token = store.issue("writes", payload)
    store.reject(token)
    with pytest.raises(ApprovalError, match="rejected"):
        store.approve(token, "writes", payload)
    with pytest.raises(ApprovalError, match="rejected"):
        store.consume(token, "writes", payload)


def test_reject_is_idempotent(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.reject(token)
    store.reject(token)  # no error
    store.reject("does-not-exist")  # no error


def test_gc_drops_expired_tokens(store: ApprovalStore) -> None:
    a = store.issue("writes", {"k": "v"}, now=1000.0)
    b = store.issue("writes", {"k": "w"}, now=1000.0)
    assert len(store) == 2
    removed = store.gc(now=2000.0)
    assert removed == 2
    assert len(store) == 0


def test_gc_drops_consumed_tokens(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.approve(token, "writes", {"k": "v"})
    store.consume(token, "writes", {"k": "v"})
    removed = store.gc()
    assert removed == 1
    assert len(store) == 0


# ───────────────────────────── wait_for_approval ──────────────────────

def test_wait_for_unknown_returns_unknown(store: ApprovalStore) -> None:
    assert store.wait_for_approval("nope", timeout=0.01) == "unknown"


def test_wait_for_already_approved_returns_immediately(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.approve(token, "writes", {"k": "v"})
    t0 = time.monotonic()
    assert store.wait_for_approval(token, timeout=5.0) == "approved"
    assert time.monotonic() - t0 < 0.5  # didn't actually block


def test_wait_for_approval_unblocks_on_approve(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    result_holder: list[str] = []

    def waiter() -> None:
        result_holder.append(store.wait_for_approval(token, timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)  # let the waiter actually start blocking
    store.approve(token, "writes", {"k": "v"})
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result_holder == ["approved"]


def test_wait_for_approval_unblocks_on_reject(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    result_holder: list[str] = []

    def waiter() -> None:
        result_holder.append(store.wait_for_approval(token, timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    store.reject(token)
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result_holder == ["rejected"]


def test_wait_for_approval_unblocks_on_revoke(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    result_holder: list[str] = []

    def waiter() -> None:
        result_holder.append(store.wait_for_approval(token, timeout=2.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    store.revoke(token)
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result_holder == ["rejected"]


def test_wait_for_approval_timeout(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    t0 = time.monotonic()
    result = store.wait_for_approval(token, timeout=0.1)
    elapsed = time.monotonic() - t0
    assert result == "timeout"
    assert 0.08 <= elapsed <= 1.0  # actually waited
