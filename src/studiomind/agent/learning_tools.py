"""Anthropic tool schemas + dispatch for the training agent.

The training agent's tool surface mirrors :class:`TrainingOrchestrator`
one-to-one. Each Anthropic ``tool_use`` block resolves to one
orchestrator method via :func:`execute_training_tool`; the result
gets serialized to a JSON-friendly dict and fed back as a
``tool_result``.

Where the orchestrator stores stateful objects between calls (the
pending :class:`CommitProposal` produced by ``build_commit_proposal``
and consumed by ``request_commit_approval`` / ``apply_commit``), this
module owns a small :class:`TrainingDispatchState` dataclass to bridge
those calls.

Approval-gated tools (``apply_writes``, ``apply_commit``) accept the
token as input — the agent first calls ``request_writes_approval`` /
``request_commit_approval``, surfaces the token + payload to the UI,
waits for the user's Approve click (which echoes the token back as a
follow-up user message), and then calls the apply tool. The orchestra-
tor's ApprovalStore validates the token before any disk/git work fires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from studiomind.agent.learning_loop import TrainingOrchestrator
    from studiomind.learning.code_edit import CommitProposal

log = logging.getLogger(__name__)


# ───────────────────────────── tool schemas ───────────────────────────

TRAINING_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "enumerate",
        "description": (
            "Read the plugin's parameter list from FL. Call this ONCE at "
            "the start of every wizard run. Returns "
            "{params: [{id, name, default_value}, ...]}. The list is "
            "sorted by id."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "classify_param",
        "description": (
            "Drive a single parameter to two probe values, ask the user "
            "what FL displays for each, decide continuous vs enum vs "
            "ambiguous. Call ONCE per parameter, in id order, after "
            "enumerate. Result: {kind, confident, readback_a, readback_b, "
            "note}. If kind is 'ambiguous' or confident is false, ask "
            "the user and use set_param_kind_manual to pin the kind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "param_id": {"type": "integer", "description": "Parameter id from enumerate"},
            },
            "required": ["param_id"],
        },
    },
    {
        "name": "set_param_kind_manual",
        "description": (
            "Override classification for an ambiguous parameter. Use "
            "AFTER asking the user. For enum, pass enum_values "
            "{label: param_value, ...}; for continuous, omit it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "param_id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["continuous", "enum"]},
                "enum_values": {
                    "type": "object",
                    "description": "{label: param_value, ...}; required when kind=enum.",
                },
            },
            "required": ["param_id", "kind"],
        },
    },
    {
        "name": "sweep_param",
        "description": (
            "Six-point sweep on a continuous parameter. Drives FL to "
            "0.0/0.2/0.4/0.6/0.8/1.0 and asks the user for the "
            "displayed value at each step. Call ONLY after classify "
            "confirms continuous. Returns "
            "{samples: [{param_value, raw, parsed}, ...], all_parsed}. "
            "If all_parsed is false, ask the user to retype the failing "
            "step before calling fit_param — fit needs every sample to "
            "be numeric."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"param_id": {"type": "integer"}},
            "required": ["param_id"],
        },
    },
    {
        "name": "fit_param",
        "description": (
            "Fit a curve to the recorded samples. Picks the simplest "
            "shape (linear / log / power / exp / quadratic) with "
            "R² ≥ min_r_squared (default 0.99). Returns {fit, ok}. "
            "When fit is null, ask the user for 3 more samples at the "
            "highest-residual points and re-sweep before retrying."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "param_id": {"type": "integer"},
                "min_r_squared": {
                    "type": "number",
                    "description": "R² floor for accepting a fit (default 0.99).",
                },
            },
            "required": ["param_id"],
        },
    },
    {
        "name": "validate_param",
        "description": (
            "Drive 4 deterministic probe points (2 extremity + 2 "
            "midpoint), ask the user for FL readbacks, compare against "
            "the fit's prediction. Returns {passed, probes}. If passed "
            "is false: surface the failing deltas, ask the user for "
            "more samples at those points, re-sweep, re-fit, re-"
            "validate. Up to 3 retries before calling abort."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"param_id": {"type": "integer"}},
            "required": ["param_id"],
        },
    },
    {
        "name": "codegen",
        "description": (
            "Render the skill files (manifest, wrapper, tool, knowledge, "
            "tests, calibration log) and queue them through the sandbox. "
            "Disk untouched until apply_writes runs. Call ONLY after "
            "every continuous param has a passing validate_param result "
            "and every enum param has its enum_values pinned. Returns "
            "{files: [...], files_proposed: [...]}."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_writes_approval",
        "description": (
            "Ask the server to issue a one-shot approval token bound to "
            "the current write queue. Returns {token, payload}. Surface "
            "the payload to the user so they can preview the diff before "
            "approving. Pass the token to apply_writes after the user "
            "approves."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_writes",
        "description": (
            "Flush the queued writes to disk. Token is one-shot and "
            "bound to the queue payload — if you've added writes since "
            "the token was issued, it's rejected. Returns "
            "{ok, written: [paths]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Token from request_writes_approval (after user approves).",
                },
            },
            "required": ["token"],
        },
    },
    {
        "name": "run_pytest",
        "description": (
            "Run pytest against the freshly-written tests.py. Returns "
            "{all_passed, passed, failed, errors, summary, timed_out}. "
            "all_passed=false MUST block commit — surface the failure "
            "and abort, do not commit broken code."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "build_commit_proposal",
        "description": (
            "Build the commit proposal (message + paths + structured "
            "trailers). Returns {message, paths, trailers}. The "
            "proposal is held server-side until request_commit_approval "
            "fires; one proposal per session. Calling this twice "
            "replaces the previous draft."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Optional override; default is auto-generated.",
                },
            },
        },
    },
    {
        "name": "request_commit_approval",
        "description": (
            "Mint the commit-approval token. UI shows the user the "
            "preview from build_commit_proposal so they can confirm. "
            "Returns {token, proposal}."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_commit",
        "description": (
            "Stage the proposed paths and run git commit. NEVER pushes; "
            "the sandbox would refuse anyway. Returns {ok, sha}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "Token from request_commit_approval (after user approves).",
                },
            },
            "required": ["token"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user a free-form question and wait for their typed "
            "response. Use when you need clarification — e.g. whether an "
            "ambiguous param is continuous or enum, whether to skip a "
            "param, or any other decision that requires human input. "
            "Returns {answer: '...'}. ALWAYS use this instead of emitting "
            "bare text when you need user input — bare text without a "
            "tool call ends the session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "abort",
        "description": (
            "Mark the session aborted. Use when validation fails 3 "
            "rounds, the plugin's curve is too irregular for v1's "
            "fitter, or the user asks to stop. Always provide a reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

TRAINING_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TRAINING_TOOL_SCHEMAS)


# ───────────────────────────── dispatch state ────────────────────────

@dataclass
class TrainingDispatchState:
    """Stateful objects that persist across tool calls within one
    agent run. Currently just the pending :class:`CommitProposal` —
    built by ``build_commit_proposal``, then approved + applied in
    later turns."""
    pending_commit_proposal: "CommitProposal | None" = None


# ───────────────────────────── dispatch ──────────────────────────────

def execute_training_tool(
    orchestrator: "TrainingOrchestrator",
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    state: TrainingDispatchState,
) -> dict[str, Any]:
    """Dispatch one Anthropic tool call to the corresponding
    orchestrator method (or the dispatch-state holder for staged
    proposals). Always returns a JSON-friendly dict; never raises
    — orchestrator errors are surfaced as {"error": "..."} so the
    agent can decide to retry or abort."""
    if tool_name == "enumerate":
        rows = orchestrator.enumerate()
        return {
            "params": [
                {"id": r.id, "name": r.name, "default_value": r.default_value}
                for r in rows
            ],
        }

    if tool_name == "classify_param":
        result = orchestrator.classify_param(int(tool_input["param_id"]))
        return {
            "kind": result.kind,
            "confident": result.confident,
            "readback_a": {
                "raw": result.readback_a.raw,
                "parsed": result.readback_a.parsed,
            },
            "readback_b": {
                "raw": result.readback_b.raw,
                "parsed": result.readback_b.parsed,
            },
            "note": result.note,
        }

    if tool_name == "set_param_kind_manual":
        orchestrator.set_param_kind_manual(
            int(tool_input["param_id"]),
            str(tool_input["kind"]),
            enum_values=tool_input.get("enum_values"),
        )
        return {"ok": True}

    if tool_name == "sweep_param":
        sweep = orchestrator.sweep_param(int(tool_input["param_id"]))
        samples = [
            {"param_value": p, "raw": r.raw, "parsed": r.parsed}
            for p, r in sweep
        ]
        return {
            "samples": samples,
            "all_parsed": all(r.parsed is not None for _, r in sweep),
        }

    if tool_name == "fit_param":
        kwargs: dict[str, Any] = {}
        if "min_r_squared" in tool_input:
            kwargs["min_r_squared"] = float(tool_input["min_r_squared"])
        fit = orchestrator.fit_param(int(tool_input["param_id"]), **kwargs)
        if fit is None:
            return {
                "ok": False,
                "fit": None,
                "reason": (
                    "No candidate shape cleared the R² floor. Ask the "
                    "user for 3 more samples at the highest-residual "
                    "points and re-sweep + re-fit."
                ),
            }
        return {
            "ok": True,
            "fit": {
                "shape": fit.shape,
                "params": list(fit.params),
                "r_squared": fit.r_squared,
                "rmse": fit.rmse,
            },
        }

    if tool_name == "validate_param":
        outcome = orchestrator.validate_param(int(tool_input["param_id"]))
        return {
            "passed": outcome.passed,
            "probes": [
                {
                    "param_value": p.param_value,
                    "predicted": p.predicted,
                    "actual": p.actual,
                    "delta": p.delta,
                    "ok": p.ok,
                    "tolerance": p.tolerance,
                }
                for p in outcome.probes
            ],
        }

    if tool_name == "codegen":
        files = orchestrator.codegen()
        return {
            "files": sorted(files.keys()),
            "files_proposed": list(orchestrator.session.files_proposed),
        }

    if tool_name == "request_writes_approval":
        token = orchestrator.request_writes_approval()
        return {
            "token": token,
            "payload": orchestrator.write_queue.to_payload(),
        }

    if tool_name == "apply_writes":
        written = orchestrator.apply_writes(token=str(tool_input["token"]))
        return {
            "ok": True,
            "written": [str(p) for p in written],
        }

    if tool_name == "run_pytest":
        result = orchestrator.run_pytest()
        return {
            "all_passed": result.all_passed,
            "passed": result.passed,
            "failed": result.failed,
            "errors": result.errors,
            "summary": result.summary,
            "timed_out": result.timed_out,
            "returncode": result.returncode,
        }

    if tool_name == "build_commit_proposal":
        kwargs2: dict[str, Any] = {}
        if "message" in tool_input and tool_input["message"]:
            kwargs2["message"] = str(tool_input["message"])
        proposal = orchestrator.build_commit_proposal(**kwargs2)
        state.pending_commit_proposal = proposal
        return {
            "message": proposal.message,
            "paths": list(proposal.paths),
            "trailers": dict(proposal.trailers),
        }

    if tool_name == "request_commit_approval":
        if state.pending_commit_proposal is None:
            return {
                "ok": False,
                "error": "Call build_commit_proposal first.",
            }
        token = orchestrator.request_commit_approval(state.pending_commit_proposal)
        prop = state.pending_commit_proposal
        return {
            "token": token,
            "proposal": {
                "message": prop.message,
                "paths": list(prop.paths),
                "trailers": dict(prop.trailers),
            },
        }

    if tool_name == "apply_commit":
        if state.pending_commit_proposal is None:
            return {
                "ok": False,
                "error": "Call build_commit_proposal first.",
            }
        sha = orchestrator.apply_commit(
            state.pending_commit_proposal,
            token=str(tool_input["token"]),
        )
        return {"ok": True, "sha": sha}

    if tool_name == "ask_user":
        from studiomind.learning.calibration import ReadbackContext
        answer = orchestrator.readback_provider.request(
            str(tool_input["question"]),
            expected_unit="",
            context=ReadbackContext(
                phase="question", param_id=-1,
                param_name="", step="",
            ),
        )
        return {"answer": answer}

    if tool_name == "abort":
        orchestrator.abort(str(tool_input.get("reason", "agent abort")))
        return {"ok": True}

    return {"error": f"Unknown training tool: {tool_name!r}"}
