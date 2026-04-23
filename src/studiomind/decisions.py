"""
Decision log: per-project record of destructive tool calls and their outcomes.

`.studiomind/decisions.json` lives beside the session manifest. Every destructive
write the agent makes appends a decision with outcome="pending". If the user
`revert`s, the most recent pending decision is marked "reverted". Decisions from
prior sessions that are still "pending" when a new session opens are assumed to
have been kept (the user closed their DAW without reverting).

The agent reads the log at session start so it can see patterns — "user reverted
3/3 of my +3dB shelf boosts last session; be more conservative this time."
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pending decisions older than this are assumed "kept" — the user had the
# opportunity to revert and didn't. Chosen liberally: most sessions finish
# within an hour, and a decision that has survived 6 hours of real-world use
# is a real kept decision, not an unresolved one.
PENDING_STALE_AFTER_S = 6 * 3600

OUTCOME_PENDING = "pending"
OUTCOME_KEPT = "kept"
OUTCOME_REVERTED = "reverted"


@dataclass
class Decision:
    id: str
    timestamp: float
    tool: str
    track_id: int | None
    track_name: str | None
    params: dict[str, Any]
    description: str
    expected_delta: str | None = None
    outcome: str = OUTCOME_PENDING

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Decision:
        return cls(**d)


@dataclass
class DecisionsLog:
    """
    File-backed append-only log of agent decisions for one project.

    Not thread-safe on its own — callers (Project) hold a lock during
    append/mark to serialize access within a process.
    """
    path: Path
    decisions: list[Decision] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> DecisionsLog:
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            decisions = [Decision.from_dict(d) for d in raw.get("decisions", [])]
            return cls(path=path, decisions=decisions)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # Corrupt file: don't lose future writes, but don't crash either.
            logger.warning("Could not parse %s: %s — starting fresh", path, e)
            return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"decisions": [d.to_dict() for d in self.decisions]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def append(
        self,
        tool: str,
        params: dict[str, Any],
        description: str,
        track_id: int | None = None,
        track_name: str | None = None,
        expected_delta: str | None = None,
    ) -> Decision:
        dec = Decision(
            id=f"dec_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            timestamp=time.time(),
            tool=tool,
            track_id=track_id,
            track_name=track_name,
            params=params,
            description=description,
            expected_delta=expected_delta,
        )
        self.decisions.append(dec)
        self.save()
        return dec

    def mark_last_reverted(self) -> Decision | None:
        """Mark the most recent pending decision as reverted. Returns it, or None."""
        for dec in reversed(self.decisions):
            if dec.outcome == OUTCOME_PENDING:
                dec.outcome = OUTCOME_REVERTED
                self.save()
                return dec
        return None

    def age_pending(self, now: float | None = None) -> int:
        """
        Convert old pending decisions to "kept". Called at session start.

        A decision is "kept" if it's been pending longer than PENDING_STALE_AFTER_S —
        the user had a chance to revert it and didn't. Returns the count aged.
        """
        cutoff = (now or time.time()) - PENDING_STALE_AFTER_S
        aged = 0
        for dec in self.decisions:
            if dec.outcome == OUTCOME_PENDING and dec.timestamp < cutoff:
                dec.outcome = OUTCOME_KEPT
                aged += 1
        if aged:
            self.save()
        return aged

    def recent(self, limit: int = 20) -> list[Decision]:
        return list(self.decisions[-limit:])

    def summary_counts(self) -> dict[str, int]:
        """Count outcomes across the whole log — useful for the agent's orient step."""
        counts = {OUTCOME_PENDING: 0, OUTCOME_KEPT: 0, OUTCOME_REVERTED: 0}
        for d in self.decisions:
            counts[d.outcome] = counts.get(d.outcome, 0) + 1
        return counts
