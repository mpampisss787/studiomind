"""Tests for the device-install module + CLI subcommand."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from studiomind import device_install
from studiomind.device_install import (
    DEVICE_SCRIPT_NAME,
    DEVICE_SUBDIR,
    DeviceInstallError,
    DeviceStatus,
    check_device_freshness,
    find_fl_hardware_dir,
    get_device_status,
    install_device,
    scripts_match,
)


# ───────────────────────────── fixtures ───────────────────────────────

@pytest.fixture
def bundled(tmp_path: Path) -> Path:
    """A synthetic 'bundled' device script with known content."""
    p = tmp_path / "scripts" / DEVICE_SCRIPT_NAME
    p.parent.mkdir(parents=True)
    p.write_bytes(b"# bundled v2\nimport plugins\n")
    return p


@pytest.fixture
def fl_hardware(tmp_path: Path) -> Path:
    """A faux FL Hardware folder."""
    h = tmp_path / "fl_hardware"
    h.mkdir()
    return h


# ───────────────────────────── path discovery ─────────────────────────

def test_find_fl_hardware_dir_picks_first_existing(tmp_path: Path) -> None:
    miss_a = tmp_path / "nope_a"
    miss_b = tmp_path / "nope_b"
    hit = tmp_path / "real"
    hit.mkdir()
    found = find_fl_hardware_dir(candidates=[miss_a, miss_b, hit])
    assert found == hit


def test_find_fl_hardware_dir_returns_none_when_nothing_matches(tmp_path: Path) -> None:
    found = find_fl_hardware_dir(candidates=[tmp_path / "nope1", tmp_path / "nope2"])
    assert found is None


def test_find_fl_hardware_dir_default_candidates_include_canonical_paths() -> None:
    """The default search includes at least the standard Windows path
    under ~/Documents. Other platforms don't matter here — we're
    asserting that the canonical Windows location is probed."""
    # Internal helper — exposed so the canonical list isn't a black box.
    candidates = device_install._candidate_hardware_dirs()
    pathstrs = [str(c) for c in candidates]
    assert any("Image-Line" in p and "FL Studio" in p for p in pathstrs)


# ───────────────────────────── hashing ───────────────────────────────

def test_file_hash_normalizes_crlf(tmp_path: Path) -> None:
    """Two files differing only by line endings hash identically —
    so a Windows autocrlf checkout matches its Linux source."""
    lf = tmp_path / "a.py"
    crlf = tmp_path / "b.py"
    lf.write_bytes(b"line one\nline two\n")
    crlf.write_bytes(b"line one\r\nline two\r\n")
    assert device_install._file_hash(lf) == device_install._file_hash(crlf)


def test_scripts_match_true_for_identical(tmp_path: Path) -> None:
    a = tmp_path / "a"; b = tmp_path / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    assert scripts_match(a, b) is True


def test_scripts_match_false_for_drift(tmp_path: Path) -> None:
    a = tmp_path / "a"; b = tmp_path / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    assert scripts_match(a, b) is False


def test_scripts_match_false_when_one_missing(tmp_path: Path) -> None:
    a = tmp_path / "a"; b = tmp_path / "b"
    a.write_bytes(b"x")
    assert scripts_match(a, b) is False


# ───────────────────────────── get_device_status ──────────────────────

def test_status_no_fl_when_no_hardware_dir(bundled: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        device_install, "_candidate_hardware_dirs",
        lambda: [tmp_path / "nope"],
    )
    s = get_device_status(bundled_script=bundled)
    assert s.state == "no_fl"
    assert s.target_path is None


def test_status_missing_when_subdir_absent(bundled: Path, fl_hardware: Path) -> None:
    s = get_device_status(bundled_script=bundled, target_dir=fl_hardware)
    assert s.state == "missing"
    assert s.target_path == fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    assert s.deployed_exists is False


def test_status_up_to_date_when_hashes_match(bundled: Path, fl_hardware: Path) -> None:
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    shutil.copy(bundled, deployed)
    s = get_device_status(bundled_script=bundled, target_dir=fl_hardware)
    assert s.state == "up_to_date"
    assert s.matches is True


def test_status_stale_when_deployed_differs(bundled: Path, fl_hardware: Path) -> None:
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    deployed.write_bytes(b"# old version\n")
    s = get_device_status(bundled_script=bundled, target_dir=fl_hardware)
    assert s.state == "stale"
    assert s.matches is False


def test_status_stale_with_crlf_only_diff_is_actually_up_to_date(
    bundled: Path, fl_hardware: Path,
) -> None:
    """Windows autocrlf checkout with CRLF line endings shouldn't be
    flagged stale — _file_hash normalizes them away."""
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    deployed.write_bytes(bundled.read_bytes().replace(b"\n", b"\r\n"))
    s = get_device_status(bundled_script=bundled, target_dir=fl_hardware)
    assert s.state == "up_to_date"


def test_status_raises_when_bundled_missing(tmp_path: Path) -> None:
    with pytest.raises(DeviceInstallError, match="Bundled device script missing"):
        get_device_status(bundled_script=tmp_path / "nonexistent.py")


# ───────────────────────────── install_device ────────────────────────

def test_install_device_copies_into_missing_target(
    bundled: Path, fl_hardware: Path,
) -> None:
    new_status = install_device(bundled_script=bundled, target_dir=fl_hardware)
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    assert deployed.exists()
    assert deployed.read_bytes() == bundled.read_bytes()
    assert new_status.state == "up_to_date"


def test_install_device_creates_subdir_if_missing(
    bundled: Path, fl_hardware: Path,
) -> None:
    install_device(bundled_script=bundled, target_dir=fl_hardware)
    assert (fl_hardware / DEVICE_SUBDIR).is_dir()


def test_install_device_overwrites_stale(bundled: Path, fl_hardware: Path) -> None:
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    deployed.write_bytes(b"# stale\n")
    install_device(bundled_script=bundled, target_dir=fl_hardware)
    assert deployed.read_bytes() == bundled.read_bytes()


def test_install_device_is_idempotent(bundled: Path, fl_hardware: Path) -> None:
    install_device(bundled_script=bundled, target_dir=fl_hardware)
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    mtime_before = deployed.stat().st_mtime_ns
    # Sleep-free: shutil.copy2 always touches mtime, so a no-op idempotent
    # second install must NOT call copy2. We just rerun and confirm the
    # file's mtime is unchanged.
    install_device(bundled_script=bundled, target_dir=fl_hardware)
    assert deployed.stat().st_mtime_ns == mtime_before


def test_install_device_force_recopies(
    bundled: Path, fl_hardware: Path, monkeypatch,
) -> None:
    """Force=True re-copies even when hashes match. We can't observe
    the second copy via mtime (shutil.copy2 preserves source mtime),
    so we count copy2 invocations instead."""
    install_device(bundled_script=bundled, target_dir=fl_hardware)

    calls: list[tuple] = []
    real_copy2 = device_install.shutil.copy2

    def counting_copy2(src, dst):
        calls.append((src, dst))
        return real_copy2(src, dst)

    monkeypatch.setattr(device_install.shutil, "copy2", counting_copy2)
    install_device(bundled_script=bundled, target_dir=fl_hardware, force=True)
    assert len(calls) == 1, "force=True must fire copy2 exactly once"


def test_install_device_raises_when_no_fl(bundled: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        device_install, "_candidate_hardware_dirs",
        lambda: [tmp_path / "nope"],
    )
    with pytest.raises(DeviceInstallError, match="Cannot locate FL Studio"):
        install_device(bundled_script=bundled)


# ───────────────────────────── check_device_freshness ────────────────

def test_check_freshness_silent_when_no_fl(bundled: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(device_install, "_candidate_hardware_dirs", lambda: [])
    caplog.set_level("WARNING", logger="studiomind.device_install")
    status = check_device_freshness(bundled_script=bundled)
    assert status is not None
    assert status.state == "no_fl"
    # No WARNING log entry for no_fl.
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_check_freshness_logs_warning_on_stale(
    bundled: Path, fl_hardware: Path, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(
        device_install, "_candidate_hardware_dirs", lambda: [fl_hardware],
    )
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    deployed.write_bytes(b"# stale\n")
    caplog.set_level("WARNING", logger="studiomind.device_install")
    status = check_device_freshness(bundled_script=bundled)
    assert status is not None
    assert status.state == "stale"
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warns, "expected a WARNING for stale script"
    msg = "\n".join(r.getMessage() for r in warns)
    assert "STALE" in msg
    assert "install-device" in msg


def test_check_freshness_logs_warning_on_missing(
    bundled: Path, fl_hardware: Path, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(
        device_install, "_candidate_hardware_dirs", lambda: [fl_hardware],
    )
    caplog.set_level("WARNING", logger="studiomind.device_install")
    status = check_device_freshness(bundled_script=bundled)
    assert status is not None
    assert status.state == "missing"
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("NOT INSTALLED" in r.getMessage() for r in warns)


def test_check_freshness_silent_on_match(
    bundled: Path, fl_hardware: Path, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(
        device_install, "_candidate_hardware_dirs", lambda: [fl_hardware],
    )
    deployed = fl_hardware / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    deployed.parent.mkdir()
    shutil.copy(bundled, deployed)
    caplog.set_level("WARNING", logger="studiomind.device_install")
    status = check_device_freshness(bundled_script=bundled)
    assert status is not None
    assert status.state == "up_to_date"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_check_freshness_returns_none_when_bundled_missing(tmp_path: Path) -> None:
    out = check_device_freshness(bundled_script=tmp_path / "nope.py")
    assert out is None


# ───────────────────────────── CLI subcommand ────────────────────────

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "studiomind", *args],
        capture_output=True, text=True, timeout=20,
        cwd="/home/babis/studiomind",
        env={"PYTHONPATH": "/home/babis/studiomind/src", "PATH": "/usr/bin:/bin"},
    )


def test_install_device_help_lists_flags() -> None:
    proc = _run_cli("install-device", "--help")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for flag in ("--target", "--check", "--force"):
        assert flag in out


def test_install_device_no_fl_exits_with_clear_message() -> None:
    """No FL on the Linux dev box → friendly error pointing the user
    at --target."""
    proc = _run_cli("install-device")
    assert proc.returncode != 0
    out = proc.stdout
    assert "FL's Hardware folder" in out
    assert "--target" in out


def test_install_device_target_argument_works(tmp_path: Path) -> None:
    """Spotted via --target: the synthetic empty hardware dir → state
    'missing' → install copies + reports installed."""
    target = tmp_path / "fakehardware"
    target.mkdir()
    proc = _run_cli("install-device", "--target", str(target))
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    out = proc.stdout
    assert "[installed]" in out
    deployed = target / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    assert deployed.exists()
    # Idempotent re-run: state up_to_date, no second copy
    proc2 = _run_cli("install-device", "--target", str(target))
    assert proc2.returncode == 0
    assert "already up to date" in proc2.stdout


def test_install_device_check_mode_reports_without_copying(tmp_path: Path) -> None:
    """--check on a missing target reports + exits 1; no file written."""
    target = tmp_path / "fakehardware"
    target.mkdir()
    proc = _run_cli("install-device", "--check", "--target", str(target))
    assert proc.returncode == 1
    assert "missing" in proc.stdout.lower() or "stale" in proc.stdout.lower()
    deployed = target / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    assert not deployed.exists()
