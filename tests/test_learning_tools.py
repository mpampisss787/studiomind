"""Per-tool dispatch tests for learning_tools.execute_training_tool.

Each test runs against a real (FakeFL-backed) TrainingOrchestrator —
no mocks of the orchestrator. The point is to lock down:
  * tool result shapes (so the LLM sees stable JSON)
  * error surfaces (errors go back as ``{"error": "..."}`` dicts)
  * dispatch-state plumbing (CommitProposal staged between turns)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from studiomind.agent.learning_loop import TrainingOrchestrator
from studiomind.agent.learning_tools import (
    TRAINING_TOOL_NAMES,
    TRAINING_TOOL_SCHEMAS,
    TrainingDispatchState,
    execute_training_tool,
)
from studiomind.learning.approval_tokens import ApprovalStore


# ───────────────────────────── shared fakes ───────────────────────────

class FakeFL:
    PARAMS = [
        {"id": 0, "name": "Threshold", "default_value": 0.8},
        {"id": 1, "name": "Style", "default_value": 0.0},
    ]

    def __init__(self) -> None:
        self.set_calls: list[dict] = []

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        return {"params": self.PARAMS}

    def set_plugin_param(self, track_id: int, slot: int, param_id: int, value: float) -> dict:
        self.set_calls.append(dict(
            track_id=track_id, slot=slot, param_id=param_id, value=value,
        ))
        return {"ok": True, "param_id": param_id, "new_value": value, "display": str(value)}


class CannedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def request(self, prompt: str, *, expected_unit: str = "") -> str:
        if not self.responses:
            return ""
        return self.responses.pop(0)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "studiomind" / "skills").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='t'\n")
    return repo


@pytest.fixture(autouse=True)
def _patch_sandbox(monkeypatch, repo_root: Path) -> None:
    from studiomind.learning import sandbox as sb
    monkeypatch.setattr(sb, "REPO_ROOT", repo_root.resolve())
    monkeypatch.setattr(
        sb, "SKILLS_DIR",
        (repo_root / "src" / "studiomind" / "skills").resolve(),
    )


def _make_orch(repo_root: Path, tmp_path: Path, *, responses: list[str] | None = None) -> TrainingOrchestrator:
    return TrainingOrchestrator(
        fl=FakeFL(),
        repo_root=repo_root,
        plugin_name="Demo Plugin",
        skill_name="demo_plugin",
        tool_name="set_demo",
        fl_version="21.2.10",
        track_id=4,
        slot=0,
        readback_provider=CannedProvider(responses or []),
        approval_store=ApprovalStore(),
        session_path=tmp_path / "s.json",
        sleep=lambda s: None,
        dwell_s=0.0,
    )


def _state() -> TrainingDispatchState:
    return TrainingDispatchState()


# ───────────────────────────── schema sanity ──────────────────────────

def test_every_schema_has_required_fields() -> None:
    for spec in TRAINING_TOOL_SCHEMAS:
        assert "name" in spec
        assert "description" in spec
        assert "input_schema" in spec
        assert spec["input_schema"]["type"] == "object"


def test_tool_names_are_unique() -> None:
    names = [s["name"] for s in TRAINING_TOOL_SCHEMAS]
    assert len(names) == len(set(names)), f"duplicate tool name(s): {names}"


def test_tool_names_match_dispatcher_branches() -> None:
    """Every name in TRAINING_TOOL_SCHEMAS has a dispatch branch and
    nothing else does. Catches drift in either direction."""
    expected = {
        "enumerate", "classify_param", "set_param_kind_manual",
        "sweep_param", "fit_param", "validate_param",
        "codegen", "request_writes_approval", "apply_writes",
        "run_pytest", "build_commit_proposal", "request_commit_approval",
        "apply_commit", "abort",
    }
    assert TRAINING_TOOL_NAMES == expected


# ───────────────────────────── enumerate ─────────────────────────────

def test_enumerate_returns_sorted_param_list(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path)
    out = execute_training_tool(orch, "enumerate", {}, state=_state())
    assert out == {"params": [
        {"id": 0, "name": "Threshold", "default_value": 0.8},
        {"id": 1, "name": "Style", "default_value": 0.0},
    ]}


# ───────────────────────────── classify ──────────────────────────────

def test_classify_continuous_serializes(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path, responses=["-30 dB", "-15 dB"])
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    out = execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    assert out["kind"] == "continuous"
    assert out["confident"] is True
    assert out["readback_a"]["parsed"] == -30.0
    assert out["readback_b"]["parsed"] == -15.0


def test_classify_enum_serializes(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path, responses=["hard", "smooth"])
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    out = execute_training_tool(orch, "classify_param", {"param_id": 1}, state=state)
    assert out["kind"] == "enum"
    assert out["readback_a"]["parsed"] is None
    assert out["readback_b"]["parsed"] is None


# ───────────────────────────── set_param_kind_manual ─────────────────

def test_set_param_kind_manual_continuous(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    out = execute_training_tool(
        orch, "set_param_kind_manual",
        {"param_id": 0, "kind": "continuous"},
        state=state,
    )
    assert out == {"ok": True}
    assert orch.session.get_param(0).kind == "continuous"


def test_set_param_kind_manual_enum_with_values(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    out = execute_training_tool(
        orch, "set_param_kind_manual",
        {"param_id": 1, "kind": "enum", "enum_values": {"a": 0.0, "b": 1.0}},
        state=state,
    )
    assert out == {"ok": True}
    assert orch.session.get_param(1).enum_values == {"a": 0.0, "b": 1.0}


# ───────────────────────────── sweep + fit + validate ────────────────

def test_sweep_returns_per_step_samples(repo_root: Path, tmp_path: Path) -> None:
    expected = [-60, -48, -36, -24, -12, 0]
    responses = ["-30 dB", "-15 dB"] + [f"{v} dB" for v in expected]
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    out = execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    assert len(out["samples"]) == 6
    assert out["all_parsed"] is True
    assert out["samples"][0]["parsed"] == -60.0


def test_sweep_flags_unparsed_readback(repo_root: Path, tmp_path: Path) -> None:
    """If the user types something non-numeric mid-sweep, all_parsed
    surfaces False so the agent can re-ask."""
    expected = ["-60 dB", "-48 dB", "bogus", "-24 dB", "-12 dB", "0 dB"]
    responses = ["-30 dB", "-15 dB"] + expected
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    out = execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    assert out["all_parsed"] is False


def test_fit_returns_fit_dict_when_curve_recoverable(repo_root: Path, tmp_path: Path) -> None:
    expected = [-60, -48, -36, -24, -12, 0]
    responses = ["-30 dB", "-15 dB"] + [f"{v} dB" for v in expected]
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    out = execute_training_tool(orch, "fit_param", {"param_id": 0}, state=state)
    assert out["ok"] is True
    assert out["fit"]["shape"] == "linear"
    assert out["fit"]["r_squared"] >= 0.99


def test_fit_returns_actionable_failure_when_unfit(repo_root: Path, tmp_path: Path) -> None:
    """Inject noisy samples that no shape can fit at R² ≥ 0.99."""
    noisy = ["1 dB", "100 dB", "5 dB", "200 dB", "10 dB", "300 dB"]
    responses = ["-30 dB", "-15 dB"] + noisy
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    out = execute_training_tool(
        orch, "fit_param",
        {"param_id": 0, "min_r_squared": 0.999999},
        state=state,
    )
    assert out["ok"] is False
    assert out["fit"] is None
    assert "more samples" in out["reason"].lower()


def test_validate_returns_per_probe_breakdown(repo_root: Path, tmp_path: Path) -> None:
    expected = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"]
        + [f"{v} dB" for v in expected]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "fit_param", {"param_id": 0}, state=state)
    out = execute_training_tool(orch, "validate_param", {"param_id": 0}, state=state)
    assert out["passed"] is True
    assert len(out["probes"]) == 4
    for p in out["probes"]:
        assert "predicted" in p
        assert "actual" in p
        assert "ok" in p


# ───────────────────────────── codegen + writes ──────────────────────

def test_codegen_lists_files_without_writing_disk(repo_root: Path, tmp_path: Path) -> None:
    """Run a minimal pipeline (1 continuous + 1 enum) just so codegen
    has something to render. Verify codegen returns a file list and
    populates files_proposed."""
    sweep = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"]                 # classify 0
        + ["hard", "smooth"]                  # classify 1
        + [f"{v} dB" for v in sweep]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "classify_param", {"param_id": 1}, state=state)
    execute_training_tool(orch, "sweep_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "fit_param", {"param_id": 0}, state=state)
    execute_training_tool(orch, "validate_param", {"param_id": 0}, state=state)
    out = execute_training_tool(orch, "codegen", {}, state=state)
    assert "manifest.json" in out["files"]
    assert any("wrapper.py" in f for f in out["files"])
    # files_proposed is the queue — non-empty after codegen
    assert out["files_proposed"]
    # Disk untouched (writes go via apply_writes)
    skill_dir = repo_root / "src" / "studiomind" / "skills" / "demo_plugin"
    if skill_dir.exists():
        assert not (skill_dir / "wrapper.py").exists()


def test_request_writes_approval_returns_token_and_payload(repo_root: Path, tmp_path: Path) -> None:
    sweep = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"] + ["hard", "smooth"]
        + [f"{v} dB" for v in sweep]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    for name, args in [
        ("enumerate", {}), ("classify_param", {"param_id": 0}),
        ("classify_param", {"param_id": 1}),
        ("sweep_param", {"param_id": 0}),
        ("fit_param", {"param_id": 0}),
        ("validate_param", {"param_id": 0}),
        ("codegen", {}),
    ]:
        execute_training_tool(orch, name, args, state=state)

    out = execute_training_tool(orch, "request_writes_approval", {}, state=state)
    assert isinstance(out["token"], str) and len(out["token"]) > 20
    assert isinstance(out["payload"], list)
    assert all("path" in entry and "content" in entry for entry in out["payload"])


def test_apply_writes_with_valid_token_lands_files(repo_root: Path, tmp_path: Path) -> None:
    sweep = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"] + ["hard", "smooth"]
        + [f"{v} dB" for v in sweep]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    for name, args in [
        ("enumerate", {}), ("classify_param", {"param_id": 0}),
        ("classify_param", {"param_id": 1}),
        ("sweep_param", {"param_id": 0}),
        ("fit_param", {"param_id": 0}),
        ("validate_param", {"param_id": 0}),
        ("codegen", {}),
    ]:
        execute_training_tool(orch, name, args, state=state)
    approval = execute_training_tool(orch, "request_writes_approval", {}, state=state)
    # Simulate the UI's /api/training/approve POST.
    orch.approval_store.approve(
        approval["token"], "writes", orch.write_queue.to_payload(),
    )
    out = execute_training_tool(
        orch, "apply_writes", {"token": approval["token"]}, state=state,
    )
    assert out["ok"] is True
    skill_dir = repo_root / "src" / "studiomind" / "skills" / "demo_plugin"
    assert (skill_dir / "manifest.json").exists()
    assert (skill_dir / "wrapper.py").exists()


def test_apply_writes_with_bogus_token_returns_error(repo_root: Path, tmp_path: Path) -> None:
    """The orchestrator raises ApprovalError; the dispatcher's
    enclosing TrainingAgent loop catches that and converts to a
    tool_result with is_error=True. At dispatch level the error
    propagates."""
    sweep = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"] + ["hard", "smooth"]
        + [f"{v} dB" for v in sweep]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    state = _state()
    for name, args in [
        ("enumerate", {}), ("classify_param", {"param_id": 0}),
        ("classify_param", {"param_id": 1}),
        ("sweep_param", {"param_id": 0}),
        ("fit_param", {"param_id": 0}),
        ("validate_param", {"param_id": 0}),
        ("codegen", {}),
    ]:
        execute_training_tool(orch, name, args, state=state)
    execute_training_tool(orch, "request_writes_approval", {}, state=state)
    from studiomind.learning.approval_tokens import ApprovalError
    with pytest.raises(ApprovalError):
        execute_training_tool(
            orch, "apply_writes", {"token": "bogus"}, state=state,
        )


# ───────────────────────────── commit proposal staging ───────────────

def test_commit_proposal_stays_in_dispatch_state(
    repo_root: Path, tmp_path: Path,
) -> None:
    """build_commit_proposal stashes a CommitProposal that
    request_commit_approval and apply_commit later read."""
    state = _state()
    assert state.pending_commit_proposal is None
    # Walk far enough to land files on disk so build_commit_proposal
    # has paths to stage.
    sweep = [-60, -48, -36, -24, -12, 0]
    probe_pred = [60.0 * p - 60.0 for p in (0.05, 0.30, 0.55, 0.95)]
    responses = (
        ["-30 dB", "-15 dB"] + ["hard", "smooth"]
        + [f"{v} dB" for v in sweep]
        + [f"{v:.4f} dB" for v in probe_pred]
    )
    orch = _make_orch(repo_root, tmp_path, responses=responses)
    for name, args in [
        ("enumerate", {}), ("classify_param", {"param_id": 0}),
        ("classify_param", {"param_id": 1}),
        ("sweep_param", {"param_id": 0}),
        ("fit_param", {"param_id": 0}),
        ("validate_param", {"param_id": 0}),
        ("codegen", {}),
    ]:
        execute_training_tool(orch, name, args, state=state)
    approval = execute_training_tool(orch, "request_writes_approval", {}, state=state)
    orch.approval_store.approve(
        approval["token"], "writes", orch.write_queue.to_payload(),
    )
    execute_training_tool(orch, "apply_writes", {"token": approval["token"]}, state=state)

    out = execute_training_tool(orch, "build_commit_proposal", {}, state=state)
    assert "Skill-Acquired-Via" in out["trailers"]
    assert state.pending_commit_proposal is not None
    assert out["paths"]


def test_request_commit_approval_without_proposal_returns_error(
    repo_root: Path, tmp_path: Path,
) -> None:
    orch = _make_orch(repo_root, tmp_path)
    state = _state()
    out = execute_training_tool(orch, "request_commit_approval", {}, state=state)
    assert out["ok"] is False
    assert "build_commit_proposal" in out["error"]


def test_apply_commit_without_proposal_returns_error(
    repo_root: Path, tmp_path: Path,
) -> None:
    orch = _make_orch(repo_root, tmp_path)
    state = _state()
    out = execute_training_tool(orch, "apply_commit", {"token": "x"}, state=state)
    assert out["ok"] is False
    assert "build_commit_proposal" in out["error"]


# ───────────────────────────── abort + unknown ───────────────────────

def test_abort_marks_session_aborted(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path)
    state = _state()
    execute_training_tool(orch, "enumerate", {}, state=state)
    out = execute_training_tool(
        orch, "abort", {"reason": "user cancelled"}, state=state,
    )
    assert out == {"ok": True}
    assert orch.session.step == "aborted"
    assert "user cancelled" in orch.session.notes


def test_unknown_tool_returns_error(repo_root: Path, tmp_path: Path) -> None:
    orch = _make_orch(repo_root, tmp_path)
    out = execute_training_tool(orch, "haxx", {}, state=_state())
    assert "error" in out
    assert "haxx" in out["error"]
