"""Integration tests for P3-C — mixing-agent ToolExecutor dispatching
through the skill registry.

Asserts that ``set_proq3`` and ``set_compressor`` are no longer
hand-defined in ``agent/tools.py`` but are instead delivered by the
FabFilter Pro-Q 3 and Fruity Compressor skills under
``src/studiomind/skills/``. The result shapes must match the
hand-written executors' shapes from the pre-P3-C era so the mixing
prompt's expectations stay valid."""

from __future__ import annotations

from typing import Any

import pytest

from studiomind.agent.tools import (
    DESTRUCTIVE_TOOLS,
    TOOL_SCHEMAS,
    ToolExecutor,
    build_tool_schemas,
    compute_destructive_tools,
)
from studiomind.skills._registry import build_knowledge_section, load_all_skills


# ───────────────────────────── Fake FL ────────────────────────────────

class FakeFL:
    """Records every set_plugin_param call and returns a FL-shaped dict
    where ``new_value`` echoes the requested value (so the Fruity
    Compressor verification path treats every write as accepted)."""

    def __init__(self, *, echo_writes: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._echo = echo_writes

    def set_plugin_param(
        self,
        track_id: int,
        slot: int,
        param_id: int,
        value: float,
    ) -> dict[str, Any]:
        self.calls.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": param_id,
            "value": value,
        })
        return {
            "ok": True,
            "param_id": param_id,
            "new_value": value if self._echo else 0.0,
            "display": f"{value:.4f}",
        }

    # The real FLStudio bridge has many other methods but the skill
    # tools only call set_plugin_param. Anything else would be a bug
    # in the skill — let it raise via attribute error.


# ───────────────────────────── Fixtures ───────────────────────────────

@pytest.fixture
def loaded_skills():
    skills, errors = load_all_skills()
    assert not errors, f"Skill load errors: {errors}"
    by_name = {s.name: s for s in skills}
    assert "fabfilter_proq3" in by_name
    assert "fruity_compressor" in by_name
    return skills


@pytest.fixture
def executor(loaded_skills) -> ToolExecutor:
    return ToolExecutor(fl=FakeFL(), workspace=None, skills=loaded_skills)


# ───────────────────────── Schema assembly ────────────────────────────

def test_set_proq3_not_in_builtin_schemas() -> None:
    """The hand-written set_proq3 schema entry was removed in P3-C —
    its source of truth is the FabFilter Pro-Q 3 skill."""
    builtin_names = {t["name"] for t in TOOL_SCHEMAS}
    assert "set_proq3" not in builtin_names
    assert "set_compressor" not in builtin_names


def test_set_proq3_not_in_builtin_destructive() -> None:
    """The hand-written DESTRUCTIVE_TOOLS set must no longer contain
    skill-provided tool names; those are folded in by
    compute_destructive_tools(skills) at boot."""
    assert "set_proq3" not in DESTRUCTIVE_TOOLS
    assert "set_compressor" not in DESTRUCTIVE_TOOLS


def test_build_tool_schemas_merges_skill_tools(loaded_skills) -> None:
    combined = build_tool_schemas(loaded_skills)
    names = {t["name"] for t in combined}
    assert "set_proq3" in names
    assert "set_compressor" in names
    # Built-ins still present
    assert "set_builtin_eq" in names
    assert "apply_sidechain" in names


def test_compute_destructive_tools_merges_skill_destructives(loaded_skills) -> None:
    combined = compute_destructive_tools(loaded_skills)
    assert "set_proq3" in combined
    assert "set_compressor" in combined
    # Built-ins still present
    assert "set_builtin_eq" in combined
    assert "apply_sidechain" in combined


# ───────────────────── set_proq3 dispatch via skill ───────────────────

def test_dispatch_set_proq3_returns_simple_shape(executor) -> None:
    """The dispatch path matches the pre-P3-C result shape: ok, band,
    params_set, plus the human-input fields echoed back."""
    result = executor.execute("set_proq3", {
        "track_id": 4,
        "slot": 0,
        "band": 3,
        "frequency_hz": 1000.0,
        "gain_db": -2.5,
        "q": 1.0,
        "shape": "bell",
    })
    assert result["ok"] is True
    assert result["band"] == 3
    assert result["params_set"] >= 4   # used + freq + gain + q at minimum
    assert result["frequency_hz"] == 1000.0
    assert result["gain_db"] == -2.5
    assert result["shape"] == "bell"


def test_dispatch_set_proq3_drives_fl_set_plugin_param(loaded_skills) -> None:
    fl = FakeFL()
    ex = ToolExecutor(fl=fl, workspace=None, skills=loaded_skills)
    ex.execute("set_proq3", {
        "track_id": 7,
        "slot": 1,
        "band": 1,
        "gain_db": -3.0,
    })
    assert fl.calls, "skill dispatch must drive FL.set_plugin_param"
    for call in fl.calls:
        assert call["track_id"] == 7
        assert call["slot"] == 1
        assert 0.0 <= call["value"] <= 1.0


# ─────────────────── set_compressor dispatch via skill ────────────────

def test_dispatch_set_compressor_returns_verification_shape(executor) -> None:
    result = executor.execute("set_compressor", {
        "track_id": 5,
        "slot": 2,
        "threshold_db": -18.0,
        "ratio": 4.0,
        "attack_ms": 10.0,
        "release_ms": 100.0,
        "gain_db": 2.0,
        "knee": "smooth",
    })
    assert result["ok"] is True
    assert result["params_attempted"] == 6
    assert result["params_accepted"] == 6
    assert result["params_rejected"] == 0
    assert isinstance(result["per_param"], list) and len(result["per_param"]) == 6
    for p in result["per_param"]:
        assert p["took"] is True
    # Echoed input fields
    assert result["threshold_db"] == -18.0
    assert result["ratio"] == 4.0
    assert result["knee"] == "smooth"


def test_dispatch_set_compressor_flags_rejected_writes(loaded_skills) -> None:
    """When FL's new_value doesn't match the requested write (i.e.,
    write was silently rejected or clamped), per_param[].took flips
    False and ok rolls up to False."""
    fl = FakeFL(echo_writes=False)
    ex = ToolExecutor(fl=fl, workspace=None, skills=loaded_skills)
    result = ex.execute("set_compressor", {
        "track_id": 5, "slot": 2, "threshold_db": -18.0,
    })
    assert result["ok"] is False
    assert result["params_attempted"] == 1
    assert result["params_accepted"] == 0
    assert result["params_rejected"] == 1
    assert result["per_param"][0]["took"] is False


# ───────────────────────── Knowledge section ──────────────────────────

def test_knowledge_section_includes_both_skills(loaded_skills) -> None:
    section = build_knowledge_section(loaded_skills)
    assert "FabFilter Pro-Q 3" in section
    assert "Fruity Compressor" in section
    # The section is appended to the system prompt; it must be a single
    # contiguous block headed by a top-level heading so the mixing agent
    # can scope its mental model.
    assert section.startswith("# Acquired skills")


# ───────────────────────── Unknown tool path ──────────────────────────

def test_unknown_tool_falls_through_to_error(loaded_skills) -> None:
    ex = ToolExecutor(fl=FakeFL(), workspace=None, skills=loaded_skills)
    out = ex.execute("nonexistent_tool", {})
    assert "error" in out
    assert "Unknown tool" in out["error"]
