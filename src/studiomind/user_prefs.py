"""
Global (cross-project) user preferences.

Lives at `~/StudioMind/user.json`. Unlike `notes.md` (per-project) or
`decisions.json` (per-project, per-action), user.json holds durable facts about
the *user's taste and working style* that apply across every project: "never
boost above 10kHz", "prefers bell Q around 1.5 for surgical cuts", "always
wants master headroom at -1dBTP".

The agent reads these at session start and writes to them when it observes
a strong, persistent preference signal (user confirms an unusual choice, or
reverts the same type of move multiple times).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

USER_PREFS_PATH = Path.home() / "StudioMind" / "user.json"

SOURCE_EXPLICIT = "user_explicit"      # User stated it directly ("I never...")
SOURCE_DERIVED = "derived"             # Computed from patterns in decisions.json
SOURCE_OBSERVATION = "agent_observation"  # Agent inferred from one exchange


@dataclass
class Preference:
    id: str
    statement: str
    source: str
    first_seen: float
    last_confirmed: float
    # Loose confidence 0.0–1.0. Explicit statements start at 1.0; agent
    # observations start at ~0.4 and grow when re-confirmed.
    strength: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Preference:
        return cls(**d)


@dataclass
class UserPreferences:
    path: Path
    preferences: list[Preference] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> UserPreferences:
        path = path or USER_PREFS_PATH
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            prefs = [Preference.from_dict(p) for p in raw.get("preferences", [])]
            return cls(path=path, preferences=prefs)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Could not parse %s: %s — starting fresh", path, e)
            return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"preferences": [p.to_dict() for p in self.preferences]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def record(
        self,
        statement: str,
        source: str = SOURCE_OBSERVATION,
        strength: float | None = None,
    ) -> Preference:
        """
        Add or reinforce a preference.

        If a near-identical statement already exists (case-insensitive substring
        match either direction), we bump `last_confirmed` and `strength` instead
        of duplicating. Callers can always force a new entry by rewording.
        """
        statement = statement.strip()
        if not statement:
            raise ValueError("preference statement must be non-empty")

        now = time.time()
        default_strength = 1.0 if source == SOURCE_EXPLICIT else 0.4
        strength = strength if strength is not None else default_strength

        existing = self._find_similar(statement)
        if existing is not None:
            existing.last_confirmed = now
            # Re-confirmation bumps strength but asymptotes near 1.0.
            existing.strength = min(1.0, existing.strength + 0.15)
            # Explicit restatement upgrades the source.
            if source == SOURCE_EXPLICIT:
                existing.source = SOURCE_EXPLICIT
            self.save()
            return existing

        pref = Preference(
            id=f"pref_{int(now)}_{uuid.uuid4().hex[:6]}",
            statement=statement,
            source=source,
            first_seen=now,
            last_confirmed=now,
            strength=strength,
        )
        self.preferences.append(pref)
        self.save()
        return pref

    def _find_similar(self, statement: str) -> Preference | None:
        needle = statement.lower()
        for p in self.preferences:
            hay = p.statement.lower()
            if needle == hay:
                return p
            # Substring in either direction catches "never boost above 10kHz"
            # vs "don't boost above 10kHz" via shared phrase. Intentionally loose.
            if len(needle) > 12 and (needle in hay or hay in needle):
                return p
        return None

    def remove(self, pref_id: str) -> bool:
        before = len(self.preferences)
        self.preferences = [p for p in self.preferences if p.id != pref_id]
        changed = len(self.preferences) < before
        if changed:
            self.save()
        return changed

    def sorted_for_agent(self) -> list[Preference]:
        """Strongest first — the agent's attention is scarce."""
        return sorted(self.preferences, key=lambda p: (-p.strength, -p.last_confirmed))
