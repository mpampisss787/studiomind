"""Tests for the TrainingSession persistence layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from studiomind.learning import session_state as ss
from studiomind.learning.session_state import (
    FitRecord,
    ParamRecord,
    SampleRecord,
    TrainingSession,
    ValidationProbeRecord,
)


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "training-session.json"


# ───────────────────────────── new + lifecycle ────────────────────────

def test_new_session_seeds_session_id_and_timestamps() -> None:
    s = TrainingSession.new("Fruity Limiter", fl_version="21.2.10", now=1700000000.0)
    assert s.plugin_name == "Fruity Limiter"
    assert s.fl_version == "21.2.10"
    assert s.session_id.endswith("Z")          # ISO UTC suffix
    assert s.started_at == 1700000000.0
    assert s.updated_at == 1700000000.0
    assert s.step == "init"


def test_set_step_updates_timestamp() -> None:
    s = TrainingSession.new("X", now=1000.0)
    s.set_step("enumerated", now=1100.0)
    assert s.step == "enumerated"
    assert s.updated_at == 1100.0


def test_set_step_rejects_invalid_step() -> None:
    s = TrainingSession.new("X")
    with pytest.raises(ValueError):
        s.set_step("haxx")  # type: ignore[arg-type]


def test_upsert_param_inserts_then_replaces() -> None:
    s = TrainingSession.new("X")
    p = ParamRecord(id=0, name="Threshold")
    s.upsert_param(p)
    assert len(s.params) == 1
    s.upsert_param(ParamRecord(id=0, name="Threshold", kind="continuous"))
    assert len(s.params) == 1
    assert s.params[0].kind == "continuous"


# ───────────────────────────── round-trip ─────────────────────────────

def test_round_trip_preserves_full_state() -> None:
    s = TrainingSession.new("Fruity Limiter", fl_version="21.2.10",
                            track_id=5, slot=0, now=1000.0)
    s.set_step("sweeping", now=1100.0)
    s.upsert_param(ParamRecord(
        id=0,
        name="Ceiling",
        kind="continuous",
        samples=[
            SampleRecord(param_value=0.0, displayed="-60.0 dB", displayed_value=-60.0),
            SampleRecord(param_value=1.0, displayed="0.0 dB", displayed_value=0.0),
        ],
        fits_attempted=[
            FitRecord(shape="linear", params=[60.0, -60.0], r_squared=1.0, rmse=0.0),
        ],
        selected_fit=FitRecord(shape="linear", params=[60.0, -60.0], r_squared=1.0, rmse=0.0),
        validation_probes=[
            ValidationProbeRecord(param_value=0.5, predicted=-30.0, actual=-30.0, delta=0.0, ok=True),
        ],
    ))
    s.upsert_param(ParamRecord(
        id=1, name="Style", kind="enum",
        enum_values={"hard": 0.0, "smooth": 0.5, "transparent": 1.0},
    ))
    s.files_proposed = ["src/studiomind/skills/fruity_limiter/wrapper.py"]
    s.notes = "Mid-sweep when the user paused."

    parsed = TrainingSession.from_json(s.to_json())
    assert parsed.plugin_name == "Fruity Limiter"
    assert parsed.fl_version == "21.2.10"
    assert parsed.step == "sweeping"
    assert parsed.track_id == 5
    assert parsed.slot == 0
    assert parsed.notes == "Mid-sweep when the user paused."
    assert parsed.files_proposed == ["src/studiomind/skills/fruity_limiter/wrapper.py"]
    assert len(parsed.params) == 2
    p0, p1 = parsed.params
    assert p0.kind == "continuous"
    assert len(p0.samples) == 2
    assert p0.samples[0].displayed_value == -60.0
    assert p0.selected_fit is not None
    assert p0.selected_fit.shape == "linear"
    assert p0.validation_probes[0].ok is True
    assert p1.kind == "enum"
    assert p1.enum_values == {"hard": 0.0, "smooth": 0.5, "transparent": 1.0}


def test_to_json_is_stable() -> None:
    """Same in-memory session twice → same bytes (modulo updated_at,
    which we hold constant here)."""
    a = TrainingSession.new("X", now=1000.0)
    b = TrainingSession.new("X", now=1000.0)
    # session_id is timestamp-derived so they match.
    assert a.to_json() == b.to_json()


def test_invalid_step_in_persisted_file_raises() -> None:
    bad = '{"step": "haxx", "schema_version": 1}'
    with pytest.raises(ValueError):
        TrainingSession.from_json(bad)


# ───────────────────────────── persistence (save/load) ────────────────

def test_save_then_load(session_path: Path) -> None:
    s = TrainingSession.new("Fruity Limiter", now=1000.0)
    s.set_step("enumerated", now=1100.0)
    s.save(session_path)
    assert session_path.exists()

    loaded = ss.load(session_path)
    assert loaded is not None
    assert loaded.plugin_name == "Fruity Limiter"
    assert loaded.step == "enumerated"


def test_load_returns_none_when_no_file(session_path: Path) -> None:
    assert ss.load(session_path) is None


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "deeper" / "session.json"
    s = TrainingSession.new("X")
    s.save(nested)
    assert nested.exists()


def test_save_is_atomic_writer_creates_no_partial_file(session_path: Path, monkeypatch) -> None:
    """If write_text raises mid-write on the *target*, there should be
    no half-written target file. Atomicity is achieved via tmp+replace,
    so the failure has to fire on the tmp file before replace().

    We simulate by patching Path.replace to raise — the tmp file
    exists but the target should NOT.
    """
    s = TrainingSession.new("X")

    real_replace = Path.replace
    def boom(self, target):  # noqa: ANN001
        raise OSError("simulated failure during atomic replace")
    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError):
        s.save(session_path)

    assert not session_path.exists(), "target must not exist after a failed atomic write"
    monkeypatch.setattr(Path, "replace", real_replace)


def test_discard_removes_file(session_path: Path) -> None:
    s = TrainingSession.new("X")
    s.save(session_path)
    assert session_path.exists()
    ss.discard(session_path)
    assert not session_path.exists()


def test_discard_when_missing_is_noop(session_path: Path) -> None:
    ss.discard(session_path)  # should not raise


# ───────────────────────────── forward compat ────────────────────────

def test_unknown_top_level_keys_round_trip() -> None:
    """A future StudioMind may add fields. The loader must preserve
    them via ``extra`` so save() doesn't drop them silently."""
    raw = """{
      "schema_version": 1,
      "step": "init",
      "future_field": {"some": "thing"},
      "another_one": 42
    }"""
    s = TrainingSession.from_json(raw)
    assert s.extra["future_field"] == {"some": "thing"}
    assert s.extra["another_one"] == 42

    # Save round-trip — the unknown keys should still be in the JSON.
    saved = s.to_json()
    parsed_again = TrainingSession.from_json(saved)
    assert parsed_again.extra["future_field"] == {"some": "thing"}
    assert parsed_again.extra["another_one"] == 42
