"""Skill registry — discovers, validates, and loads skill directories.

Walks ``src/studiomind/skills/*/`` at session start, reads each
``manifest.json``, validates schema_version, imports the wrapper +
tool modules, and yields ``Skill`` objects.

Robustness invariants enforced here:

  * **Hermetic discovery.** Each skill is one directory. Adding a skill
    is `git add`; removing is `rm -rf`. No shared-file edits.
  * **Schema-versioned.** Manifests carry ``schema_version``. The loader
    refuses skills outside the supported range (currently {1}).
  * **Tamper-detection.** Each manifest carries a ``content_hash``.
    The loader recomputes it on load and warns on mismatch (does not
    block — hand edits are sometimes intentional during migrations).
  * **Hard cap.** No more than ``MAX_SKILLS`` (default 50) loaded per
    session, to keep mixing prompt size predictable.

The registry is read-only at runtime: training mode writes new skill
directories to disk, but the running mixing-agent process does not
hot-reload — see docs/training-mode.md § "Mode exclusivity".
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

log = logging.getLogger(__name__)

SKILLS_PACKAGE = "studiomind.skills"
SKILLS_DIR: Path = Path(__file__).resolve().parent

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
MAX_SKILLS: int = 50

_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUIRED_MANIFEST_FIELDS = (
    "schema_version", "name", "type", "display_name",
    "tool_name", "fl_version", "acquired", "content_hash", "params",
)
_REQUIRED_FILES = ("manifest.json", "wrapper.py", "tool.py", "knowledge.md", "tests.py")


class SkillLoadError(Exception):
    """Raised when a skill directory is malformed enough that it can't load."""


@dataclass(frozen=True)
class Skill:
    """A loaded skill: manifest + module references + knowledge text."""

    name: str
    display_name: str
    tool_name: str
    fl_version: str
    schema_version: int
    manifest: dict[str, Any]
    wrapper: ModuleType
    tool: ModuleType
    knowledge: str
    skill_dir: Path
    content_hash_matched: bool


# ─────────────────── content hashing ───────────────────

def compute_content_hash(skill_dir: Path) -> str:
    """SHA-256 over canonicalized manifest (minus its hash field) +
    wrapper.py + tool.py + knowledge.md, in that order. Two
    acquisitions of the same plugin on the same FL version with
    matching readbacks should produce the same hash."""
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_for_hash = {k: v for k, v in manifest.items() if k != "content_hash"}
    canonical_manifest = json.dumps(manifest_for_hash, sort_keys=True, separators=(",", ":"))

    h = hashlib.sha256()
    h.update(canonical_manifest.encode("utf-8"))
    for filename in ("wrapper.py", "tool.py", "knowledge.md"):
        path = skill_dir / filename
        h.update(b"\x1e")  # record separator between files
        h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


# ─────────────────── manifest validation ───────────────────

def _validate_manifest(manifest: dict[str, Any], skill_dir: Path) -> None:
    """Raise SkillLoadError if the manifest is missing required fields
    or has a wrong type for any."""
    for field in _REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            raise SkillLoadError(f"{skill_dir.name}: manifest missing field '{field}'")

    if not isinstance(manifest["schema_version"], int):
        raise SkillLoadError(f"{skill_dir.name}: schema_version must be int")

    if manifest["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise SkillLoadError(
            f"{skill_dir.name}: unsupported schema_version "
            f"{manifest['schema_version']!r} (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    if manifest["name"] != skill_dir.name:
        raise SkillLoadError(
            f"{skill_dir.name}: manifest name {manifest['name']!r} "
            f"does not match directory name {skill_dir.name!r}"
        )

    if not _SKILL_NAME_RE.fullmatch(manifest["name"]):
        raise SkillLoadError(f"{skill_dir.name}: invalid skill name format")

    if manifest["type"] not in ("plugin_wrapper",):
        # v1: only plugin_wrapper. v2 may add 'mixing_technique' etc.
        raise SkillLoadError(
            f"{skill_dir.name}: unsupported skill type {manifest['type']!r}"
        )

    if not isinstance(manifest["tool_name"], str) or not manifest["tool_name"]:
        raise SkillLoadError(f"{skill_dir.name}: tool_name must be non-empty string")

    if not isinstance(manifest["params"], list):
        raise SkillLoadError(f"{skill_dir.name}: params must be a list")


def _check_required_files(skill_dir: Path) -> None:
    """Raise SkillLoadError if any of the five required files is
    missing from the skill directory."""
    for filename in _REQUIRED_FILES:
        if not (skill_dir / filename).is_file():
            raise SkillLoadError(f"{skill_dir.name}: missing required file '{filename}'")


# ─────────────────── module imports ───────────────────

def _import_skill_submodule(skill_name: str, submodule: str) -> ModuleType:
    """Import e.g. ``studiomind.skills.fruity_compressor.wrapper`` as a
    real Python module. Caching is deferred to importlib's machinery —
    re-importing returns the cached object."""
    qualname = f"{SKILLS_PACKAGE}.{skill_name}.{submodule}"
    return importlib.import_module(qualname)


# ─────────────────── public API ───────────────────

def load_skill(skill_dir: Path) -> Skill:
    """Load and validate a single skill directory. Raises SkillLoadError."""
    if not skill_dir.is_dir():
        raise SkillLoadError(f"{skill_dir}: not a directory")

    if not _SKILL_NAME_RE.fullmatch(skill_dir.name):
        raise SkillLoadError(f"{skill_dir.name}: invalid directory name")

    _check_required_files(skill_dir)

    manifest_path = skill_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        raise SkillLoadError(f"{skill_dir.name}: manifest.json is not valid JSON: {e}") from e

    _validate_manifest(manifest, skill_dir)

    # Tamper detection — don't block load, but log a clear warning.
    recorded_hash = manifest["content_hash"]
    actual_hash = compute_content_hash(skill_dir)
    hash_matched = (recorded_hash == actual_hash)
    if not hash_matched:
        log.warning(
            "Skill %s content_hash mismatch: manifest=%s actual=%s. "
            "Hand edit since acquisition?",
            skill_dir.name, recorded_hash, actual_hash,
        )

    knowledge = (skill_dir / "knowledge.md").read_text()

    try:
        wrapper = _import_skill_submodule(skill_dir.name, "wrapper")
        tool = _import_skill_submodule(skill_dir.name, "tool")
    except Exception as e:
        raise SkillLoadError(f"{skill_dir.name}: import failed: {e}") from e

    return Skill(
        name=manifest["name"],
        display_name=manifest["display_name"],
        tool_name=manifest["tool_name"],
        fl_version=manifest["fl_version"],
        schema_version=manifest["schema_version"],
        manifest=manifest,
        wrapper=wrapper,
        tool=tool,
        knowledge=knowledge,
        skill_dir=skill_dir,
        content_hash_matched=hash_matched,
    )


def discover_skill_dirs(skills_root: Path | None = None) -> list[Path]:
    """Return all candidate skill subdirectories, sorted alphabetically.
    Hidden dirs (``_*``, ``.``-prefixed) are skipped — those are
    reserved for the registry / migrations / private state."""
    root = skills_root or SKILLS_DIR
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        out.append(entry)
    return out


def load_all_skills(
    skills_root: Path | None = None,
    *,
    max_skills: int = MAX_SKILLS,
) -> tuple[list[Skill], list[tuple[str, str]]]:
    """Load every valid skill. Returns ``(skills, errors)`` where
    ``errors`` is a list of ``(skill_dir_name, error_message)`` pairs
    — the loader continues past individual skill failures so one bad
    manifest doesn't take down the whole agent."""
    skill_dirs = discover_skill_dirs(skills_root)
    if len(skill_dirs) > max_skills:
        log.warning(
            "Found %d skill directories; loading first %d (MAX_SKILLS).",
            len(skill_dirs), max_skills,
        )
        skill_dirs = skill_dirs[:max_skills]

    skills: list[Skill] = []
    errors: list[tuple[str, str]] = []
    for skill_dir in skill_dirs:
        try:
            skills.append(load_skill(skill_dir))
        except SkillLoadError as e:
            errors.append((skill_dir.name, str(e)))
            log.error("Skill %s failed to load: %s", skill_dir.name, e)
        except Exception as e:  # noqa: BLE001 — defensive
            errors.append((skill_dir.name, f"unexpected error: {e}"))
            log.exception("Skill %s raised unexpected error", skill_dir.name)
    return skills, errors


def build_knowledge_section(skills: list[Skill]) -> str:
    """Concatenate every loaded skill's knowledge.md body into a single
    'Skills' section that the mixing prompt builder can append."""
    if not skills:
        return ""
    parts = ["# Acquired skills\n"]
    for s in sorted(skills, key=lambda x: x.name):
        parts.append(f"\n## {s.display_name}\n")
        parts.append(s.knowledge.strip())
        parts.append("\n")
    return "\n".join(parts)
