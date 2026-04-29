"""CLI smoke tests for the `studiomind train` subcommand."""

from __future__ import annotations

import subprocess
import sys


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "studiomind", *args],
        capture_output=True, text=True, timeout=30,
        cwd="/home/babis/studiomind",
        env={"PYTHONPATH": "/home/babis/studiomind/src", "PATH": "/usr/bin:/bin"},
    )


def test_train_help_lists_required_flags() -> None:
    proc = _run_cli("train", "--help")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "--track" in out
    assert "--slot" in out
    assert "--dry-run" in out
    assert "--resume" in out


def test_train_dry_run_prints_plan() -> None:
    proc = _run_cli(
        "train", "Fruity Limiter",
        "--track", "5", "--slot", "0",
        "--dry-run",
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    out = proc.stdout
    assert "Fruity Limiter" in out
    assert "fruity_limiter" in out      # default skill_name
    assert "set_limiter" in out         # default tool_name
    assert "Wizard plan:" in out
    assert "enumerate_plugin_params" in out
    assert "validate_param" in out
    assert "never pushes" in out


def test_train_missing_plugin_name_errors() -> None:
    proc = _run_cli("train", "--track", "5", "--slot", "0")
    assert proc.returncode != 0
    assert "plugin_name" in (proc.stderr + proc.stdout)


def test_train_dry_run_with_overrides() -> None:
    proc = _run_cli(
        "train", "Custom Plugin",
        "--track", "1", "--slot", "2",
        "--skill-name", "my_custom",
        "--tool-name", "set_custom",
        "--fl-version", "22.0.0",
        "--dry-run",
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "skill_name = my_custom" in out
    assert "tool_name  = set_custom" in out
    assert "fl_version = 22.0.0" in out
