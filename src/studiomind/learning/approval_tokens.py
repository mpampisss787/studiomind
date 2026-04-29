"""One-shot approval tokens for training-mode write/commit gates.

The training agent never touches disk or git directly — it calls
``propose_write`` / ``propose_commit`` which return a server-issued
nonce. The user clicks Approve in the UI, the UI POSTs the nonce to
``/api/training/approve``, the backend validates and consumes the
token exactly once, and only then does the actual write/commit fire.

This module is the in-memory token store. Properties (asserted by
tests):

  * **One-shot.** Tokens cannot be consumed twice. Replay rejected.
  * **TTL.** Tokens expire after ``DEFAULT_TTL_SECONDS`` (10 min).
  * **Payload-bound.** Each token is tied to a hash of the payload
    that was previewed to the user. Calling ``consume(token, action,
    payload)`` with a different payload than was issued is rejected
    — prevents an attacker (or a confused agent) from approving one
    diff and substituting another at apply time.
  * **Action-scoped.** A token issued for ``"writes"`` cannot be
    consumed for ``"commit"``, and vice versa.

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

    def consume(
        self,
        token: str,
        action: Action,
        payload: Any,
        *,
        now: float | None = None,
    ) -> None:
        """Validate the token for ``action`` + ``payload`` and mark it
        consumed. Raises ``ApprovalError`` on any rejection. Tokens
        cannot be consumed twice — the second call always fails."""
        now = now if now is not None else time.time()
        with self._lock:
            record = self._records.get(token)
            if record is None:
                raise ApprovalError("Unknown approval token")
            if record.consumed:
                raise ApprovalError(
                    f"Approval token already consumed at {record.consumed_at:.0f}"
                )
            if now > record.expires_at:
                raise ApprovalError(
                    f"Approval token expired at {record.expires_at:.0f} (now {now:.0f})"
                )
            if record.action != action:
                raise ApprovalError(
                    f"Token action mismatch: issued for {record.action!r}, "
                    f"consumed for {action!r}"
                )
            actual_hash = _payload_hash(payload)
            if record.payload_hash != actual_hash:
                raise ApprovalError(
                    "Approval token payload hash mismatch — UI preview did not "
                    "match the apply-time payload"
                )
            record.consumed = True
            record.consumed_at = now
        log.debug("Approval token consumed: action=%s", action)

    def revoke(self, token: str) -> None:
        """Drop a token without consuming it (e.g. when the user
        clicks Reject). Idempotent: revoking an unknown token is a
        no-op."""
        with self._lock:
            self._records.pop(token, None)

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
