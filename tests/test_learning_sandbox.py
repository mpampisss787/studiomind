"""Tests for the training-mode sandbox.

Every fail-closed assertion is exercised. New write/git/pytest tools
that the training agent grows over time must route through these
checks; if a future tool bypasses them, code review catches it.
Anything not explicitly allowed must be rejected here first.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from studiomind.learning import sandbox


# ─────────────────── path: writable ───────────────────

def test_writable_accepts_canonical_skill_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    skill_dir = tmp_path / "fruity_limiter"
    skill_dir.mkdir()
    for name in ("wrapper.py", "tests.py", "tool.py", "knowledge.md", "manifest.json"):
        target = skill_dir / name
        resolved = sandbox.assert_path_writable(str(target), current_skill="fruity_limiter")
        assert resolved == target.resolve()


def test_writable_accepts_nested_calibration_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    skill_dir = tmp_path / "fruity_limiter"
    (skill_dir / "calibration-logs").mkdir(parents=True)
    target = skill_dir / "calibration-logs" / "2026-04-30T15-22-08.json"
    resolved = sandbox.assert_path_writable(str(target), current_skill="fruity_limiter")
    assert resolved == target.resolve()


def test_writable_rejects_other_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    (tmp_path / "fruity_limiter").mkdir()
    (tmp_path / "fruity_compressor").mkdir()
    bad = tmp_path / "fruity_compressor" / "wrapper.py"
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_writable(str(bad), current_skill="fruity_limiter")


def test_writable_rejects_dot_dot_escape(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    (tmp_path / "fruity_limiter").mkdir()
    bad = tmp_path / "fruity_limiter" / ".." / "secret.py"
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_writable(str(bad), current_skill="fruity_limiter")


def test_writable_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    """A malicious symlink inside the skill directory pointing at a
    path outside the skill root must be rejected."""
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    skill_dir = tmp_path / "fruity_limiter"
    skill_dir.mkdir()
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    link = skill_dir / "evil"
    os.symlink(outside, link)
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_writable(str(link / "x.py"), current_skill="fruity_limiter")


def test_writable_rejects_outside_skills_dir_entirely(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    bad = tmp_path.parent / "rogue.py"
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_writable(str(bad), current_skill="fruity_limiter")


def test_writable_rejects_invalid_skill_names() -> None:
    for bad in ("", "Bad-Name", "with space", "Capital", "1leading_digit",
                "../escape", "..", "."):
        with pytest.raises(sandbox.SandboxViolation):
            sandbox.assert_path_writable(
                "/tmp/anything", current_skill=bad,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("forbidden", [
    "/home/babis/.git/config",
    "/home/babis/.ssh/id_rsa",
    "/home/babis/.config/something",
    "/home/babis/obsidian-vault/Projects/StudioMind.md",
])
def test_writable_rejects_forbidden_substring_paths(forbidden, tmp_path, monkeypatch) -> None:
    """Even within the skill dir, paths that resolve to a sensitive
    substring (via symlink trickery in a future world) must be blocked.
    Belt and suspenders."""
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    (tmp_path / "fruity_limiter").mkdir()
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_writable(forbidden, current_skill="fruity_limiter")


# ─────────────────── path: readable ───────────────────

def test_readable_accepts_repo_paths() -> None:
    # The pyproject is in the repo, must be readable
    assert sandbox.assert_path_readable(str(sandbox.REPO_ROOT / "pyproject.toml"))


def test_readable_rejects_paths_outside_repo() -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_path_readable("/etc/passwd")


def test_readable_rejects_dotfile_homes() -> None:
    for bad in ("/home/babis/.ssh/id_rsa", "/home/babis/.git/config"):
        with pytest.raises(sandbox.SandboxViolation):
            sandbox.assert_path_readable(bad)


# ─────────────────── git: allowlist ───────────────────

@pytest.mark.parametrize("args", [
    ["status"],
    ["status", "--porcelain"],
    ["diff"],
    ["diff", "--staged"],
    ["diff", "--cached"],
    ["log"],
    ["log", "--oneline"],
    ["log", "--oneline", "-10"],
    ["rev-parse", "HEAD"],
    ["config", "--get", "user.name"],
    ["config", "--get", "user.email"],
])
def test_git_allowlist_accepts_read_ops(args) -> None:
    sandbox.assert_safe_git(args)  # should not raise


@pytest.mark.parametrize("args", [
    ["push"], ["push", "origin", "main"],
    ["pull"], ["fetch"], ["clone"],
    ["rebase", "main"], ["reset", "--hard", "HEAD"],
    ["checkout", "main"], ["restore", "."],
    ["revert", "HEAD"], ["merge", "main"],
    ["cherry-pick", "abc"], ["branch", "-D", "old"],
    ["tag", "v1"], ["switch", "main"],
    ["clean", "-fd"], ["gc"], ["prune"], ["reflog"],
    ["remote", "add", "origin", "url"],
])
def test_git_allowlist_rejects_dangerous_subcommands(args) -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(args)


@pytest.mark.parametrize("args", [
    ["status", "--force"],
    ["commit", "-m", "x", "--amend"],
    ["commit", "-m", "x", "--no-verify"],
    ["commit", "-m", "x", "--no-gpg-sign"],
    ["log", "-A"],
    ["add", "--all"],
    ["log", "-i"],
])
def test_git_allowlist_rejects_dangerous_flags(args) -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(args, current_skill="fruity_limiter")


def test_git_status_rejects_unexpected_args() -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["status", "--porcelain", "--branch"])


def test_git_diff_rejects_unexpected_args() -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["diff", "--name-only"])


def test_git_config_rejects_writes() -> None:
    for args in (
        ["config", "--global", "user.name", "x"],
        ["config", "user.name", "x"],
        ["config", "--unset", "user.name"],
        ["config", "--get", "remote.origin.url"],
    ):
        with pytest.raises(sandbox.SandboxViolation):
            sandbox.assert_safe_git(args)


def test_git_add_requires_skill_context_and_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    (tmp_path / "fruity_limiter").mkdir()

    # No paths
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["add"], current_skill="fruity_limiter")

    # No skill context
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["add", str(tmp_path / "fruity_limiter" / "wrapper.py")])

    # Bulk forms
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["add", "."], current_skill="fruity_limiter")
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["add", "-A"], current_skill="fruity_limiter")

    # Path outside skill
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(
            ["add", str(tmp_path.parent / "rogue.py")],
            current_skill="fruity_limiter",
        )


def test_git_add_accepts_paths_within_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    skill_dir = tmp_path / "fruity_limiter"
    skill_dir.mkdir()
    (skill_dir / "wrapper.py").write_text("# stub")
    sandbox.assert_safe_git(
        ["add", str(skill_dir / "wrapper.py")],
        current_skill="fruity_limiter",
    )


def test_git_commit_strict_form() -> None:
    sandbox.assert_safe_git(["commit", "-m", "Add fruity_limiter"])
    for bad in (
        ["commit"],
        ["commit", "-m"],
        ["commit", "-am", "x"],
        ["commit", "-m", "x", "--amend"],
        ["commit", "-m", "x", "extra"],
        ["commit", "--message=x"],
    ):
        with pytest.raises(sandbox.SandboxViolation):
            sandbox.assert_safe_git(bad)


def test_git_empty_args_rejected() -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git([])


def test_git_unknown_subcommand_rejected() -> None:
    with pytest.raises(sandbox.SandboxViolation):
        sandbox.assert_safe_git(["foobar"])


# ─────────────────── pytest target ───────────────────

def test_pytest_safe_accepts_valid_skill_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    skill_dir = tmp_path / "fruity_limiter"
    skill_dir.mkdir()
    p = sandbox.assert_pytest_safe("fruity_limiter")
    assert p == (skill_dir / "tests.py").resolve()


def test_pytest_safe_accepts_skill_with_no_tests_yet(tmp_path, monkeypatch) -> None:
    """Tests file may not exist yet (skill being acquired). The path is
    still validated, just doesn't exist."""
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    p = sandbox.assert_pytest_safe("fruity_limiter")
    assert not p.exists()
    assert p.parent.name == "fruity_limiter"


def test_pytest_safe_rejects_invalid_names() -> None:
    for bad in ("", "Bad", "with space", "..", "../escape", "1bad"):
        with pytest.raises(sandbox.SandboxViolation):
            sandbox.assert_pytest_safe(bad)
