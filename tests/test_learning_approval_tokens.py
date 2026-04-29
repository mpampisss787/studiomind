"""Tests for the one-shot approval-token store."""

from __future__ import annotations

import pytest

from studiomind.learning.approval_tokens import (
    ApprovalError,
    ApprovalStore,
)


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore(ttl_seconds=600.0)


# ───────────────────────────── happy path ─────────────────────────────

def test_issue_and_consume_succeeds(store: ApprovalStore) -> None:
    payload = {"path": "src/foo.py", "content": "x = 1\n"}
    token = store.issue("writes", payload)
    store.consume(token, "writes", payload)


def test_token_is_url_safe_and_long(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    # secrets.token_urlsafe(32) yields 43-char strings.
    assert len(token) >= 40
    assert " " not in token


# ───────────────────────────── replay rejection ───────────────────────

def test_token_cannot_be_consumed_twice(store: ApprovalStore) -> None:
    payload = {"k": "v"}
    token = store.issue("writes", payload)
    store.consume(token, "writes", payload)
    with pytest.raises(ApprovalError, match="already consumed"):
        store.consume(token, "writes", payload)


def test_unknown_token_rejected(store: ApprovalStore) -> None:
    with pytest.raises(ApprovalError, match="Unknown approval token"):
        store.consume("does-not-exist", "writes", {"k": "v"})


# ───────────────────────────── TTL ────────────────────────────────────

def test_expired_token_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    with pytest.raises(ApprovalError, match="expired"):
        store.consume(token, "writes", {"k": "v"}, now=2000.0)


def test_token_just_inside_ttl_succeeds() -> None:
    store = ApprovalStore(ttl_seconds=600.0)
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    # 599 seconds later — still valid.
    store.consume(token, "writes", {"k": "v"}, now=1599.0)


def test_token_exactly_at_ttl_succeeds() -> None:
    """Boundary check: now == expires_at is allowed (the > comparison
    rejects strictly later times only)."""
    store = ApprovalStore(ttl_seconds=600.0)
    token = store.issue("writes", {"k": "v"}, now=1000.0)
    store.consume(token, "writes", {"k": "v"}, now=1600.0)


# ───────────────────────────── action scoping ─────────────────────────

def test_action_mismatch_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    with pytest.raises(ApprovalError, match="action mismatch"):
        store.consume(token, "commit", {"k": "v"})


def test_invalid_action_at_issue_raises(store: ApprovalStore) -> None:
    with pytest.raises(ValueError):
        store.issue("haxx", {"k": "v"})  # type: ignore[arg-type]


# ───────────────────────────── payload binding ────────────────────────

def test_payload_hash_mismatch_rejected(store: ApprovalStore) -> None:
    token = store.issue("writes", {"path": "a.py", "content": "x = 1"})
    with pytest.raises(ApprovalError, match="payload hash mismatch"):
        store.consume(token, "writes", {"path": "a.py", "content": "x = 2"})


def test_payload_canonicalisation_is_key_order_insensitive(store: ApprovalStore) -> None:
    """Reordering the dict keys must NOT invalidate the token —
    canonical hash is computed with sort_keys=True."""
    issued_payload = {"path": "a.py", "content": "x"}
    consumed_payload = {"content": "x", "path": "a.py"}
    token = store.issue("writes", issued_payload)
    store.consume(token, "writes", consumed_payload)


# ───────────────────────────── revoke + gc ────────────────────────────

def test_revoke_drops_token(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.revoke(token)
    with pytest.raises(ApprovalError, match="Unknown approval token"):
        store.consume(token, "writes", {"k": "v"})


def test_revoke_unknown_is_noop(store: ApprovalStore) -> None:
    store.revoke("does-not-exist")  # no error


def test_gc_drops_expired_tokens(store: ApprovalStore) -> None:
    a = store.issue("writes", {"k": "v"}, now=1000.0)
    b = store.issue("writes", {"k": "w"}, now=1000.0)
    assert len(store) == 2
    removed = store.gc(now=2000.0)
    assert removed == 2
    assert len(store) == 0


def test_gc_drops_consumed_tokens(store: ApprovalStore) -> None:
    token = store.issue("writes", {"k": "v"})
    store.consume(token, "writes", {"k": "v"})
    removed = store.gc()
    assert removed == 1
    assert len(store) == 0
