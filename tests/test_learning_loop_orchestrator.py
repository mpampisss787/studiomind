"""Smoke tests for TrainingOrchestrator step methods.

End-to-end mock acquisition lives in tests/test_training_e2e.py
(P4-G). This file exercises individual step methods + the resume
helper to keep failures local."""

from __future__ import annotations

from pathlib import Path

import pytest

from studiomind.agent.learning_loop import (
    TrainingOrchestrator,
    TrainingError,
    resume_orchestrator,
)
from studiomind.learning.approval_tokens import ApprovalStore
from studiomind.learning.calibration import Readback
from studiomind.learning import session_state as ss


# ───────────────────────────── fakes ──────────────────────────────────

class FakeFL:
    def __init__(self, params_table: list[dict] | None = None) -> None:
        self.params_table = params_table or [
            {"id": 0, "name": "Threshold", "default_value": 0.8},
            {"id": 1, "name": "Style", "default_value": 0.0},
        ]
        self.set_calls: list[dict] = []

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        return {"params": self.params_table}

    def set_plugin_param(self, track_id: int, slot: int, param_id: int, value: float) -> dict:
        self.set_calls.append({
            "track_id": track_id, "slot": slot,
            "param_id": param_id, "value": value,
        })
        return {"ok": True, "param_id": param_id, "new_value": value, "display": str(value)}


class CannedProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def request(self, prompt: str, *, expected_unit: str = "", context: object = None) -> str:
        if not self._responses:
            return "0"
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _patch_sandbox(monkeypatch, tmp_path: Path) -> None:
    from studiomind.learning import sandbox as sb
    fake_repo = tmp_path / "repo"
    (fake_repo / "src" / "studiomind" / "skills").mkdir(parents=True)
    monkeypatch.setattr(sb, "REPO_ROOT", fake_repo.resolve())
    monkeypatch.setattr(
        sb, "SKILLS_DIR",
        (fake_repo / "src" / "studiomind" / "skills").resolve(),
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    return tmp_path / "repo"


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "session.json"


def _make_orch(
    fake_repo: Path,
    session_path: Path,
    *,
    responses: list[str] | None = None,
    fl: FakeFL | None = None,
) -> TrainingOrchestrator:
    return TrainingOrchestrator(
        fl=fl or FakeFL(),
        repo_root=fake_repo,
        plugin_name="Demo Plugin",
        skill_name="demo_plugin",
        tool_name="set_demo",
        fl_version="21.2.10",
        track_id=4,
        slot=0,
        readback_provider=CannedProvider(responses or []),
        approval_store=ApprovalStore(),
        session_path=session_path,
        sleep=lambda s: None,
        dwell_s=0.0,
    )


# ───────────────────────────── enumerate ──────────────────────────────

def test_enumerate_seeds_param_records(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path)
    rows = orch.enumerate()
    assert len(rows) == 2
    assert orch.session.step == "enumerated"
    assert {p.id for p in orch.session.params} == {0, 1}
    # File written
    assert session_path.exists()


def test_enumerate_idempotent(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path)
    orch.enumerate()
    # Mutate one record then re-enumerate — pre-existing kind preserved.
    rec = orch.session.get_param(0)
    rec.kind = "continuous"
    orch.session.upsert_param(rec)
    orch.enumerate()
    assert orch.session.get_param(0).kind == "continuous"


# ───────────────────────────── classify ───────────────────────────────

def test_classify_continuous_records_kind(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path, responses=["-30 dB", "-15 dB"])
    orch.enumerate()
    result = orch.classify_param(0)
    assert result.kind == "continuous"
    assert orch.session.get_param(0).kind == "continuous"


def test_classify_enum_seeds_enum_values(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path, responses=["hard", "smooth"])
    orch.enumerate()
    orch.classify_param(1)
    rec = orch.session.get_param(1)
    assert rec.kind == "enum"
    assert "hard" in rec.enum_values
    assert "smooth" in rec.enum_values


def test_classify_unknown_param_raises(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path, responses=["x", "y"])
    orch.enumerate()
    with pytest.raises(TrainingError):
        orch.classify_param(99)


def test_set_param_kind_manual_override(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path)
    orch.enumerate()
    orch.set_param_kind_manual(0, "enum", enum_values={"a": 0.0, "b": 1.0})
    rec = orch.session.get_param(0)
    assert rec.kind == "enum"
    assert rec.enum_values == {"a": 0.0, "b": 1.0}


# ───────────────────────────── sweep + fit ────────────────────────────

def test_sweep_records_samples(fake_repo: Path, session_path: Path) -> None:
    # Linear curve — 6 readbacks for the 6 sweep points.
    expected = [-60, -48, -36, -24, -12, 0]
    orch = _make_orch(
        fake_repo, session_path,
        responses=["-30 dB", "-15 dB"] + [f"{v} dB" for v in expected],
    )
    orch.enumerate()
    orch.classify_param(0)
    sweep = orch.sweep_param(0)
    assert len(sweep) == 6
    rec = orch.session.get_param(0)
    assert len(rec.samples) == 6


def test_fit_recovers_linear(fake_repo: Path, session_path: Path) -> None:
    expected = [-60, -48, -36, -24, -12, 0]
    orch = _make_orch(
        fake_repo, session_path,
        responses=["-30 dB", "-15 dB"] + [f"{v} dB" for v in expected],
    )
    orch.enumerate()
    orch.classify_param(0)
    orch.sweep_param(0)
    fit = orch.fit_param(0)
    assert fit is not None
    assert fit.shape == "linear"
    rec = orch.session.get_param(0)
    assert rec.selected_fit is not None
    assert rec.selected_fit.shape == "linear"


def test_sweep_rejects_non_continuous(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path, responses=["hard", "smooth"])
    orch.enumerate()
    orch.classify_param(1)   # → enum
    with pytest.raises(TrainingError):
        orch.sweep_param(1)


# ───────────────────────────── validate ───────────────────────────────

def test_validate_passes_on_clean_readbacks(fake_repo: Path, session_path: Path) -> None:
    expected = [-60, -48, -36, -24, -12, 0]
    # 2 classify + 6 sweep + 4 validation
    probe_points = [0.05, 0.95, 0.30, 0.70]
    probe_expected = [60.0 * p - 60.0 for p in probe_points]
    responses = (
        ["-30 dB", "-15 dB"]
        + [f"{v} dB" for v in expected]
        + [f"{v} dB" for v in probe_expected]
    )
    orch = _make_orch(fake_repo, session_path, responses=responses)
    orch.enumerate()
    orch.classify_param(0)
    orch.sweep_param(0)
    orch.fit_param(0)
    outcome = orch.validate_param(0, probe_points=probe_points)
    assert outcome.passed is True


# ───────────────────────────── resume ─────────────────────────────────

def test_resume_loads_persisted_session(fake_repo: Path, session_path: Path) -> None:
    # Run partway, persist, then resume into a fresh orchestrator.
    orch = _make_orch(fake_repo, session_path, responses=["-30 dB", "-15 dB"])
    orch.enumerate()
    orch.classify_param(0)

    # Confirm session file exists.
    assert session_path.exists()

    # Resume via the helper.
    resumed = resume_orchestrator(
        fl=FakeFL(),
        repo_root=fake_repo,
        plugin_name="Demo Plugin",
        skill_name="demo_plugin",
        tool_name="set_demo",
        fl_version="21.2.10",
        track_id=4,
        slot=0,
        readback_provider=CannedProvider([]),
        session_path=session_path,
    )
    assert resumed is not None
    assert resumed.session.step == "classifying"
    rec = resumed.session.get_param(0)
    assert rec is not None
    assert rec.kind == "continuous"


def test_resume_returns_none_with_no_session(fake_repo: Path, session_path: Path) -> None:
    out = resume_orchestrator(
        fl=FakeFL(),
        repo_root=fake_repo,
        plugin_name="X", skill_name="x", tool_name="set_x",
        fl_version="x", track_id=0, slot=0,
        readback_provider=CannedProvider([]),
        session_path=session_path,
    )
    assert out is None


def test_discard_removes_session_file(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path)
    orch.enumerate()
    assert session_path.exists()
    orch.discard()
    assert not session_path.exists()


def test_abort_marks_session_aborted(fake_repo: Path, session_path: Path) -> None:
    orch = _make_orch(fake_repo, session_path)
    orch.enumerate()
    orch.abort("user cancelled")
    assert orch.session.step == "aborted"
    assert "user cancelled" in orch.session.notes


# ───────────────────────────── Anthropic shell ────────────────────────

def test_training_agent_constructor_requires_client_or_api_key(
    fake_repo: Path, session_path: Path, monkeypatch,
) -> None:
    """Without an injected anthropic_client AND no API key, the
    constructor must raise so the user gets a clear message instead
    of a confused failure mid-run."""
    from studiomind.agent.learning_loop import TrainingAgent
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(
        "studiomind.config.get_anthropic_key",
        lambda: None,
    )
    orch = _make_orch(fake_repo, session_path)
    with pytest.raises(RuntimeError, match="No Anthropic API key"):
        TrainingAgent(orch)


def test_training_agent_accepts_injected_client(
    fake_repo: Path, session_path: Path,
) -> None:
    """A mock client lets tests drive the loop without an API key.
    Constructor stores the orchestrator unchanged."""
    from studiomind.agent.learning_loop import TrainingAgent
    orch = _make_orch(fake_repo, session_path)

    class _MockClient:  # noqa: D401 — minimal stub
        messages = None  # not used when run() isn't called

    agent = TrainingAgent(orch, anthropic_client=_MockClient())
    assert agent.orchestrator is orch
    assert agent.last_text_response == ""
