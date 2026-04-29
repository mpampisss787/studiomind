"""Locate FL Studio's Hardware folder and keep the bundled
``scripts/device_StudioMind.py`` in sync with the deployed copy.

The repo ships ``scripts/device_StudioMind.py`` — the controller
script FL loads at startup. After every ``git pull`` the bundled
script may be newer than what's deployed in FL's Hardware folder,
which is what causes ``Unknown method`` errors at runtime (see
2026-04-29 live test: ``apply_sidechain`` failed because the
deployed device script lacked ``set_send``).

Two entry points:

  * :func:`install_device` — copy the bundled script over the
    deployed one. Idempotent (no-op on hash match).
  * :func:`check_device_freshness` — read-only diff. Called from
    ``cli.main`` so a stale script logs a warning + a "run
    `studiomind install-device`" hint at every boot.

Pure module — no side effects on import. CLI / boot-time hooks
call into it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# The script file we keep in sync. The bundled copy lives in the repo;
# the deployed copy lives in FL's Hardware\StudioMind\ folder.
DEVICE_SCRIPT_NAME = "device_StudioMind.py"
DEVICE_SUBDIR = "StudioMind"


class DeviceInstallError(Exception):
    """Raised when install/check cannot proceed (missing repo, etc.)."""


# ───────────────────────────── path discovery ─────────────────────────

def _candidate_hardware_dirs() -> list[Path]:
    """Return the canonical FL Hardware folder paths to probe in
    order. First match wins. Cross-platform — works on Windows,
    macOS, and Linux+Wine."""
    home = Path.home()
    out: list[Path] = [
        # Windows: standard install
        home / "Documents" / "Image-Line" / "FL Studio" / "Settings" / "Hardware",
    ]
    # Windows: portable / LOCALAPPDATA
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        out.append(Path(localappdata) / "Image-Line" / "FL Studio" / "Settings" / "Hardware")
    # macOS
    out.append(
        home / "Library" / "Application Support" / "Image-Line"
        / "FL Studio" / "Settings" / "Hardware"
    )
    # Linux + Wine
    user = os.environ.get("USER", "user")
    out.append(
        home / ".wine" / "drive_c" / "users" / user / "Documents"
        / "Image-Line" / "FL Studio" / "Settings" / "Hardware"
    )
    return out


def find_fl_hardware_dir(
    *,
    candidates: Iterable[Path] | None = None,
) -> Path | None:
    """Walk the candidate list, return the first existing directory.
    ``candidates`` is injectable for tests."""
    paths = list(candidates) if candidates is not None else _candidate_hardware_dirs()
    for p in paths:
        if p.is_dir():
            return p
    return None


# ───────────────────────────── hashing ────────────────────────────────

def _file_hash(path: Path) -> str:
    """SHA-256 of a file's content. CRLF-normalized so a Windows
    autocrlf checkout matches the Linux-side bundled script."""
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def scripts_match(bundled: Path, deployed: Path) -> bool:
    """True if both files exist with identical (CRLF-normalized) content."""
    if not bundled.exists() or not deployed.exists():
        return False
    return _file_hash(bundled) == _file_hash(deployed)


# ───────────────────────────── status ─────────────────────────────────

@dataclass(frozen=True)
class DeviceStatus:
    bundled_path: Path
    target_path: Path | None             # None when no Hardware folder discovered
    deployed_exists: bool
    matches: bool
    bundled_hash: str | None             # short hex, for log/CLI display
    deployed_hash: str | None

    @property
    def state(self) -> str:
        """``up_to_date`` | ``stale`` | ``missing`` | ``no_fl``."""
        if self.target_path is None:
            return "no_fl"
        if not self.deployed_exists:
            return "missing"
        return "up_to_date" if self.matches else "stale"


def _short(h: str | None) -> str | None:
    return h[:12] if h else None


def get_device_status(
    *,
    bundled_script: Path,
    target_dir: Path | None = None,
) -> DeviceStatus:
    """Resolve everything needed to compare bundled vs deployed.

    ``target_dir`` is FL's Hardware root (the directory that contains
    ``StudioMind/device_StudioMind.py``). When None, we discover via
    :func:`find_fl_hardware_dir`."""
    if not bundled_script.exists():
        raise DeviceInstallError(
            f"Bundled device script missing at {bundled_script}. "
            "Run from a `pip install -e .` checkout."
        )
    bundled_hash = _file_hash(bundled_script)

    hardware_dir = target_dir if target_dir is not None else find_fl_hardware_dir()
    if hardware_dir is None:
        return DeviceStatus(
            bundled_path=bundled_script,
            target_path=None,
            deployed_exists=False,
            matches=False,
            bundled_hash=bundled_hash,
            deployed_hash=None,
        )

    deployed_path = hardware_dir / DEVICE_SUBDIR / DEVICE_SCRIPT_NAME
    if not deployed_path.exists():
        return DeviceStatus(
            bundled_path=bundled_script,
            target_path=deployed_path,
            deployed_exists=False,
            matches=False,
            bundled_hash=bundled_hash,
            deployed_hash=None,
        )
    deployed_hash = _file_hash(deployed_path)
    return DeviceStatus(
        bundled_path=bundled_script,
        target_path=deployed_path,
        deployed_exists=True,
        matches=(bundled_hash == deployed_hash),
        bundled_hash=bundled_hash,
        deployed_hash=deployed_hash,
    )


# ───────────────────────────── install ───────────────────────────────

def install_device(
    *,
    bundled_script: Path,
    target_dir: Path | None = None,
    force: bool = False,
) -> DeviceStatus:
    """Copy ``bundled_script`` into FL's Hardware folder, creating the
    ``StudioMind/`` subdirectory if missing.

    Idempotent: if the deployed script already matches, no copy fires
    (unless ``force=True``). Returns the DeviceStatus AFTER any copy.

    Raises ``DeviceInstallError`` when no FL install can be located
    (caller should ask the user to pass ``--target``).
    """
    status = get_device_status(
        bundled_script=bundled_script,
        target_dir=target_dir,
    )
    if status.state == "no_fl":
        raise DeviceInstallError(
            "Cannot locate FL Studio's Hardware folder. Pass --target with "
            "the full path, or check that FL is installed."
        )

    # status.target_path is set whenever state != "no_fl"
    deployed_path = status.target_path
    assert deployed_path is not None  # for type narrowing

    if status.matches and not force:
        log.info(
            "Device script already up to date at %s (sha256:%s)",
            deployed_path, _short(status.bundled_hash),
        )
        return status

    deployed_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled_script, deployed_path)
    log.info(
        "Installed device script: %s -> %s",
        bundled_script, deployed_path,
    )
    # Recompute the post-copy status so callers see matches=True.
    return get_device_status(
        bundled_script=bundled_script,
        target_dir=deployed_path.parent.parent,
    )


# ───────────────────────────── boot-time check ────────────────────────

def check_device_freshness(
    *,
    bundled_script: Path | None = None,
) -> DeviceStatus | None:
    """Best-effort warning at CLI boot. Logs a WARNING when the
    deployed script differs from the bundled one. Silent in three
    cases:

      * bundled_script is None / missing — non-editable install or
        running outside the repo
      * FL Hardware folder not found — FL isn't installed
      * already up-to-date

    Returns the DeviceStatus when something was checked, else None.
    """
    if bundled_script is None:
        bundled_script = _bundled_script_from_repo()
    if bundled_script is None or not bundled_script.exists():
        return None
    try:
        status = get_device_status(bundled_script=bundled_script)
    except DeviceInstallError:
        return None

    if status.state == "stale":
        log.warning(
            "FL device script is STALE: deployed %s differs from bundled %s. "
            "Run `studiomind install-device` to update, then F10-toggle the "
            "controller row in FL.",
            _short(status.deployed_hash),
            _short(status.bundled_hash),
        )
    elif status.state == "missing":
        log.warning(
            "FL device script is NOT INSTALLED at %s. "
            "Run `studiomind install-device` to deploy it.",
            status.target_path,
        )
    # "no_fl" and "up_to_date" stay silent.
    return status


def _bundled_script_from_repo() -> Path | None:
    """Locate the bundled script by walking up from this module's
    location to the repo root. Returns None when not in an editable
    install."""
    try:
        from studiomind.logging_setup import find_repo_root
    except ImportError:
        return None
    repo = find_repo_root()
    if repo is None:
        return None
    candidate = repo / "scripts" / DEVICE_SCRIPT_NAME
    return candidate if candidate.exists() else None
