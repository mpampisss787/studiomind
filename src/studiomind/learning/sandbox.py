"""Single-source enforcement for filesystem and git operations performed
by training mode.

ALL training-mode tools that touch disk, git, or run tests MUST route
through this module. The assertions are fail-closed — anything not
explicitly allowed raises ``SandboxViolation``.

Three guarantees:

1. **Path writes** are limited to one skill directory at a time (the
   skill currently being acquired). Writes outside that directory —
   even with ``..`` traversal or symlinks — are rejected.
2. **Git commands** are limited to a small allowlist (read + commit
   only). No push, no rebase, no force, no global config writes.
3. **Pytest invocations** target exactly one skill's tests file.

These are not advisory. Every write tool in ``learning_tools.py``
must route through these checks; bypass attempts fail code review
(single-source rule, see docs/training-mode.md § "Sandbox rules").
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence


# Resolve the studiomind repo root from this file's location:
# /home/<user>/studiomind/src/studiomind/learning/sandbox.py
#  └─ four parents up = repo root
REPO_ROOT: Path = Path(__file__).resolve().parents[3]
SKILLS_DIR: Path = REPO_ROOT / "src" / "studiomind" / "skills"

# Skill-name format: lowercase alphanum + underscore, must start with letter.
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Substrings that, if they appear in a resolved path, are always rejected.
# Belt-and-suspenders against malicious symlinks pointing at sensitive dirs.
_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "/.git/",
    "/.claude/",
    "/.ssh/",
    "/.config/",
    "/obsidian-vault/",
    "/.gnupg/",
    "/.aws/",
)


class SandboxViolation(Exception):
    """Raised when training-mode code attempts an action outside the sandbox."""


# ─────────────────── helpers ───────────────────

def _resolve(path: str | Path) -> Path:
    """Return the fully-resolved (symlinks followed) absolute Path."""
    return Path(path).expanduser().resolve(strict=False)


def _check_forbidden(resolved: Path) -> None:
    s = str(resolved)
    for forb in _FORBIDDEN_SUBSTRINGS:
        if forb in s:
            raise SandboxViolation(f"Path resolves into forbidden directory ({forb}): {resolved}")


def _validate_skill_name(name: str) -> None:
    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        raise SandboxViolation(f"Invalid skill name: {name!r}")


# ─────────────────── path enforcement ───────────────────

def assert_path_writable(path: str | Path, *, current_skill: str) -> Path:
    """Reject every write that isn't inside
    ``src/studiomind/skills/<current_skill>/``.

    Returns the resolved path on success. Raises ``SandboxViolation``
    otherwise."""
    _validate_skill_name(current_skill)
    resolved = _resolve(path)
    skill_root = (SKILLS_DIR / current_skill).resolve()
    try:
        resolved.relative_to(skill_root)
    except ValueError:
        raise SandboxViolation(
            f"Write {resolved} is not inside skill directory {skill_root}"
        ) from None
    _check_forbidden(resolved)
    return resolved


def assert_path_readable(path: str | Path) -> Path:
    """Reject reads that escape the repo working tree or hit sensitive dirs.

    Reading is more permissive than writing: any file under the repo
    root is fine. ``.git/``, vault, dotfile homes are still blocked."""
    resolved = _resolve(path)
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        raise SandboxViolation(
            f"Read {resolved} is outside the repo root {REPO_ROOT}"
        ) from None
    _check_forbidden(resolved)
    return resolved


# ─────────────────── git enforcement ───────────────────

# Subcommands that are categorically rejected. These are network ops,
# history rewrites, or HEAD movements that change shared state.
_GIT_FORBIDDEN_SUBCOMMANDS: frozenset[str] = frozenset({
    "push", "fetch", "pull", "clone",
    "rebase", "reset", "checkout", "restore", "revert",
    "merge", "cherry-pick",
    "branch", "tag", "switch",
    "clean", "gc", "prune", "reflog",
    "remote", "submodule",
})

# Flags that are rejected regardless of subcommand.
_GIT_FORBIDDEN_FLAGS: frozenset[str] = frozenset({
    "--force", "-f",
    "--hard", "--soft", "--mixed",
    "--amend",
    "--no-verify", "--no-gpg-sign",
    "-i", "--interactive",
    "-A", "--all",
})


def assert_safe_git(args: Sequence[str], *, current_skill: str | None = None) -> None:
    """Validate a git invocation. Training-mode git tools call this
    before running any subprocess.

    **Allowed shapes:**
      - ``git status [--porcelain]``
      - ``git diff [--staged | --cached]``
      - ``git log [--oneline] [-N]``
      - ``git rev-parse HEAD``
      - ``git config --get user.{name,email}``  (read-only)
      - ``git add <path>...``  (paths must pass ``assert_path_writable``)
      - ``git commit -m <message>``  (no other forms)

    **Always rejected** — see ``_GIT_FORBIDDEN_SUBCOMMANDS`` /
    ``_GIT_FORBIDDEN_FLAGS``.
    """
    if not args:
        raise SandboxViolation("Empty git args")
    cmd = args[0]
    rest = list(args[1:])

    # Forbidden flags reject regardless of subcommand
    for a in rest:
        if a in _GIT_FORBIDDEN_FLAGS:
            raise SandboxViolation(f"Disallowed git flag: {a}")

    if cmd in _GIT_FORBIDDEN_SUBCOMMANDS:
        raise SandboxViolation(f"Disallowed git subcommand: {cmd}")

    if cmd == "status":
        if rest and rest != ["--porcelain"]:
            raise SandboxViolation(f"git status: unexpected args {rest}")
        return

    if cmd == "diff":
        if rest and rest not in (["--staged"], ["--cached"]):
            raise SandboxViolation(f"git diff: unexpected args {rest}")
        return

    if cmd == "log":
        for a in rest:
            if a == "--oneline":
                continue
            if re.fullmatch(r"-\d+", a):
                continue
            raise SandboxViolation(f"git log: unexpected arg {a}")
        return

    if cmd == "rev-parse":
        if rest != ["HEAD"]:
            raise SandboxViolation(f"git rev-parse: unexpected args {rest}")
        return

    if cmd == "config":
        if rest != ["--get", "user.name"] and rest != ["--get", "user.email"]:
            raise SandboxViolation(f"git config: only read-only --get user.{{name,email}}")
        return

    if cmd == "add":
        if not rest:
            raise SandboxViolation("git add requires explicit paths")
        if "." in rest:
            raise SandboxViolation("git add: bulk form (.) disallowed")
        if current_skill is None:
            raise SandboxViolation("git add requires current_skill context")
        for path in rest:
            if path.startswith("-"):
                raise SandboxViolation(f"git add: unrecognised flag {path}")
            assert_path_writable(path, current_skill=current_skill)
        return

    if cmd == "commit":
        if len(rest) < 2 or rest[0] != "-m":
            raise SandboxViolation("git commit: require -m <message>, no other forms")
        if len(rest) > 2:
            raise SandboxViolation(f"git commit: unexpected extra args {rest[2:]}")
        return

    raise SandboxViolation(f"git command not in allowlist: {cmd}")


# ─────────────────── pytest enforcement ───────────────────

def assert_pytest_safe(skill_name: str) -> Path:
    """Validate a pytest invocation. Must target exactly one skill's
    tests file. Returns the resolved path."""
    _validate_skill_name(skill_name)
    tests_path = (SKILLS_DIR / skill_name / "tests.py").resolve()
    skills_root = SKILLS_DIR.resolve()
    try:
        tests_path.relative_to(skills_root)
    except ValueError:
        raise SandboxViolation(f"Tests path {tests_path} escapes skills/ root")
    _check_forbidden(tests_path)
    return tests_path
