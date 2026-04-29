"""Tests for the subprocess pytest runner used by training mode.

Covers the structured-result contract, hard timeout, sandbox-rejected
inputs, and behavior on tests that pass / fail / error / hang.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from studiomind.learning import pytest_runner, sandbox


@pytest.fixture
def fake_skill(tmp_path, monkeypatch):
    """Create a one-skill scaffold under a tmp skills dir, returning
    the skill_dir path. Tests write tests.py into that dir."""
    monkeypatch.setattr(sandbox, "SKILLS_DIR", tmp_path)
    monkeypatch.setattr(pytest_runner, "REPO_ROOT", sandbox.REPO_ROOT)
    skill_dir = tmp_path / "fruity_limiter"
    skill_dir.mkdir()
    return skill_dir


def test_runner_reports_no_tests_when_file_missing(fake_skill) -> None:
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert result.passed == 0
    assert result.failed == 0
    assert result.timed_out is False
    assert "No tests.py" in result.summary


def test_runner_reports_pass_count(fake_skill) -> None:
    (fake_skill / "tests.py").write_text(textwrap.dedent("""
        def test_one():
            assert 1 + 1 == 2

        def test_two():
            assert "studiomind".startswith("studio")
    """).lstrip())
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert result.timed_out is False
    assert result.passed == 2
    assert result.failed == 0
    assert result.errors == 0
    assert result.all_passed is True
    assert result.returncode == 0


def test_runner_reports_failure_without_raising(fake_skill) -> None:
    (fake_skill / "tests.py").write_text(textwrap.dedent("""
        def test_passes():
            assert True

        def test_fails():
            assert 1 == 2
    """).lstrip())
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert result.timed_out is False
    assert result.passed == 1
    assert result.failed == 1
    assert result.all_passed is False
    assert result.returncode != 0


def test_runner_reports_error_in_collection(fake_skill) -> None:
    """A SyntaxError or import error in the tests file shows up as an error."""
    (fake_skill / "tests.py").write_text("def def def\n")
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert result.timed_out is False
    assert result.all_passed is False
    # passed/failed/errors all 0 is fine — pytest reports a collection error
    # not a test result. Important is that the runner doesn't crash and
    # all_passed is False.
    assert result.returncode != 0


def test_runner_enforces_hard_timeout(fake_skill) -> None:
    (fake_skill / "tests.py").write_text(textwrap.dedent("""
        import time
        def test_hangs():
            time.sleep(30)
    """).lstrip())
    result = pytest_runner.run_pytest_for_skill("fruity_limiter", timeout_s=2.0)
    assert result.timed_out is True
    assert result.all_passed is False
    assert result.returncode == -2
    assert "timed out" in result.summary.lower()


def test_runner_rejects_invalid_skill_name(fake_skill) -> None:
    with pytest.raises(sandbox.SandboxViolation):
        pytest_runner.run_pytest_for_skill("Bad-Name")


def test_runner_summary_is_truncated_to_2000_chars(fake_skill) -> None:
    """A wall of failure output shouldn't blow up the agent's context."""
    body = "\n".join(f"    print('line {i}')" for i in range(500))
    (fake_skill / "tests.py").write_text(textwrap.dedent(f"""
        def test_chatty():
{body}
            assert True
    """).lstrip())
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert len(result.summary) <= 2000


def test_runner_uses_pythonhashseed_for_determinism(fake_skill) -> None:
    """Subprocess env hard-sets PYTHONHASHSEED=0. Verify by reading it
    back in the test."""
    (fake_skill / "tests.py").write_text(textwrap.dedent("""
        import os

        def test_hashseed_is_zero():
            assert os.environ['PYTHONHASHSEED'] == '0'
    """).lstrip())
    result = pytest_runner.run_pytest_for_skill("fruity_limiter")
    assert result.all_passed is True
    assert result.passed == 1
