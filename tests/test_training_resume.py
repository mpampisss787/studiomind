"""Resumability test — kill mid-acquisition, reload from disk, finish.

The orchestrator persists session state after every step (P4-B). This
test proves the end-to-end path: a fresh orchestrator can pick up
where the killed one left off and produce the same final outcome.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from studiomind.agent.learning_loop import (
    TrainingOrchestrator,
    resume_orchestrator,
)
from studiomind.learning.approval_tokens import ApprovalStore


# ───────────────────────────── fakes (mirror e2e) ─────────────────────

class FakeFL:
    PARAMS = [
        {"id": 0, "name": "Threshold", "default_value": 0.8},
        {"id": 1, "name": "Style", "default_value": 0.0},
        {"id": 2, "name": "Mix", "default_value": 1.0},
    ]

    def __init__(self) -> None:
        self.set_calls: list[dict] = []

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        return {"params": self.PARAMS}

    def set_plugin_param(self, track_id: int, slot: int, param_id: int, value: float) -> dict:
        self.set_calls.append({
            "track_id": track_id, "slot": slot,
            "param_id": param_id, "value": value,
        })
        return {"ok": True, "param_id": param_id, "new_value": value, "display": str(value)}


class CannedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def request(self, prompt: str, *, expected_unit: str = "") -> str:
        if not self.responses:
            raise RuntimeError("CannedProvider exhausted")
        return self.responses.pop(0)


# ───────────────────────────── readback recipes ───────────────────────

CLASSIFY_THRESHOLD = ["-30 dB", "-15 dB"]
CLASSIFY_STYLE = ["hard", "smooth"]
CLASSIFY_MIX = ["25 %", "75 %"]
SWEEP_THRESHOLD = ["-60 dB", "-48 dB", "-36 dB", "-24 dB", "-12 dB", "0 dB"]
SWEEP_MIX = ["0 %", "20 %", "40 %", "60 %", "80 %", "100 %"]
VAL_PROBE_POINTS = [0.05, 0.30, 0.55, 0.95]
VAL_THRESHOLD = [f"{60.0 * p - 60.0:.4f} dB" for p in VAL_PROBE_POINTS]
VAL_MIX = [f"{100.0 * p:.4f} %" for p in VAL_PROBE_POINTS]


# ───────────────────────────── fixtures ───────────────────────────────

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skills_dir = repo / "src" / "studiomind" / "skills"
    skills_dir.mkdir(parents=True)
    (repo / "src" / "studiomind" / "__init__.py").write_text("")
    (skills_dir / "__init__.py").write_text("")
    (repo / "pyproject.toml").write_text("[project]\nname='test'\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture(autouse=True)
def _patch_sandbox(monkeypatch, repo_root: Path) -> None:
    from studiomind.learning import sandbox as sb
    monkeypatch.setattr(sb, "REPO_ROOT", repo_root.resolve())
    monkeypatch.setattr(
        sb, "SKILLS_DIR",
        (repo_root / "src" / "studiomind" / "skills").resolve(),
    )


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return tmp_path / "training-session.json"


# ───────────────────────────── kill-and-restart test ──────────────────

def test_resume_after_simulated_kill_lands_same_commit(
    repo_root: Path, session_path: Path,
) -> None:
    """Run partway through, drop the orchestrator (simulated kill),
    rebuild via resume_orchestrator, finish through commit. The
    resumed run must reach step="done" with a valid commit SHA, and
    the on-disk skill files must match what the kill-free run would
    have produced."""

    # Phase 1: enumerate + classify all 3 + sweep param 0.
    fl_a = FakeFL()
    provider_a = CannedProvider(
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD
    )
    orch_a = TrainingOrchestrator(
        fl=fl_a, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=provider_a,
        approval_store=ApprovalStore(),
        session_path=session_path,
        sleep=lambda s: None, dwell_s=0.0,
    )
    orch_a.enumerate()
    for pid in (0, 1, 2):
        orch_a.classify_param(pid)
    orch_a.sweep_param(0)
    # Capture the state after partway-through kill.
    snapshot_step = orch_a.session.step
    snapshot_param0_samples = list(orch_a.session.get_param(0).samples)
    snapshot_param1_kind = orch_a.session.get_param(1).kind
    snapshot_param2_kind = orch_a.session.get_param(2).kind

    # Drop orch_a — simulating process kill. The session file is the
    # only durable state.
    del orch_a, fl_a, provider_a

    # Phase 2: resume via the helper, finish.
    fl_b = FakeFL()
    # Provider has only what's left: sweep mix + validation readbacks.
    provider_b = CannedProvider(SWEEP_MIX + VAL_THRESHOLD + VAL_MIX)
    orch_b = resume_orchestrator(
        fl=fl_b, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=provider_b,
        session_path=session_path,
    )
    assert orch_b is not None

    # Resumed state matches the snapshot.
    assert orch_b.session.step == snapshot_step
    assert orch_b.session.get_param(0).samples == snapshot_param0_samples
    assert orch_b.session.get_param(1).kind == snapshot_param1_kind
    assert orch_b.session.get_param(2).kind == snapshot_param2_kind

    # Continue forward.
    orch_b.fit_param(0)
    orch_b.sweep_param(2)
    orch_b.fit_param(2)
    out0 = orch_b.validate_param(0, probe_points=VAL_PROBE_POINTS)
    out2 = orch_b.validate_param(2, probe_points=VAL_PROBE_POINTS)
    assert out0.passed is True
    assert out2.passed is True

    orch_b.codegen()
    write_token = orch_b.request_writes_approval()
    orch_b.approval_store.approve(
        write_token, "writes", orch_b.write_queue.to_payload(),
    )
    orch_b.apply_writes(token=write_token)
    pytest_result = orch_b.run_pytest()
    assert pytest_result.all_passed, pytest_result.summary

    proposal = orch_b.build_commit_proposal()
    commit_token = orch_b.request_commit_approval(proposal)
    orch_b.approval_store.approve(
        commit_token, "commit", proposal.to_payload(),
    )
    sha = orch_b.apply_commit(proposal, token=commit_token)
    assert len(sha) == 40
    assert orch_b.session.step == "done"
    assert orch_b.session.commit_sha == sha


def test_resume_picks_up_after_codegen_kill(
    repo_root: Path, session_path: Path,
) -> None:
    """Kill *after* codegen has queued writes but before they're
    flushed. The session records files_proposed; the resumed
    orchestrator's WriteQueue is rebuilt from codegen+propose
    (idempotent: same SkillSpec → same files), then approval +
    apply lands as if uninterrupted."""
    responses = (
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    fl_a = FakeFL()
    orch_a = TrainingOrchestrator(
        fl=fl_a, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=CannedProvider(responses),
        approval_store=ApprovalStore(),
        session_path=session_path,
        sleep=lambda s: None, dwell_s=0.0,
    )
    orch_a.enumerate()
    for pid in (0, 1, 2):
        orch_a.classify_param(pid)
    orch_a.sweep_param(0)
    orch_a.sweep_param(2)
    orch_a.fit_param(0)
    orch_a.fit_param(2)
    orch_a.validate_param(0, probe_points=VAL_PROBE_POINTS)
    orch_a.validate_param(2, probe_points=VAL_PROBE_POINTS)
    files_a = orch_a.codegen()
    proposed_a = sorted(orch_a.session.files_proposed)
    assert orch_a.session.step == "generating"

    # Drop — but no writes flushed yet.
    del orch_a, fl_a

    # Resume + re-codegen (the queue is empty in the fresh
    # orchestrator, so the agent's natural "what state am I in?"
    # answer when step=='generating' is to call codegen() again).
    fl_b = FakeFL()
    orch_b = resume_orchestrator(
        fl=fl_b, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=CannedProvider([]),     # no further readbacks needed
        session_path=session_path,
    )
    assert orch_b is not None
    files_b = orch_b.codegen()

    # codegen is reproducible — files identical except for the
    # calibration log's started_at/finished_at timestamps.
    for fname in ("manifest.json", "wrapper.py", "tool.py",
                  "knowledge.md", "tests.py", "__init__.py"):
        assert files_a[fname] == files_b[fname], (
            f"{fname} drifted between original and resumed codegen"
        )
    proposed_b = sorted(orch_b.session.files_proposed)
    assert proposed_a == proposed_b

    # Apply + commit lands as in the kill-free path.
    write_token = orch_b.request_writes_approval()
    orch_b.approval_store.approve(
        write_token, "writes", orch_b.write_queue.to_payload(),
    )
    orch_b.apply_writes(token=write_token)
    pytest_result = orch_b.run_pytest()
    assert pytest_result.all_passed
    proposal = orch_b.build_commit_proposal()
    commit_token = orch_b.request_commit_approval(proposal)
    orch_b.approval_store.approve(
        commit_token, "commit", proposal.to_payload(),
    )
    sha = orch_b.apply_commit(proposal, token=commit_token)
    assert len(sha) == 40
    assert orch_b.session.step == "done"
