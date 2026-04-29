"""Tests for the skill code generator.

The acceptance properties:
  1. Reproducibility — same SkillSpec → byte-identical output for every file.
  2. Generated wrapper.py is importable as Python.
  3. Generated wrapper recovers the calibration curve (param_to_*
     matches the fit prediction; *_to_param round-trips back).
  4. Generated tests.py runs green when executed.
  5. content_hash in the manifest matches what the registry would
     compute on disk (so a freshly generated skill loads without
     content_hash_matched=False).
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import textwrap
from pathlib import Path

import pytest

from studiomind.learning import codegen
from studiomind.learning.codegen import (
    FitSpec,
    ParamSpec,
    SkillSpec,
    ValidationProbeSpec,
)


# ───────────────────────────── helpers ────────────────────────────────

def _spec_demo_plugin() -> SkillSpec:
    """A 3-param fake plugin: one linear continuous, one quadratic
    continuous, one enum."""
    return SkillSpec(
        plugin_name="Demo Plugin",
        skill_name="demo_plugin",
        tool_name="set_demo",
        fl_version="21.2.10",
        acquired_iso="2026-04-30T12:00:00+00:00",
        calibration_log_relpath="calibration-logs/2026-04-30T12-00-00.json",
        params=(
            ParamSpec(
                id=0,
                name="Threshold",
                kind="continuous",
                fit=FitSpec(shape="linear", params=(60.0, -60.0), r_squared=1.0),
                human_min=-60.0,
                human_max=0.0,
                unit="dB",
                validation_probes=(
                    ValidationProbeSpec(param_value=0.5, predicted=-30.0, actual=-30.0, ok=True),
                ),
            ),
            ParamSpec(
                id=1,
                name="Curve",
                kind="continuous",
                fit=FitSpec(shape="quadratic", params=(2.0, 1.0, 0.0), r_squared=0.999),
                human_min=0.0,
                human_max=3.0,
                unit="",
            ),
            ParamSpec(
                id=2,
                name="Style",
                kind="enum",
                enum_values={"hard": 0.0, "soft": 0.5, "transparent": 1.0},
            ),
        ),
    )


def _import_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ───────────────────────────── slug helpers ───────────────────────────

def test_default_skill_name() -> None:
    assert codegen.default_skill_name("Fruity Limiter") == "fruity_limiter"
    assert codegen.default_skill_name("Pro-Q 3") == "pro_q_3"
    assert codegen.default_skill_name("FabFilter Pro-Q 3") == "fabfilter_pro_q_3"
    assert codegen.default_skill_name("") == "param"  # fallback


def test_default_tool_name_drops_fruity_prefix() -> None:
    assert codegen.default_tool_name("Fruity Limiter") == "set_limiter"
    assert codegen.default_tool_name("FabFilter Pro-Q 3") == "set_fabfilter_pro_q_3"


def test_param_slug_lowercases_and_strips() -> None:
    p = ParamSpec(id=0, name="Some Wild Name!!", kind="enum", enum_values={"a": 0.0})
    assert p.slug() == "some_wild_name"


# ───────────────────────────── reproducibility ────────────────────────

def test_render_skill_is_byte_identical_across_runs() -> None:
    spec = _spec_demo_plugin()
    files_a = codegen.render_skill(spec)
    files_b = codegen.render_skill(spec)
    assert files_a == files_b
    assert sorted(files_a.keys()) == sorted(files_b.keys())


def test_render_skill_emits_all_required_files() -> None:
    files = codegen.render_skill(_spec_demo_plugin())
    expected = {"__init__.py", "manifest.json", "wrapper.py", "tool.py",
                "knowledge.md", "tests.py"}
    assert set(files.keys()) == expected


def test_manifest_content_hash_matches_registry_computation(tmp_path: Path) -> None:
    """When the rendered files land on disk, the registry's
    compute_content_hash must produce the same hash that's already in
    the manifest."""
    from studiomind.skills._registry import compute_content_hash

    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    skill_dir = tmp_path / spec.skill_name
    skill_dir.mkdir()
    for fname, body in files.items():
        (skill_dir / fname).write_text(body)
    manifest = json.loads(files["manifest.json"])
    assert manifest["content_hash"] == compute_content_hash(skill_dir)


# ───────────────────────────── generated wrapper math ─────────────────

def test_generated_wrapper_imports_and_round_trips_linear(tmp_path: Path) -> None:
    """Linear fit: param_to_threshold(0.5) == -30, threshold_to_param(-30) == 0.5."""
    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    wrapper_path = tmp_path / "demo_wrapper.py"
    wrapper_path.write_text(files["wrapper.py"])
    w = _import_module_from_path("demo_wrapper_linear", wrapper_path)

    assert w.param_to_threshold(0.5) == pytest.approx(-30.0, abs=1e-9)
    assert w.threshold_to_param(-30.0) == pytest.approx(0.5, abs=1e-9)
    # Clamping outside [-60, 0]:
    assert w.threshold_to_param(-100.0) == 0.0
    assert w.threshold_to_param(50.0) == 1.0


def test_generated_wrapper_round_trips_quadratic(tmp_path: Path) -> None:
    """Quadratic fit: 2*p² + p + 0 → at p=0.5, value = 2*0.25 + 0.5 = 1.0."""
    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    wrapper_path = tmp_path / "demo_wrapper_quad.py"
    wrapper_path.write_text(files["wrapper.py"])
    w = _import_module_from_path("demo_wrapper_quad", wrapper_path)

    assert w.param_to_curve(0.5) == pytest.approx(1.0, abs=1e-9)
    # Inverse: target value 1.0 should land at p≈0.5 (within the quadratic
    # formula's positive-root branch).
    p = w.curve_to_param(1.0)
    assert 0.0 <= p <= 1.0
    assert w.param_to_curve(p) == pytest.approx(1.0, abs=1e-6)


def test_generated_wrapper_handles_enum(tmp_path: Path) -> None:
    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    wrapper_path = tmp_path / "demo_wrapper_enum.py"
    wrapper_path.write_text(files["wrapper.py"])
    w = _import_module_from_path("demo_wrapper_enum", wrapper_path)

    assert w.style_to_param("hard") == 0.0
    assert w.style_to_param("Soft") == 0.5         # case-insensitive
    assert w.style_to_param("  TRANSPARENT  ") == 1.0
    assert w.param_to_style(0.5) == "soft"

    with pytest.raises(ValueError):
        w.style_to_param("bogus")


def test_generated_wrapper_build_commands_filters_unset(tmp_path: Path) -> None:
    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    wrapper_path = tmp_path / "demo_wrapper_build.py"
    wrapper_path.write_text(files["wrapper.py"])
    w = _import_module_from_path("demo_wrapper_build", wrapper_path)

    cmds = w.build_commands(track_id=4, slot=0, threshold=-12.0, style="soft")
    by_id = {c["param_id"]: c for c in cmds}
    assert set(by_id.keys()) == {w.PARAM_THRESHOLD, w.PARAM_STYLE}
    for c in cmds:
        assert c["track_id"] == 4
        assert c["slot"] == 0
        assert 0.0 <= c["value"] <= 1.0


# ───────────────────────────── generated tool.py ──────────────────────

def test_generated_tool_py_parses_and_has_expected_structure() -> None:
    """tool.py imports ``from studiomind.skills.<name> import wrapper``
    — that import only resolves once the skill lands under
    ``src/studiomind/skills/<name>/`` for real (covered by the P4-G
    integration test). At codegen-unit-test time we verify only the
    AST + structural shape: the TOOL spec is well-formed and the
    expected functions are defined."""
    import ast

    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    tree = ast.parse(files["tool.py"])
    function_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"build_commands_from_args", "execute", "description_from_args"} <= function_names

    # The TOOL dict's name + required list are visible as literals.
    src = files["tool.py"]
    assert "'set_demo'" in src or '"set_demo"' in src
    assert "track_id" in src
    assert "slot" in src


# ───────────────────────────── generated tests.py ─────────────────────

def test_generated_tests_pass_when_run(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: render_skill onto disk, run pytest on tests.py,
    expect green."""
    import subprocess

    spec = _spec_demo_plugin()
    files = codegen.render_skill(spec)
    skills_root = tmp_path / "studiomind" / "skills"
    skill_dir = skills_root / spec.skill_name
    skill_dir.mkdir(parents=True)
    for fname, body in files.items():
        (skill_dir / fname).write_text(body)
    (tmp_path / "studiomind" / "__init__.py").write_text("")
    (tmp_path / "studiomind" / "skills" / "__init__.py").write_text("")

    env = {
        **__import__("os").environ,
        "PYTHONPATH": f"{tmp_path}:{__import__('os').environ.get('PYTHONPATH', '')}",
        "PYTHONHASHSEED": "0",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(skill_dir / "tests.py"), "-q"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, (
        f"Generated tests failed:\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
    )


# ───────────────────────────── calibration log ───────────────────────

def test_calibration_log_render_is_stable() -> None:
    spec = _spec_demo_plugin()
    a = codegen.render_calibration_log(
        spec,
        started_iso="2026-04-30T12:00:00+00:00",
        finished_iso="2026-04-30T12:15:00+00:00",
    )
    b = codegen.render_calibration_log(
        spec,
        started_iso="2026-04-30T12:00:00+00:00",
        finished_iso="2026-04-30T12:15:00+00:00",
    )
    assert a == b
    parsed = json.loads(a)
    assert parsed["plugin"] == "Demo Plugin"
    assert parsed["fl_version"] == "21.2.10"
    assert len(parsed["params"]) == 3
