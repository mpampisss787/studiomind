"""Tests for the skill registry.

Exercises:
- Hermetic discovery (one skill = one directory)
- Schema-version validation (rejects too-new / too-old)
- Required-file presence
- Manifest field validation
- content_hash recompute on load (warns on mismatch, doesn't block)
- One bad skill doesn't poison the rest of the registry
- Hard cap (MAX_SKILLS)
- knowledge.md concatenation into a single prompt section
"""

from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from studiomind.skills import _registry as registry


# ─────────────────── helpers ───────────────────

def _write_skill(
    root: Path,
    name: str,
    *,
    schema_version: int = 1,
    type_: str = "plugin_wrapper",
    display_name: str | None = None,
    tool_name: str | None = None,
    fl_version: str = "21.2.10",
    acquired: str = "2026-04-30T15:22:08+03:00",
    extra_manifest: dict | None = None,
    wrapper_body: str = "VERSION = 1\n",
    tool_body: str = "TOOL = {'name': 'do_thing'}\n",
    knowledge_body: str = "# Skill\n\n## Use cases\nUse this thing.\n",
    tests_body: str = "def test_ok():\n    assert True\n",
    write_content_hash: bool = True,
    bad_content_hash: bool = False,
) -> Path:
    """Create a synthetic skill directory under ``root`` and return its path."""
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "__init__.py").write_text("")
    (skill_dir / "wrapper.py").write_text(wrapper_body)
    (skill_dir / "tool.py").write_text(tool_body)
    (skill_dir / "knowledge.md").write_text(knowledge_body)
    (skill_dir / "tests.py").write_text(tests_body)

    manifest = {
        "schema_version": schema_version,
        "name": name,
        "type": type_,
        "display_name": display_name or name.replace("_", " ").title(),
        "tool_name": tool_name or f"do_{name}",
        "fl_version": fl_version,
        "acquired": acquired,
        "params": [],
    }
    if extra_manifest:
        manifest.update(extra_manifest)

    if write_content_hash:
        # Write manifest first without content_hash, compute hash, then rewrite
        (skill_dir / "manifest.json").write_text(json.dumps(manifest))
        actual = registry.compute_content_hash(skill_dir)
        manifest["content_hash"] = "sha256:badbadbad" if bad_content_hash else actual

    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    return skill_dir


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """A temporary skills root + a fake studiomind.skills package
    rooted there so importlib can find the modules we write."""
    monkeypatch.setattr(registry, "SKILLS_DIR", tmp_path)

    # Create a fresh package namespace under a unique name so importlib
    # doesn't pick up cached state from a previous test.
    pkg_name = f"_test_skills_pkg_{tmp_path.name}"
    monkeypatch.setattr(registry, "SKILLS_PACKAGE", pkg_name)

    # Register the package: studiomind.skills.<pkg_name> resolves to tmp_path
    import importlib
    import types

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(tmp_path)]
    sys.modules[pkg_name] = pkg

    # Ensure cleanup of any submodules we import into sys.modules
    yield tmp_path

    for mod_name in list(sys.modules):
        if mod_name.startswith(f"{pkg_name}."):
            del sys.modules[mod_name]
    sys.modules.pop(pkg_name, None)


# ─────────────────── discovery ───────────────────

def test_discover_finds_skills_alphabetically(skills_root) -> None:
    _write_skill(skills_root, "fruity_compressor")
    _write_skill(skills_root, "fabfilter_proq3")
    _write_skill(skills_root, "fruity_limiter")
    dirs = registry.discover_skill_dirs(skills_root)
    names = [d.name for d in dirs]
    assert names == ["fabfilter_proq3", "fruity_compressor", "fruity_limiter"]


def test_discover_skips_underscore_prefixed_dirs(skills_root) -> None:
    (skills_root / "_migrations").mkdir()
    (skills_root / ".hidden").mkdir()
    _write_skill(skills_root, "real_skill")
    dirs = registry.discover_skill_dirs(skills_root)
    assert [d.name for d in dirs] == ["real_skill"]


def test_discover_handles_missing_root_gracefully(tmp_path, monkeypatch) -> None:
    nonexistent = tmp_path / "not_there"
    assert registry.discover_skill_dirs(nonexistent) == []


# ─────────────────── content hashing ───────────────────

def test_content_hash_is_deterministic(skills_root) -> None:
    _write_skill(skills_root, "skill_a")
    h1 = registry.compute_content_hash(skills_root / "skill_a")
    h2 = registry.compute_content_hash(skills_root / "skill_a")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_content_hash_differs_on_wrapper_change(skills_root) -> None:
    _write_skill(skills_root, "skill_a", wrapper_body="VERSION = 1\n")
    h1 = registry.compute_content_hash(skills_root / "skill_a")
    (skills_root / "skill_a" / "wrapper.py").write_text("VERSION = 2\n")
    h2 = registry.compute_content_hash(skills_root / "skill_a")
    assert h1 != h2


def test_content_hash_excludes_content_hash_field(skills_root) -> None:
    """The hash should be the same before and after we record it in the
    manifest — otherwise it would be self-referential."""
    skill_dir = _write_skill(skills_root, "skill_a", write_content_hash=False)
    manifest = json.loads((skill_dir / "manifest.json").read_text())
    h1 = registry.compute_content_hash(skill_dir)
    manifest["content_hash"] = h1
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    h2 = registry.compute_content_hash(skill_dir)
    assert h1 == h2


# ─────────────────── load_skill ───────────────────

def test_load_skill_succeeds_on_canonical(skills_root) -> None:
    _write_skill(skills_root, "fabfilter_proq3", display_name="FabFilter Pro-Q 3", tool_name="set_proq3")
    skill = registry.load_skill(skills_root / "fabfilter_proq3")
    assert skill.name == "fabfilter_proq3"
    assert skill.display_name == "FabFilter Pro-Q 3"
    assert skill.tool_name == "set_proq3"
    assert skill.schema_version == 1
    assert skill.content_hash_matched is True
    assert "Use cases" in skill.knowledge
    assert hasattr(skill.wrapper, "VERSION")
    assert hasattr(skill.tool, "TOOL")


def test_load_skill_warns_on_content_hash_mismatch(skills_root, caplog) -> None:
    _write_skill(skills_root, "tampered", bad_content_hash=True)
    with caplog.at_level("WARNING"):
        skill = registry.load_skill(skills_root / "tampered")
    assert skill.content_hash_matched is False
    assert "content_hash mismatch" in caplog.text


def test_load_skill_rejects_unsupported_schema_version(skills_root) -> None:
    _write_skill(skills_root, "future", schema_version=99)
    with pytest.raises(registry.SkillLoadError, match="schema_version"):
        registry.load_skill(skills_root / "future")


def test_load_skill_rejects_zero_schema_version(skills_root) -> None:
    _write_skill(skills_root, "ancient", schema_version=0)
    with pytest.raises(registry.SkillLoadError):
        registry.load_skill(skills_root / "ancient")


@pytest.mark.parametrize("missing_file", ["wrapper.py", "tool.py", "knowledge.md", "tests.py", "manifest.json"])
def test_load_skill_rejects_missing_required_file(skills_root, missing_file) -> None:
    skill_dir = _write_skill(skills_root, "incomplete")
    (skill_dir / missing_file).unlink()
    with pytest.raises(registry.SkillLoadError, match=missing_file):
        registry.load_skill(skill_dir)


def test_load_skill_rejects_invalid_json(skills_root) -> None:
    skill_dir = _write_skill(skills_root, "bad_json")
    (skill_dir / "manifest.json").write_text("{not valid json")
    with pytest.raises(registry.SkillLoadError, match="not valid JSON"):
        registry.load_skill(skill_dir)


def test_load_skill_rejects_manifest_name_mismatch(skills_root) -> None:
    _write_skill(skills_root, "right_name", extra_manifest={"name": "wrong_name"})
    # Need to recompute content_hash after we tampered with manifest...
    # Actually _write_skill computed it BEFORE applying extra_manifest.
    # Re-check load — it should fail on name mismatch regardless of hash.
    with pytest.raises(registry.SkillLoadError, match="does not match directory"):
        registry.load_skill(skills_root / "right_name")


def test_load_skill_rejects_unsupported_type(skills_root) -> None:
    _write_skill(skills_root, "tech", type_="mixing_technique")
    with pytest.raises(registry.SkillLoadError, match="unsupported skill type"):
        registry.load_skill(skills_root / "tech")


def test_load_skill_rejects_invalid_directory_name(skills_root) -> None:
    bad = skills_root / "Bad-Name"
    bad.mkdir()
    with pytest.raises(registry.SkillLoadError, match="invalid directory name"):
        registry.load_skill(bad)


@pytest.mark.parametrize("missing_field", [
    "schema_version", "name", "type", "display_name",
    "tool_name", "fl_version", "acquired", "content_hash", "params",
])
def test_load_skill_rejects_missing_manifest_field(skills_root, missing_field) -> None:
    skill_dir = _write_skill(skills_root, "incomplete_manifest")
    manifest = json.loads((skill_dir / "manifest.json").read_text())
    manifest.pop(missing_field)
    (skill_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(registry.SkillLoadError, match=missing_field):
        registry.load_skill(skill_dir)


# ─────────────────── load_all_skills ───────────────────

def test_load_all_returns_valid_and_collects_errors(skills_root) -> None:
    _write_skill(skills_root, "good_one")
    _write_skill(skills_root, "good_two")
    bad_dir = _write_skill(skills_root, "bad_one")
    (bad_dir / "manifest.json").write_text("{ bad json")

    skills, errors = registry.load_all_skills(skills_root)
    names = [s.name for s in skills]
    assert "good_one" in names
    assert "good_two" in names
    assert ("bad_one", pytest.approx) is None or any(name == "bad_one" for name, _ in errors)
    assert len(errors) == 1
    assert errors[0][0] == "bad_one"


def test_load_all_respects_max_skills(skills_root) -> None:
    for i in range(5):
        _write_skill(skills_root, f"skill_{i}")
    skills, errors = registry.load_all_skills(skills_root, max_skills=3)
    assert len(skills) == 3
    assert errors == []


def test_load_all_handles_empty_root(skills_root) -> None:
    skills, errors = registry.load_all_skills(skills_root)
    assert skills == []
    assert errors == []


# ─────────────────── knowledge concatenation ───────────────────

def test_build_knowledge_section_concatenates_in_order(skills_root) -> None:
    _write_skill(skills_root, "a_first", display_name="A First", knowledge_body="# A\n\nFirst skill.\n")
    _write_skill(skills_root, "z_last", display_name="Z Last", knowledge_body="# Z\n\nLast skill.\n")
    skills, _ = registry.load_all_skills(skills_root)
    section = registry.build_knowledge_section(skills)
    assert "# Acquired skills" in section
    # Sorted alphabetically by name
    a_pos = section.index("A First")
    z_pos = section.index("Z Last")
    assert a_pos < z_pos
    assert "First skill." in section
    assert "Last skill." in section


def test_build_knowledge_section_empty_when_no_skills() -> None:
    assert registry.build_knowledge_section([]) == ""
