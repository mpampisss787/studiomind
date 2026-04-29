"""Training agent loop.

Two layers in this file:

* :class:`TrainingOrchestrator` — deterministic step machine that runs
  the wizard (enumerate → classify → sweep → fit → validate → codegen
  → run_pytest → commit). Each step persists the ``TrainingSession``
  to disk so a kill-and-restart can resume. Pure of LLM; tested
  end-to-end with mocked FL + canned readbacks.

* :class:`TrainingAgent` — thin Anthropic-driven loop that surfaces
  the orchestrator's steps as Claude tools. P4 supplies the
  scaffolding so P5's web UI can wire it in; the live LLM run is
  exercised in P5/P6.

Reading this file top-to-bottom mirrors the wizard order.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from studiomind.learning import calibration as cal
from studiomind.learning import codegen
from studiomind.learning import code_edit
from studiomind.learning import session_state as ss
from studiomind.learning.approval_tokens import ApprovalStore
from studiomind.learning.calibration import (
    ClassificationResult,
    ReadbackProvider,
    Readback,
)
from studiomind.learning.code_edit import CommitProposal, WriteQueue
from studiomind.learning.codegen import (
    FitSpec,
    ParamSpec,
    SkillSpec,
    ValidationProbeSpec,
)
from studiomind.learning.curves import Fit, Sample
from studiomind.learning.pytest_runner import PytestResult, run_pytest_for_skill
from studiomind.learning.session_state import (
    FitRecord,
    ParamRecord,
    SampleRecord,
    TrainingSession,
    ValidationProbeRecord,
)

log = logging.getLogger(__name__)


class TrainingError(Exception):
    """Wizard-level error: bad state, invalid step transition, etc."""


@dataclass
class TrainingOrchestrator:
    """Deterministic wizard. The agent calls these methods in order;
    each one updates ``self.session`` and persists it.

    The orchestrator owns:
      * the FL bridge (or a fake)
      * a ReadbackProvider (real UI in P5; canned in tests)
      * an ApprovalStore (one per session)
      * a WriteQueue (one per session)
      * the on-disk TrainingSession path

    It does NOT own the Anthropic client — that lives on TrainingAgent
    when wired in P5.
    """

    fl: Any
    repo_root: Path
    plugin_name: str
    skill_name: str
    tool_name: str
    fl_version: str
    track_id: int
    slot: int
    readback_provider: ReadbackProvider
    approval_store: ApprovalStore = field(default_factory=ApprovalStore)
    session_path: Path = ss.SESSION_PATH
    session: TrainingSession = field(init=False)
    write_queue: WriteQueue = field(init=False)
    sleep: Callable[[float], None] = field(default=time.sleep)
    dwell_s: float = 0.5

    def __post_init__(self) -> None:
        self.session = TrainingSession.new(
            self.plugin_name,
            fl_version=self.fl_version,
            track_id=self.track_id,
            slot=self.slot,
        )
        self.write_queue = WriteQueue(
            current_skill=self.skill_name,
            repo_root=self.repo_root,
        )

    # ───────────────── persistence helpers ─────────────────

    def _persist(self) -> None:
        """Save the session after every meaningful state change."""
        self.session.updated_at = time.time()
        self.session.save(self.session_path)

    def attach_session(self, session: TrainingSession) -> None:
        """Hot-swap the session — used by ``resume_orchestrator`` to
        continue a kill-interrupted acquisition."""
        self.session = session

    # ───────────────── step 1: enumerate ─────────────────

    def enumerate(self) -> list[cal.EnumeratedParam]:
        """Read the plugin's params from FL and seed empty
        ParamRecords on the session."""
        rows = cal.enumerate_plugin_params(self.fl, self.track_id, self.slot)
        for r in rows:
            if self.session.get_param(r.id) is None:
                self.session.upsert_param(ParamRecord(id=r.id, name=r.name))
        self.session.set_step("enumerated")
        self._persist()
        return rows

    # ───────────────── step 2: classify each ─────────────────

    def classify_param(self, param_id: int) -> ClassificationResult:
        """Drive the two probe values, ask user, decide kind."""
        self.session.set_step("classifying")
        self._persist()
        result = cal.classify_param(
            self.fl, self.track_id, self.slot, param_id,
            provider=self.readback_provider,
            dwell_s=self.dwell_s,
            sleep=self.sleep,
        )
        rec = self.session.get_param(param_id)
        if rec is None:
            raise TrainingError(f"classify_param: unknown param_id {param_id}")
        if result.kind in ("continuous", "enum"):
            rec.kind = result.kind
        else:
            rec.kind = None  # ambiguous; agent should ask user
        if result.kind == "enum":
            # Seed the enum table with the two raw labels we observed.
            # The sweep step refines this for params with >2 labels.
            rec.enum_values = {
                result.readback_a.raw.strip().lower(): cal.CLASSIFY_TEST_VALUES[0],
                result.readback_b.raw.strip().lower(): cal.CLASSIFY_TEST_VALUES[1],
            }
        self.session.upsert_param(rec)
        self._persist()
        return result

    def set_param_kind_manual(
        self,
        param_id: int,
        kind: str,
        *,
        enum_values: dict[str, float] | None = None,
    ) -> None:
        """Override classification — for params the heuristic reports
        as ambiguous. The agent calls this after asking the user."""
        if kind not in ("continuous", "enum"):
            raise TrainingError(f"set_param_kind_manual: invalid kind {kind!r}")
        rec = self.session.get_param(param_id)
        if rec is None:
            raise TrainingError(f"unknown param_id {param_id}")
        rec.kind = kind
        if kind == "enum" and enum_values is not None:
            rec.enum_values = dict(enum_values)
        self.session.upsert_param(rec)
        self._persist()

    # ───────────────── step 3: sweep continuous ─────────────────

    def sweep_param(
        self,
        param_id: int,
        *,
        points: Sequence[float] = cal.DEFAULT_SWEEP_POINTS,
    ) -> list[tuple[float, Readback]]:
        """6-point sweep for one continuous param. Records every sample
        on the session — including unparsed readbacks (so the agent can
        ask the user to retry that point)."""
        rec = self.session.get_param(param_id)
        if rec is None:
            raise TrainingError(f"sweep_param: unknown param_id {param_id}")
        if rec.kind != "continuous":
            raise TrainingError(
                f"sweep_param: param {param_id} is not continuous (kind={rec.kind!r})"
            )
        self.session.set_step("sweeping")
        self._persist()

        sweep = cal.run_sweep(
            self.fl, self.track_id, self.slot, param_id,
            provider=self.readback_provider,
            points=points,
            dwell_s=self.dwell_s,
            sleep=self.sleep,
        )
        rec.samples = [
            SampleRecord(param_value=p, displayed=r.raw, displayed_value=r.parsed if r.parsed is not None else float("nan"))
            for p, r in sweep
        ]
        self.session.upsert_param(rec)
        self._persist()
        return sweep

    # ───────────────── step 4: fit ─────────────────

    def fit_param(self, param_id: int, *, min_r_squared: float = 0.99) -> Fit | None:
        """Run the curve fitter over the recorded samples. Stores the
        selected fit (or None) on the session."""
        rec = self.session.get_param(param_id)
        if rec is None:
            raise TrainingError(f"fit_param: unknown param_id {param_id}")
        samples: list[Sample] = []
        for s in rec.samples:
            if s.displayed_value != s.displayed_value:   # NaN check
                continue
            samples.append(Sample(param=s.param_value, displayed=s.displayed_value))
        if len(samples) < 2:
            raise TrainingError(
                f"fit_param: need ≥2 valid samples, have {len(samples)}"
            )
        self.session.set_step("fitting")
        self._persist()

        fit = cal.fit_param(samples, min_r_squared=min_r_squared)
        if fit is not None:
            rec.selected_fit = FitRecord(
                shape=fit.shape, params=list(fit.params),
                r_squared=fit.r_squared, rmse=fit.rmse,
            )
        else:
            rec.selected_fit = None
        self.session.upsert_param(rec)
        self._persist()
        return fit

    # ───────────────── step 5: validate ─────────────────

    def validate_param(
        self,
        param_id: int,
        *,
        runner_up: Fit | None = None,
        tolerance_abs: float = 0.5,
        tolerance_rel: float = 0.01,
        probe_points: Sequence[float] | None = None,
    ) -> cal.ValidationOutcome:
        rec = self.session.get_param(param_id)
        if rec is None or rec.selected_fit is None:
            raise TrainingError(
                f"validate_param: param {param_id} has no selected_fit"
            )
        fit = Fit(
            shape=rec.selected_fit.shape,                 # type: ignore[arg-type]
            params=tuple(rec.selected_fit.params),
            r_squared=rec.selected_fit.r_squared,
            rmse=rec.selected_fit.rmse,
        )
        self.session.set_step("validating")
        self._persist()

        outcome = cal.validate_fit(
            self.fl, self.track_id, self.slot, param_id,
            fit=fit, runner_up=runner_up,
            provider=self.readback_provider,
            tolerance_abs=tolerance_abs,
            tolerance_rel=tolerance_rel,
            dwell_s=self.dwell_s,
            sleep=self.sleep,
            probe_points=probe_points,
        )
        rec.validation_probes = [
            ValidationProbeRecord(
                param_value=p.param_value,
                predicted=p.predicted,
                actual=p.actual,
                delta=p.delta,
                ok=p.ok,
            )
            for p in outcome.probes
        ]
        self.session.upsert_param(rec)
        self._persist()
        return outcome

    # ───────────────── step 6: codegen ─────────────────

    def build_skill_spec(self, *, acquired_iso: str | None = None,
                         human_ranges: dict[int, tuple[float, float, str]] | None = None,
                         ) -> SkillSpec:
        """Assemble a SkillSpec from the session's current state.

        ``human_ranges`` maps param_id → (human_min, human_max, unit).
        If absent, ranges are inferred from the recorded sample bounds
        for continuous params.
        """
        if not self.session.params:
            raise TrainingError("build_skill_spec: session has no params")
        params: list[ParamSpec] = []
        for rec in sorted(self.session.params, key=lambda x: x.id):
            if rec.kind == "continuous":
                if rec.selected_fit is None:
                    raise TrainingError(
                        f"build_skill_spec: param {rec.id} not yet fit"
                    )
                if human_ranges and rec.id in human_ranges:
                    hmin, hmax, unit = human_ranges[rec.id]
                else:
                    vals = [s.displayed_value for s in rec.samples
                            if s.displayed_value == s.displayed_value]
                    hmin = min(vals) if vals else None
                    hmax = max(vals) if vals else None
                    unit = ""
                params.append(ParamSpec(
                    id=rec.id, name=rec.name, kind="continuous",
                    fit=FitSpec(
                        shape=rec.selected_fit.shape,                 # type: ignore[arg-type]
                        params=tuple(rec.selected_fit.params),
                        r_squared=rec.selected_fit.r_squared,
                    ),
                    human_min=hmin, human_max=hmax, unit=unit,
                    validation_probes=tuple(
                        ValidationProbeSpec(
                            param_value=v.param_value,
                            predicted=v.predicted,
                            actual=v.actual if v.actual is not None else 0.0,
                            ok=bool(v.ok),
                        )
                        for v in rec.validation_probes
                    ),
                ))
            elif rec.kind == "enum":
                params.append(ParamSpec(
                    id=rec.id, name=rec.name, kind="enum",
                    enum_values=dict(rec.enum_values),
                ))
            else:
                raise TrainingError(
                    f"build_skill_spec: param {rec.id} has unresolved kind ({rec.kind!r})"
                )
        return SkillSpec(
            plugin_name=self.plugin_name,
            skill_name=self.skill_name,
            tool_name=self.tool_name,
            fl_version=self.fl_version,
            acquired_iso=acquired_iso or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            calibration_log_relpath=f"calibration-logs/{self.session.session_id}.json",
            params=tuple(params),
        )

    def codegen(
        self,
        *,
        acquired_iso: str | None = None,
        human_ranges: dict[int, tuple[float, float, str]] | None = None,
    ) -> dict[str, str]:
        """Render every text file for the skill, queue them all
        through ``propose_write``. Returns the rendered files dict
        (path-relative-to-skill → content) for the agent to preview."""
        spec = self.build_skill_spec(
            acquired_iso=acquired_iso, human_ranges=human_ranges,
        )
        files = codegen.render_skill(spec)

        # Queue every file under src/studiomind/skills/<name>/
        skill_rel = Path("src") / "studiomind" / "skills" / spec.skill_name
        for fname, body in sorted(files.items()):
            rel_path = str(skill_rel / fname)
            code_edit.propose_write(self.write_queue, rel_path=rel_path, content=body)

        # Also queue the calibration log sidecar.
        log_text = codegen.render_calibration_log(
            spec,
            started_iso=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.session.started_at)) if self.session.started_at else "",
            finished_iso=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        )
        log_rel = str(skill_rel / spec.calibration_log_relpath)
        code_edit.propose_write(self.write_queue, rel_path=log_rel, content=log_text)

        self.session.set_step("generating")
        self.session.files_proposed = [w.rel_path for w in self.write_queue.writes]
        self._persist()
        return files

    # ───────────────── step 7: apply writes + run pytest ─────────────────

    def request_writes_approval(self) -> str:
        return code_edit.request_writes_approval(self.write_queue, self.approval_store)

    def apply_writes(self, *, token: str) -> list[Path]:
        written = code_edit.apply_proposed_writes(
            self.write_queue, token=token, approval_store=self.approval_store,
        )
        self.session.files_written = [str(p) for p in written]
        self._persist()
        return written

    def run_pytest(self) -> PytestResult:
        """Run pytest on the skill's tests.py. Falls back to FAILURE
        if pytest itself errors out — the agent should treat any
        non-pass result as a hard stop."""
        self.session.set_step("testing")
        self._persist()
        result = run_pytest_for_skill(self.skill_name)
        return result

    # ───────────────── step 8: commit ─────────────────

    def build_commit_proposal(
        self,
        *,
        message: str | None = None,
        spec: SkillSpec | None = None,
        content_hash: str | None = None,
    ) -> CommitProposal:
        if spec is None:
            spec = self.build_skill_spec()
        message = message or (
            f"Acquire {spec.plugin_name} via training mode\n\n"
            f"Auto-generated skill at src/studiomind/skills/{spec.skill_name}/. "
            f"Calibration log archived in-skill."
        )
        trailers = {
            "Skill-Acquired-Via": "studiomind-training-mode",
            "Skill-Name": spec.skill_name,
            "Skill-Schema-Version": "1",
            "FL-Version": spec.fl_version,
            "Calibration-Log": f"src/studiomind/skills/{spec.skill_name}/{spec.calibration_log_relpath}",
        }
        if content_hash:
            trailers["Skill-Content-Hash"] = content_hash
        # Only stage files that were actually written.
        rel_paths = list(self.session.files_written)
        # The session stored absolute paths; convert to repo-relative
        # for the commit proposal (sandbox accepts either, but
        # repo-relative is what `git status` shows).
        rel_repo = []
        for p in rel_paths:
            ap = Path(p).resolve()
            try:
                rel_repo.append(str(ap.relative_to(self.repo_root.resolve())))
            except ValueError:
                rel_repo.append(p)
        return CommitProposal(
            current_skill=self.skill_name,
            repo_root=self.repo_root,
            message=message,
            paths=rel_repo,
            trailers=trailers,
        )

    def request_commit_approval(self, proposal: CommitProposal) -> str:
        self.session.set_step("committing")
        self._persist()
        return code_edit.request_commit_approval(proposal, self.approval_store)

    def apply_commit(
        self,
        proposal: CommitProposal,
        *,
        token: str,
        runner: Any | None = None,
    ) -> str:
        sha = code_edit.apply_commit(
            proposal, token=token,
            approval_store=self.approval_store,
            runner=runner,
        )
        self.session.commit_sha = sha
        self.session.set_step("done")
        self._persist()
        return sha

    # ───────────────── lifecycle ─────────────────

    def abort(self, reason: str = "") -> None:
        self.session.set_step("aborted")
        self.session.notes = (self.session.notes + "\n" + reason).strip()
        self._persist()

    def discard(self) -> None:
        ss.discard(self.session_path)


# ───────────────────────────── resume helper ──────────────────────────

def resume_orchestrator(
    *,
    fl: Any,
    repo_root: Path,
    plugin_name: str,
    skill_name: str,
    tool_name: str,
    fl_version: str,
    track_id: int,
    slot: int,
    readback_provider: ReadbackProvider,
    approval_store: ApprovalStore | None = None,
    session_path: Path = ss.SESSION_PATH,
) -> TrainingOrchestrator | None:
    """If a session file exists, build an orchestrator with that state
    attached; the agent can resume from the recorded step. Returns
    None when there's nothing to resume."""
    session = ss.load(session_path)
    if session is None:
        return None
    orch = TrainingOrchestrator(
        fl=fl,
        repo_root=repo_root,
        plugin_name=plugin_name,
        skill_name=skill_name,
        tool_name=tool_name,
        fl_version=fl_version,
        track_id=track_id,
        slot=slot,
        readback_provider=readback_provider,
        approval_store=approval_store or ApprovalStore(),
        session_path=session_path,
    )
    orch.attach_session(session)
    return orch


# ───────────────────────────── Anthropic-driven loop (skeleton) ──────

@dataclass
class TrainingAgentConfig:
    """Configuration for the LLM-driven loop. Wired in P5 once the
    web UI / CLI mounts a real readback provider."""
    model: str = "claude-opus-4-7"
    max_turns: int = 60
    on_message: Callable[[str], None] | None = None


class TrainingAgent:
    """Thin Anthropic wrapper around a TrainingOrchestrator. P4 ships
    only the constructor + a placeholder ``run`` so P5's web UI has a
    stable import target. The actual tool dispatch is wired in P5."""

    def __init__(
        self,
        orchestrator: TrainingOrchestrator,
        *,
        config: TrainingAgentConfig | None = None,
    ) -> None:
        self._orch = orchestrator
        self._config = config or TrainingAgentConfig()

    @property
    def orchestrator(self) -> TrainingOrchestrator:
        return self._orch

    def run(self, user_goal: str) -> str:  # pragma: no cover — wired in P5
        """Placeholder. P5 will:
          1. Build the system prompt via build_training_system_prompt()
          2. Build the tool list (one Anthropic tool per orchestrator step)
          3. Drive Anthropic's tool-use loop
          4. Surface readback + approval requests over /ws/training
        """
        raise NotImplementedError(
            "TrainingAgent.run is wired in P5; for P4 the orchestrator is "
            "exercised directly by tests and the CLI subcommand."
        )
