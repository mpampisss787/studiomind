"""End-to-end mock acquisition — the P4 acceptance gate.

Runs the TrainingOrchestrator from enumerate to commit against:
  * a fake FL bridge that records every set_plugin_param call
  * a canned readback provider returning points along known curves
  * a real git repo at tmp_path

Asserts:
  * skill directory created with all six text files
  * manifest content_hash matches what registry/_registry would
    compute on disk
  * generated wrapper recovers the linear curves used as ground truth
  * pytest passes against the auto-generated tests.py
  * commit landed in the test repo with the structured trailer
  * NO push attempted (controlled runner records every git invocation)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from studiomind.agent.learning_loop import TrainingOrchestrator
from studiomind.learning.approval_tokens import ApprovalStore
from studiomind.skills._registry import compute_content_hash


# ───────────────────────────── fakes ──────────────────────────────────

class FakeFL:
    """3-param fake plugin:
      0 Threshold (continuous, [-60, 0] dB, linear 60p-60)
      1 Style    (enum, hard/smooth)
      2 Mix      (continuous, [0, 100] %, linear 100p)
    """

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
        self.consumed: list[tuple[str, str]] = []

    def request(self, prompt: str, *, expected_unit: str = "", context: object = None) -> str:
        if not self.responses:
            raise RuntimeError(
                f"CannedProvider exhausted at prompt: {prompt!r} "
                f"(consumed {len(self.consumed)} responses)"
            )
        r = self.responses.pop(0)
        self.consumed.append((prompt, r))
        return r


# ───────────────────────────── fixtures ───────────────────────────────

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A real git-init'd repo at tmp_path/repo with the studiomind
    package skeleton (src/studiomind/skills/) and a synthetic
    pyproject.toml so the sandbox accepts it as a valid repo root."""
    repo = tmp_path / "repo"
    skills_dir = repo / "src" / "studiomind" / "skills"
    skills_dir.mkdir(parents=True)
    # Package init markers
    (repo / "src" / "studiomind" / "__init__.py").write_text("")
    (skills_dir / "__init__.py").write_text("")
    (repo / "pyproject.toml").write_text("[project]\nname='test'\n")
    # Real git repo
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


# ───────────────────────────── readback recipes ───────────────────────

CLASSIFY_THRESHOLD = ["-30 dB", "-15 dB"]                  # numeric → continuous
CLASSIFY_STYLE = ["hard", "smooth"]                         # strings → enum
CLASSIFY_MIX = ["25 %", "75 %"]                             # numeric → continuous

# linear y = 60p - 60 at sweep points 0.0, 0.2, 0.4, 0.6, 0.8, 1.0
SWEEP_THRESHOLD = ["-60 dB", "-48 dB", "-36 dB", "-24 dB", "-12 dB", "0 dB"]
# linear y = 100p
SWEEP_MIX = ["0 %", "20 %", "40 %", "60 %", "80 %", "100 %"]

# When runner_up=None, validate uses [0.05, 0.95, 0.30, 0.55] per
# select_validation_probes' fallback (sorted ascending by probe).
# That gives sorted([0.05, 0.30, 0.55, 0.95]).
VAL_PROBE_POINTS = [0.05, 0.30, 0.55, 0.95]
VAL_THRESHOLD = [f"{60.0 * p - 60.0:.4f} dB" for p in VAL_PROBE_POINTS]
VAL_MIX = [f"{100.0 * p:.4f} %" for p in VAL_PROBE_POINTS]


# ───────────────────────────── the gate test ──────────────────────────

def test_end_to_end_mock_acquisition(repo_root: Path, session_path: Path) -> None:
    fl = FakeFL()
    responses = (
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    provider = CannedProvider(responses)

    orch = TrainingOrchestrator(
        fl=fl,
        repo_root=repo_root,
        plugin_name="Demo Plugin",
        skill_name="demo_plugin",
        tool_name="set_demo",
        fl_version="21.2.10",
        track_id=4,
        slot=0,
        readback_provider=provider,
        approval_store=ApprovalStore(),
        session_path=session_path,
        sleep=lambda s: None,
        dwell_s=0.0,
    )

    # Wizard order:
    rows = orch.enumerate()
    assert {r.id for r in rows} == {0, 1, 2}

    for pid in (0, 1, 2):
        orch.classify_param(pid)

    assert orch.session.get_param(0).kind == "continuous"
    assert orch.session.get_param(1).kind == "enum"
    assert orch.session.get_param(2).kind == "continuous"

    orch.sweep_param(0)
    orch.sweep_param(2)

    f0 = orch.fit_param(0)
    f2 = orch.fit_param(2)
    assert f0 is not None and f0.shape == "linear"
    assert f2 is not None and f2.shape == "linear"

    out0 = orch.validate_param(0, probe_points=VAL_PROBE_POINTS)
    out2 = orch.validate_param(2, probe_points=VAL_PROBE_POINTS)
    assert out0.passed, f"threshold validation failed: {out0.probes}"
    assert out2.passed, f"mix validation failed: {out2.probes}"

    # codegen + writes
    files = orch.codegen()
    assert set(files.keys()) == {
        "__init__.py", "manifest.json", "wrapper.py",
        "tool.py", "knowledge.md", "tests.py",
    }
    write_token = orch.request_writes_approval()
    orch.approval_store.approve(
        write_token, "writes", orch.write_queue.to_payload(),
    )
    written = orch.apply_writes(token=write_token)
    skill_dir = repo_root / "src" / "studiomind" / "skills" / "demo_plugin"
    for fname in ("manifest.json", "wrapper.py", "tool.py",
                  "knowledge.md", "tests.py", "__init__.py"):
        assert (skill_dir / fname).exists()
    # Also the calibration log sidecar
    log_dir = skill_dir / "calibration-logs"
    assert log_dir.exists()
    assert any(log_dir.iterdir())

    # Manifest hash matches registry computation
    manifest = json.loads((skill_dir / "manifest.json").read_text())
    assert manifest["content_hash"] == compute_content_hash(skill_dir)

    # Generated wrapper recovers the curves via direct import. Importing
    # via studiomind.skills.demo_plugin would require sys.path / cache
    # contortions; load the raw wrapper file as a module.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "demo_plugin_wrapper", skill_dir / "wrapper.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # threshold = 60p - 60 → at p=0.5, value=-30
    assert abs(mod.param_to_threshold(0.5) - (-30.0)) < 1e-6
    # mix = 100p → at p=0.5, value=50
    assert abs(mod.param_to_mix(0.5) - 50.0) < 1e-6
    # style enum: probed at 0.25 and 0.75 → those are the seeded values
    assert "hard" in mod.STYLE_VALUES
    assert "smooth" in mod.STYLE_VALUES

    # Run the auto-generated pytest
    pytest_result = orch.run_pytest()
    assert pytest_result.all_passed, (
        f"Auto-generated tests failed: {pytest_result.summary}"
    )
    assert pytest_result.passed >= 1

    # commit
    proposal = orch.build_commit_proposal()
    commit_token = orch.request_commit_approval(proposal)
    orch.approval_store.approve(
        commit_token, "commit", proposal.to_payload(),
    )

    received_invocations: list[list[str]] = []

    def runner(args, **kwargs):
        received_invocations.append(list(args))
        return subprocess.run(args, **kwargs)

    sha = orch.apply_commit(proposal, token=commit_token, runner=runner)
    assert len(sha) == 40

    # Confirm the trailer landed in the commit message
    commit_msg = subprocess.run(
        ["git", "log", "-1", "--format=%B", sha],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout
    assert "Skill-Acquired-Via: studiomind-training-mode" in commit_msg
    assert "Skill-Name: demo_plugin" in commit_msg
    assert "FL-Version: 21.2.10" in commit_msg

    # No push attempted, anywhere
    pushy = [a for a in received_invocations if any("push" in tok for tok in a)]
    assert pushy == []

    # Session is now in step="done"
    assert orch.session.step == "done"
    assert orch.session.commit_sha == sha


def test_end_to_end_skips_when_validation_fails(
    repo_root: Path, session_path: Path,
) -> None:
    """Inject a bad readback during validation. The orchestrator
    should still report passed=False; the agent prompt is what
    decides to abort, not the orchestrator (which surfaces the
    diagnostic and lets the agent retry)."""
    fl = FakeFL()

    # Intentionally wrong validation readbacks for threshold (off by 10 dB)
    bad_threshold = [f"{60.0 * p - 60.0 + 10.0:.4f} dB" for p in VAL_PROBE_POINTS]

    responses = (
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + bad_threshold + VAL_MIX
    )
    orch = TrainingOrchestrator(
        fl=fl, repo_root=repo_root,
        plugin_name="Demo", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="x",
        track_id=0, slot=0,
        readback_provider=CannedProvider(responses),
        session_path=session_path,
        sleep=lambda s: None, dwell_s=0.0,
    )
    orch.enumerate()
    for pid in (0, 1, 2):
        orch.classify_param(pid)
    orch.sweep_param(0)
    orch.sweep_param(2)
    orch.fit_param(0)
    orch.fit_param(2)
    out0 = orch.validate_param(0, probe_points=VAL_PROBE_POINTS)
    assert out0.passed is False
    failed_probes = [p for p in out0.probes if not p.ok]
    assert failed_probes, "expected at least one out-of-tolerance probe"
