"""
Workspace: per-project folder structure and session manifest.

One FL Studio project maps to one StudioMind project folder:

    ~/StudioMind/projects/<slug>/
        stems/              - current per-track renders (fixed names, overwritten)
        masters/            - timestamped master renders (history kept)
        references/         - drag-dropped reference tracks
        .studiomind/
            session.json    - manifest of every render + its analysis state

Design invariants:
  - Stem filenames are deterministic from FL track index + name. Agent and user
    can never disagree about which file represents which track.
  - session.json is the single source of truth for "what audio do I have and is
    it still fresh". The LLM reads this at session start; it does not rely on
    conversation memory.
  - Each render is tagged with a hash of the relevant FL state at render time.
    If FL state changes, the render is flagged stale, not silently trusted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path.home() / "StudioMind" / "projects"

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_STALE = "stale"

KIND_STEM = "stem"
KIND_MASTER = "master"


def slugify(name: str) -> str:
    """Make a name safe for filesystem use. Empty / unnamed falls back to 'unnamed'."""
    if not name:
        return "unnamed"
    s = re.sub(r"[^\w\s-]", "", name.strip().lower())
    s = re.sub(r"[\s_-]+", "_", s)
    return s.strip("_") or "unnamed"


def project_name_from_fl_path(fl_path: str | None) -> str:
    """Derive a project name from FL's current project path. Empty -> 'untitled'.

    Handles both POSIX and Windows separators since the caller may be parsing a
    path produced on a different OS than the one running this code.
    """
    if not fl_path:
        return "untitled"
    basename = fl_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return stem or "untitled"


def hash_state(state: Any) -> str:
    """Stable 16-char hash of any JSON-serializable state. Used for staleness detection."""
    canon = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


@dataclass
class RenderRecord:
    """One rendered audio file tracked by the manifest."""

    kind: str  # KIND_STEM | KIND_MASTER
    filename: str
    status: str = STATUS_PENDING  # STATUS_PENDING | STATUS_READY | STATUS_STALE
    track_id: int | None = None  # None for master
    track_name: str | None = None  # None for master
    fl_state_hash: str | None = None  # Set when file lands
    rendered_at: float | None = None  # Set when file lands
    analysis: dict | None = None  # Populated by analyze_audio

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> RenderRecord:
        return cls(**d)


@dataclass
class Manifest:
    """Session manifest — serialized to session.json."""

    project_name: str
    fl_project_path: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    stems: dict[int, RenderRecord] = field(default_factory=dict)  # track_id -> record
    masters: list[RenderRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "fl_project_path": self.fl_project_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stems": {str(tid): rec.to_dict() for tid, rec in self.stems.items()},
            "masters": [rec.to_dict() for rec in self.masters],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Manifest:
        stems = {
            int(tid): RenderRecord.from_dict(rec) for tid, rec in d.get("stems", {}).items()
        }
        masters = [RenderRecord.from_dict(rec) for rec in d.get("masters", [])]
        return cls(
            project_name=d["project_name"],
            fl_project_path=d.get("fl_project_path"),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            stems=stems,
            masters=masters,
        )


class Project:
    """A StudioMind project folder with stems, masters, references, and manifest."""

    STEMS_DIR = "stems"
    MASTERS_DIR = "masters"
    REFERENCES_DIR = "references"
    META_DIR = ".studiomind"
    MANIFEST_FILE = "session.json"

    def __init__(self, root: Path, name: str) -> None:
        self.root = root
        self.name = name

    @property
    def stems_dir(self) -> Path:
        return self.root / self.STEMS_DIR

    @property
    def masters_dir(self) -> Path:
        return self.root / self.MASTERS_DIR

    @property
    def references_dir(self) -> Path:
        return self.root / self.REFERENCES_DIR

    @property
    def meta_dir(self) -> Path:
        return self.root / self.META_DIR

    @property
    def manifest_path(self) -> Path:
        return self.meta_dir / self.MANIFEST_FILE

    def ensure_dirs(self) -> None:
        for d in (self.stems_dir, self.masters_dir, self.references_dir, self.meta_dir):
            d.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> Manifest:
        """Load manifest from disk, or create a fresh one if missing."""
        if not self.manifest_path.exists():
            m = Manifest(project_name=self.name)
            self.save_manifest(m)
            return m
        with self.manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return Manifest.from_dict(data)

    def save_manifest(self, m: Manifest) -> None:
        self.ensure_dirs()
        m.updated_at = time.time()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(m.to_dict(), f, indent=2, sort_keys=True)
        tmp.replace(self.manifest_path)

    def stem_filename(self, track_id: int, track_name: str) -> str:
        """Deterministic filename for a stem. Zero-padded track id keeps dir sorted."""
        return f"track_{track_id:03d}_{slugify(track_name)}.wav"

    def master_filename(self, timestamp: float | None = None) -> str:
        """Timestamped master filename (history is kept)."""
        t = int(timestamp if timestamp is not None else time.time())
        return f"master_{t}.wav"

    def mark_stale(self, manifest: Manifest, current_track_hashes: dict[int, str]) -> list[int]:
        """
        Compare current FL per-track hashes against recorded hashes.
        Any stem whose track hash no longer matches gets flagged STATUS_STALE.
        Returns list of track_ids that were newly marked stale.
        """
        newly_stale: list[int] = []
        for tid, rec in manifest.stems.items():
            if rec.status != STATUS_READY:
                continue
            current = current_track_hashes.get(tid)
            if current is None or current != rec.fl_state_hash:
                rec.status = STATUS_STALE
                newly_stale.append(tid)
        return newly_stale


def open_project(
    project_name: str,
    root: Path = WORKSPACE_ROOT,
    fl_project_path: str | None = None,
) -> Project:
    """
    Open (or create) a StudioMind project folder by name.

    Returns a ready-to-use Project with directories created. Manifest is loaded
    from disk if it exists, otherwise a fresh one is written. The `fl_project_path`
    is recorded on the manifest for user reference.
    """
    proj_root = root / slugify(project_name)
    project = Project(proj_root, project_name)
    project.ensure_dirs()
    manifest = project.load_manifest()
    if fl_project_path and manifest.fl_project_path != fl_project_path:
        manifest.fl_project_path = fl_project_path
        project.save_manifest(manifest)
    return project
