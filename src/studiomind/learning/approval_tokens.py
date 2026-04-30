"""One-shot approval tokens for training-mode write/commit gates.

The training agent never touches disk or git directly — it calls
``propose_write`` / ``propose_commit`` which return a server-issued
nonce. The user clicks Approve in the UI, the UI POSTs the nonce to
``/api/training/approve``, the backend marks the token approved, the
agent's apply call then ``consume()``s it.

This module is the in-memory token store. Properties (asserted by
tests):

  * **Approval gate.** ``consume()`` refuses tokens that haven't been
    explicitly ``approve()``-d by the UI. The agent cannot consume a
    token it issued itself — only the approve endpoint can flip the
    gate. This is the prompt-injection defence the design doc spells
    out (training-mode.md § "Approval tokens").
  * **One-shot.** Tokens cannot be consumed twice. Replay rejected.
  * **TTL.** Tokens expire after ``DEFAULT_TTL_SECONDS`` (10 min).
  * **Payload-bound.** Each token is tied to a hash of the payload
    that was previewed to the user. Calling ``approve(token, action,
    payload)`` or ``consume(...)`` with a different payload than was
    issued is rejected — prevents an attacker (or a confused agent)
    from approving one diff and substituting another at apply time.
  * **Action-scoped.** A token issued for ``"writes"`` cannot be
    approved or consumed for ``"commit"``, and vice versa.

The store is process-local: tokens live in memory and don't persist
across restarts. That's intentional — a restart should void any
in-flight approval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

Action = Literal["writes", "commit"]
VALID_ACTIONS: tuple[str, ...] = ("writes", "commit")

DEFAULT_TTL_SECONDS: float = 600.0  # 10 min, per design doc


class ApprovalError(Exception):
    """Raised when token validation fails."""


def _payload_hash(payload: Any) -> str:
    """Stable SHA-256 of a JSON-canonicalised payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _TokenRecord:
    token: str
    action: Action
    payload_hash: str
    issued_at: float
    expires_at: float
    consumed: bool = False
    consumed_at: float | None = None
    approved: bool = False
    approved_at: float | None = None
    rejected: bool = False
    rejected_at: float | None = None
    # Threading event fired when approve() or reject()/revoke() lands —
    # so wait_for_approval() can block the agent thread until the UI
    # responds. The Event lives on the record so the store doesn't need
    # to track waiters separately.
    event: threading.Event = field(default_factory=threading.Event)


class ApprovalStore:
    """Thread-safe in-memory token store. One per training session."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = float(ttl_seconds)
        self._records: dict[str, _TokenRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        action: Action,
        payload: Any,
        *,
        now: float | None = None,
    ) -> str:
        """Mint a fresh one-shot token for ``action`` bound to a hash
        of ``payload``. Returns the token string. ``payload`` should
        be the exact dict the UI is about to render to the user — its
        canonical hash is recomputed at consume() time and rejected on
        mismatch."""
        if action not in VALID_ACTIONS:
            raise ValueError(f"Invalid approval action: {action!r}")
        now = now if now is not None else time.time()
        token = secrets.token_urlsafe(32)
        record = _TokenRecord(
            token=token,
            action=action,
            payload_hash=_payload_hash(payload),
            issued_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._records[token] = record
        log.debug("Approval token issued: action=%s expires=%.0f", action, record.expires_at)
        return token

    def _validate_locked(
        self,
        token: str,
        action: Action,
        payload: Any,
        *,
        now: float,
        verb: str,
    ) -> _TokenRecord:
        """Run the common token-validity checks (existence, not
        consumed, not expired, not rejected, action match, payload
        hash). Returns the record on success; raises ``ApprovalError``
        otherwise. Caller must hold ``self._lock``."""
        record = self._records.get(token)
        if record is None:
            raise ApprovalError("Unknown approval token")
        if record.consumed:
            raise ApprovalError(
                f"Approval token already consumed at {record.consumed_at:.0f}"
            )
        if record.rejected:
            raise ApprovalError(
                f"Approval token was rejected at {record.rejected_at:.0f}"
            )
        if now > record.expires_at:
            raise ApprovalError(
                f"Approval token expired at {record.expires_at:.0f} (now {now:.0f})"
            )
        if record.action != action:
            raise ApprovalError(
                f"Token action mismatch: issued for {record.action!r}, "
                f"{verb} for {action!r}"
            )
        actual_hash = _payload_hash(payload)
        if record.payload_hash != actual_hash:
            raise ApprovalError(
                "Approval token payload hash mismatch — UI preview did not "
                f"match the {verb}-time payload"
            )
        return record

    def approve(
        self,
        token: str,
        action: Action,
        payload: Any,
        *,
        now: float | None = None,
    ) -> None:
        """Mark a token as approved by the UI. After this returns,
        ``consume()`` for the same token will succeed (until expiry
        / consumption). Re-approving an already-approved token is a
        no-op. Raises ``ApprovalError`` on validation failure (just
        like ``consume()``)."""
        now = now if now is not None else time.time()
        with self._lock:
            record = self._validate_locked(
                token, action, payload, now=now, verb="approved",
            )
            if record.approved:
                # Idempotent — re-approving is fine.
                return
            record.approved = True
            record.approved_at = now
            record.event.set()
        log.debug("Approval token approved: action=%s", action)

    def reject(self, token: str, *, now: float | None = None) -> None:
        """Mark a token as rejected by the UI. Wakes any
        ``wait_for_approval()`` caller; subsequent ``consume()`` will
        fail. Idempotent: rejecting unknown / already-rejected /
        already-consumed tokens is a no-op."""
        now = now if now is not None else time.time()
        with self._lock:
            record = self._records.get(token)
            if record is None or record.consumed or record.rejected:
                return
            record.rejected = True
            record.rejected_at = now
            record.event.set()
        log.debug("Approval token rejected")

    def wait_for_approval(
        self,
        token: str,
        *,
        timeout: float | None = None,
    ) -> str:
        """Block until the token is approved, rejected, or expired.

        Returns one of:
          * ``"approved"`` — UI called ``approve()``
          * ``"rejected"`` — UI called ``reject()`` (or ``revoke()``)
          * ``"expired"`` — TTL elapsed without a decision
          * ``"consumed"`` — already-applied (idempotent caller)
          * ``"unknown"`` — token not found
          * ``"timeout"`` — waited ``timeout`` seconds, no decision

        Safe to call from a worker thread; the underlying
        :class:`threading.Event` is thread-aware.
        """
        with self._lock:
            record = self._records.get(token)
            if record is None:
                return "unknown"
            if record.consumed:
                return "consumed"
            if record.approved:
                return "approved"
            if record.rejected:
                return "rejected"
            if time.time() > record.expires_at:
                return "expired"
            event = record.event
            # Compute a per-token cap so we never out-wait the TTL even
            # if the caller's timeout is None or larger.
            ttl_remaining = max(0.0, record.expires_at - time.time())

        wait_for = ttl_remaining if timeout is None else min(timeout, ttl_remaining)
        fired = event.wait(timeout=wait_for if wait_for > 0 else 0.0)

        with self._lock:
            record = self._records.get(token)
            if record is None:
                return "rejected"  # revoked while we were waiting
            if record.approved:
                return "approved"
            if record.rejected:
                return "rejected"
            if time.time() > record.expires_at:
                return "expired"
        return "timeout" if not fired else "unknown"

    def consume(
        self,
        token: str,
        action: Action,
        payload: Any,
        *,
        now: float | None = None,
    ) -> None:
        """Validate the token for ``action`` + ``payload`` and mark it
        consumed. Raises ``ApprovalError`` on any rejection.

        Requires ``approve()`` to have been called first — consuming
        an un-approved token is the prompt-injection vector this gate
        defends against. Tests that don't need the approval gate can
        bypass via ``approve_and_consume()``."""
        now = now if now is not None else time.time()
        with self._lock:
            record = self._validate_locked(
                token, action, payload, now=now, verb="consumed",
            )
            if not record.approved:
                raise ApprovalError(
                    "Approval token has not been approved by the UI yet — "
                    "the user must click Approve before this can apply"
                )
            record.consumed = True
            record.consumed_at = now
        log.debug("Approval token consumed: action=%s", action)

    def revoke(self, token: str) -> None:
        """Drop a token without consuming it. Wakes any pending
        ``wait_for_approval()`` waiters with a ``"rejected"`` result.
        Idempotent: revoking an unknown token is a no-op."""
        with self._lock:
            record = self._records.pop(token, None)
        if record is not None:
            record.event.set()

    def gc(self, *, now: float | None = None) -> int:
        """Drop expired or already-consumed tokens. Returns the number
        of records removed. Safe to call periodically; not required
        for correctness because consume() rejects expired/consumed
        records anyway."""
        now = now if now is not None else time.time()
        with self._lock:
            stale = [
                t for t, r in self._records.items()
                if r.consumed or now > r.expires_at
            ]
            for t in stale:
                self._records.pop(t)
        return len(stale)

    # Test helpers — not for production use.
    def _record(self, token: str) -> _TokenRecord | None:
        return self._records.get(token)

    def __len__(self) -> int:
        return len(self._records)
