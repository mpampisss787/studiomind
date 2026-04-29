"""Persistent state for an in-flight plugin-acquisition session.

The training agent saves the entire acquisition wizard's state to
``~/StudioMind/state/training-session.json`` after every step. If the
browser closes, the websocket drops, FL crashes, or the user kills
StudioMind mid-acquisition, the next launch can ``load()`` the file
and offer Resume / Restart this skill / Discard.

Persistence properties (asserted by tests):

  * **JSON-canonical.** ``to_json`` uses ``sort_keys=True`` so the
    same in-memory state always serialises to the same bytes —
    helpful for diffs, content hashes, and golden tests.
  * **Atomic write.** ``save()`` writes to a tmp file in the same
    directory and renames; readers either see the old or the new
    file but never a half-written one.
  * **Forward-compatible.** Unknown keys at the top level are
    preserved on round-trip so an older StudioMind reading a newer
    state file doesn't silently drop fields.

Schema versioning is explicit: ``schema_version`` field. v1 is
defined here; future migrations live alongside the manifest
migrations in ``src/studiomind/skills/_migrations/``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION: int = 1

STATE_DIR: Path = Path.home() / "StudioMind" / "state"
SESSION_PATH: Path = STATE_DIR / "training-session.json"

# The wizard's lifecycle. Steps move forward only; every transition
# saves the session so a kill mid-step can resume cleanly.
Step = Literal[
    "init",            # session created, no plugin yet
    "enumerated",      # enumerate_plugin_params has returned
    "classifying",     # mid-classification (continuous vs enum)
    "sweeping",        # mid-six-point sweep on at least one param
    "fitting",         # all sweeps done; running curve fits
    "validating",      # post-fit four-probe validation
    "generating",      # writing skill files into the in-memory queue
    "testing",         # running pytest on the generated tests
    "committing",      # commit pending user approval
    "done",            # commit landed
    "aborted",         # user discarded, FL fault, or fit floor failure
]

VALID_STEPS: tuple[str, ...] = (
    "init", "enumerated", "classifying", "sweeping", "fitting",
    "validating", "generating", "testing", "committing", "done",
    "aborted",
)


@dataclass
class SampleRecord:
    """One (param_value, displayed_value) probe captured during a sweep."""
    param_value: float
    displayed: str            # raw FL display string (e.g. "-12.0 dB")
    displayed_value: float    # parsed numeric


@dataclass
class FitRecord:
    """A curve fit from learning.curves, persisted to the session.
    Mirrors curves.Fit but as plain types so the JSON round-trip is
    explicit."""
    shape: str                            # "linear" | "log" | "power" | ...
    params: list[float]
    r_squared: float
    rmse: float


@dataclass
class ValidationProbeRecord:
    param_value: float
    predicted: float
    actual: float | None    # None until user provides readback
    delta: float | None
    ok: bool | None         # None until tolerance check has run


@dataclass
class ParamRecord:
    """Per-param state across the wizard."""
    id: int
    name: str
    kind: str | None = None                                  # "continuous" | "enum" | None (unclassified)
    enum_values: dict[str, float] = field(default_factory=dict)
    samples: list[SampleRecord] = field(default_factory=list)
    fits_attempted: list[FitRecord] = field(default_factory=list)
    selected_fit: FitRecord | None = None
    validation_probes: list[ValidationProbeRecord] = field(default_factory=list)


@dataclass
class TrainingSession:
    """The whole wizard state — written to disk after every step."""
    schema_version: int = SCHEMA_VERSION
    session_id: str = ""                  # ISO-8601 UTC timestamp string, see new_session()
    plugin_name: str = ""
    fl_version: str = ""
    track_id: int = -1
    slot: int = -1
    step: Step = "init"
    params: list[ParamRecord] = field(default_factory=list)
    files_proposed: list[str] = field(default_factory=list)   # paths queued via propose_write
    files_written: list[str] = field(default_factory=list)    # paths actually flushed
    commit_sha: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
    notes: str = ""                                           # freeform agent notes
    extra: dict[str, Any] = field(default_factory=dict)       # forward-compat unknown fields

    # ───────────────── lifecycle ─────────────────

    @classmethod
    def new(
        cls,
        plugin_name: str,
        *,
        fl_version: str = "",
        track_id: int = -1,
        slot: int = -1,
        now: float | None = None,
    ) -> "TrainingSession":
        now = now if now is not None else time.time()
        return cls(
            session_id=time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(now)) + "Z",
            plugin_name=plugin_name,
            fl_version=fl_version,
            track_id=track_id,
            slot=slot,
            started_at=now,
            updated_at=now,
        )

    def set_step(self, step: Step, *, now: float | None = None) -> None:
        if step not in VALID_STEPS:
            raise ValueError(f"Invalid wizard step: {step!r}")
        self.step = step
        self.updated_at = now if now is not None else time.time()

    def upsert_param(self, record: ParamRecord) -> None:
        for i, p in enumerate(self.params):
            if p.id == record.id:
                self.params[i] = record
                return
        self.params.append(record)

    def get_param(self, param_id: int) -> ParamRecord | None:
        for p in self.params:
            if p.id == param_id:
                return p
        return None

    # ───────────────── (de)serialisation ─────────────────

    def to_json(self) -> str:
        """Stable JSON representation — sort_keys + UTF-8 + 2-space
        indent. Identical TrainingSession objects always emit identical
        bytes (modulo ``updated_at``, which intentionally drifts)."""
        return json.dumps(
            asdict(self),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> "TrainingSession":
        data = json.loads(text)
        # Forward-compat: allow newer files with extra top-level keys
        # to load by routing the unknowns into ``extra``. Known fields
        # we care about pass through; everything else round-trips
        # untouched on save() so a future StudioMind reading the file
        # back doesn't lose work.
        known = {
            "schema_version", "session_id", "plugin_name", "fl_version",
            "track_id", "slot", "step", "params", "files_proposed",
            "files_written", "commit_sha", "started_at", "updated_at",
            "notes", "extra",
        }
        unknown = {k: v for k, v in data.items() if k not in known}

        params = [
            ParamRecord(
                id=p["id"],
                name=p["name"],
                kind=p.get("kind"),
                enum_values=dict(p.get("enum_values", {})),
                samples=[
                    SampleRecord(**s) for s in p.get("samples", [])
                ],
                fits_attempted=[
                    FitRecord(**f) for f in p.get("fits_attempted", [])
                ],
                selected_fit=(
                    FitRecord(**p["selected_fit"])
                    if p.get("selected_fit") is not None
                    else None
                ),
                validation_probes=[
                    ValidationProbeRecord(**v) for v in p.get("validation_probes", [])
                ],
            )
            for p in data.get("params", [])
        ]

        sess = cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            session_id=data.get("session_id", ""),
            plugin_name=data.get("plugin_name", ""),
            fl_version=data.get("fl_version", ""),
            track_id=int(data.get("track_id", -1)),
            slot=int(data.get("slot", -1)),
            step=data.get("step", "init"),
            params=params,
            files_proposed=list(data.get("files_proposed", [])),
            files_written=list(data.get("files_written", [])),
            commit_sha=data.get("commit_sha"),
            started_at=float(data.get("started_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            notes=data.get("notes", ""),
            extra={**data.get("extra", {}), **unknown},
        )
        if sess.step not in VALID_STEPS:
            raise ValueError(f"Loaded session has invalid step {sess.step!r}")
        return sess

    # ───────────────── persistence ─────────────────

    def save(self, path: Path = SESSION_PATH) -> None:
        """Atomic write to ``path``. Creates parent dirs as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(path)


def load(path: Path = SESSION_PATH) -> TrainingSession | None:
    """Return the persisted session if any, else None."""
    if not path.exists():
        return None
    return TrainingSession.from_json(path.read_text())


def discard(path: Path = SESSION_PATH) -> None:
    """Remove the session file. Idempotent."""
    if path.exists():
        path.unlink()
