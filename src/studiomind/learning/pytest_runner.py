"""Subprocess-based pytest runner with hard timeout.

Used by training mode to verify a freshly-generated wrapper before
proposing the commit. Runs only the new skill's tests file (sandbox
enforces this), in a fresh subprocess (so the running web app's
process state isn't polluted), with a wall-clock timeout.

Design points (see docs/training-mode.md § "Sandbox rules"):

  * Subprocess only — no in-process pytest.main() that could leak
    fixtures / collect side effects.
  * Hard timeout (default 60 s) catches infinite loops in generated
    wrappers.
  * ``PYTHONHASHSEED=0`` for reproducibility across runs.
  * Returns a structured ``PytestResult``; never raises on test
    failure (only on sandbox violation or process error).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from studiomind.learning.sandbox import REPO_ROOT, assert_pytest_safe


@dataclass(frozen=True)
class PytestResult:
    skill_name: str
    passed: int
    failed: int
    errors: int
    duration_s: float
    timed_out: bool
    summary: str   # tail of pytest output for quick agent inspection
    returncode: int

    @property
    def all_passed(self) -> bool:
        return (
            not self.timed_out
            and self.returncode == 0
            and self.failed == 0
            and self.errors == 0
        )


_SUMMARY_RE_PASSED = re.compile(r"(\d+) passed")
_SUMMARY_RE_FAILED = re.compile(r"(\d+) failed")
_SUMMARY_RE_ERROR = re.compile(r"(\d+) error")


def _parse_summary(stdout: str) -> tuple[int, int, int]:
    """Walk pytest output bottom-up looking for the summary line."""
    passed = failed = errors = 0
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        if "passed" in line or "failed" in line or "error" in line:
            m = _SUMMARY_RE_PASSED.search(line)
            if m: passed = int(m.group(1))
            m = _SUMMARY_RE_FAILED.search(line)
            if m: failed = int(m.group(1))
            m = _SUMMARY_RE_ERROR.search(line)
            if m: errors = int(m.group(1))
            if passed or failed or errors:
                break
    return passed, failed, errors


def _build_env() -> dict[str, str]:
    """Minimal environment for the pytest subprocess."""
    env: dict[str, str] = {
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Inherit only what pytest legitimately needs
    for k in ("PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"):
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    return env


def run_pytest_for_skill(
    skill_name: str,
    *,
    timeout_s: float = 60.0,
) -> PytestResult:
    """Run pytest against a single skill's ``tests.py`` in a subprocess.

    The skill name is validated by ``assert_pytest_safe``. Hard timeout
    via ``subprocess.run(timeout=...)``. Returns a structured result;
    never raises on test failure."""
    tests_path = assert_pytest_safe(skill_name)

    if not tests_path.exists():
        try:
            display = str(tests_path.relative_to(REPO_ROOT))
        except ValueError:
            display = str(tests_path)
        return PytestResult(
            skill_name=skill_name,
            passed=0, failed=0, errors=0,
            duration_s=0.0, timed_out=False,
            summary=f"No tests.py at {display}",
            returncode=-1,
        )

    cmd = [
        sys.executable, "-m", "pytest",
        "-q", "--tb=short", "--no-header",
        str(tests_path),
    ]
    env = _build_env()
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, env=env,
            capture_output=True, text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return PytestResult(
            skill_name=skill_name,
            passed=0, failed=0, errors=0,
            duration_s=timeout_s, timed_out=True,
            summary=f"Pytest timed out after {timeout_s:.1f}s.",
            returncode=-2,
        )

    duration = time.monotonic() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    summary = out[-2000:]
    passed, failed, errors = _parse_summary(out)

    return PytestResult(
        skill_name=skill_name,
        passed=passed, failed=failed, errors=errors,
        duration_s=duration, timed_out=False,
        summary=summary,
        returncode=proc.returncode,
    )
