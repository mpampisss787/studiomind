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

import errno
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from studiomind.decisions import DecisionsLog

logger = logging.getLogger(__name__)

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


def project_name_from_fl_path(fl_path: str | None) -> str | None:
    """Derive a project name from FL's current project path. Empty/None -> None.

    Handles both POSIX and Windows separators since the caller may be parsing a
    path produced on a different OS than the one running this code.
    """
    if not fl_path:
        return None
    basename = fl_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return stem or None


def hash_state(state: Any) -> str:
    """Stable 16-char hash of any JSON-serializable state. Used for staleness detection."""
    canon = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# Workflow-only fields on a mixer track that change during a session but don't
# affect the rendered audio decision. Excluded from staleness hashes so that
# soloing a track for rendering doesn't immediately flag its own render stale.
_WORKFLOW_FIELDS = frozenset({"solo", "armed", "selected"})


def hash_track_state(track_state: dict) -> str:
    """Hash a mixer-track state for staleness, ignoring workflow-only fields."""
    filtered = {k: v for k, v in track_state.items() if k not in _WORKFLOW_FIELDS}
    return hash_state(filtered)


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
    DROPS_DIR = "drops"
    META_DIR = ".studiomind"
    ANALYSES_DIR = ".studiomind/analyses"
    MANIFEST_FILE = "session.json"
    HISTORY_FILE = "history.md"
    NOTES_FILE = "notes.md"
    DECISIONS_FILE = "decisions.json"
    HISTORY_TAIL_ENTRIES = 20  # how many recent entries to expose to the agent
    HISTORY_PRUNE_KEEP = 30   # keep this many recent entries; summarise the rest

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
    def drops_dir(self) -> Path:
        """User-volunteered audio: bounces, samples, voice memos, mystery WAVs.
        Anything dropped without a specific role; the agent decides what to do
        with it via conversation. Filenames are user-controlled — no slug
        invariant like stems/."""
        return self.root / self.DROPS_DIR

    @property
    def meta_dir(self) -> Path:
        return self.root / self.META_DIR

    @property
    def analyses_dir(self) -> Path:
        """Cache directory for STFT-derived analysis artifacts. One .npz per
        ingested WAV; keyed by file basename."""
        return self.root / self.ANALYSES_DIR

    @property
    def manifest_path(self) -> Path:
        return self.meta_dir / self.MANIFEST_FILE

    @property
    def history_path(self) -> Path:
        return self.meta_dir / self.HISTORY_FILE

    @property
    def notes_path(self) -> Path:
        """User-authored project notes (optional). Lives at project root so the
        user can edit it in a plain editor without digging into .studiomind/."""
        return self.root / self.NOTES_FILE

    @property
    def decisions_path(self) -> Path:
        return self.meta_dir / self.DECISIONS_FILE

    def load_decisions(self) -> DecisionsLog:
        """Load the decisions log. Call age_pending() on first load per session."""
        return DecisionsLog.load(self.decisions_path)

    def ensure_dirs(self) -> None:
        for d in (
            self.stems_dir,
            self.masters_dir,
            self.references_dir,
            self.drops_dir,
            self.meta_dir,
            self.analyses_dir,
        ):
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

    # ── History / notes ──────────────────────────────────────────

    def append_history_entry(self, entry: str, *, timestamp: float | None = None) -> str:
        """Append a markdown entry with a UTC timestamp heading. Returns the header line."""
        import datetime as _dt

        self.ensure_dirs()
        ts = _dt.datetime.fromtimestamp(
            timestamp if timestamp is not None else time.time(), _dt.timezone.utc
        )
        header = f"## {ts.strftime('%Y-%m-%d %H:%M UTC')}"
        # First write on a fresh project prepends a title
        existing = ""
        if self.history_path.exists():
            existing = self.history_path.read_text(encoding="utf-8")
        if not existing:
            existing = f"# {self.name} — StudioMind history\n\n"

        block = f"{header}\n{entry.strip()}\n\n"
        self.history_path.write_text(existing + block, encoding="utf-8")
        return header

    def read_history(self, max_entries: int | None = None) -> str:
        """Return the most-recent N history entries concatenated. Empty string if none."""
        if not self.history_path.exists():
            return ""
        content = self.history_path.read_text(encoding="utf-8")
        if max_entries is None:
            max_entries = self.HISTORY_TAIL_ENTRIES
        # Entries are delimited by "## " headings. Keep the top title (#) + last N entries.
        parts = content.split("\n## ")
        if len(parts) <= max_entries + 1:
            return content
        title_block = parts[0]
        # parts[1:] are entries minus their leading "## "; restore it
        tail = ["## " + p for p in parts[-max_entries:]]
        return title_block + "\n" + "\n".join(tail)

    def history_entry_count(self) -> int:
        """Return the total number of entries in history.md."""
        if not self.history_path.exists():
            return 0
        return self.history_path.read_text(encoding="utf-8").count("\n## ")

    def prune_history(self, summary: str) -> None:
        """
        Replace everything except the last HISTORY_PRUNE_KEEP entries with
        a compact summary block.  Call this when history.md has grown large.

        The summary is typically produced by the agent (write_history_entry
        already does this) or can be passed in from an external compaction step.
        """
        if not self.history_path.exists():
            return
        content = self.history_path.read_text(encoding="utf-8")
        parts = content.split("\n## ")
        if len(parts) <= self.HISTORY_PRUNE_KEEP + 1:
            return  # not large enough to bother

        title_block = parts[0]
        entries = ["## " + p for p in parts[1:]]
        recent = entries[-self.HISTORY_PRUNE_KEEP:]

        pruned = (
            title_block.rstrip()
            + "\n\n## Archive summary (auto-compacted)\n"
            + summary.strip()
            + "\n\n"
            + "\n".join(recent)
        )
        self.history_path.write_text(pruned, encoding="utf-8")

    def read_notes(self) -> str:
        """Return notes.md contents (user- or agent-authored), or empty if absent."""
        if not self.notes_path.exists():
            return ""
        return self.notes_path.read_text(encoding="utf-8")

    def append_notes_entry(self, entry: str) -> None:
        """
        Append an agent-authored insight to notes.md. Append-only — agent
        never rewrites existing content, which means the user's manual notes
        (and previous agent observations) are safe. The user can always
        hand-edit notes.md to prune stale entries.

        First write on a fresh project seeds the file with a title so the
        structure is predictable.
        """
        self.ensure_dirs()
        existing = ""
        if self.notes_path.exists():
            existing = self.notes_path.read_text(encoding="utf-8")
        if not existing.strip():
            existing = f"# {self.name} — Project notes\n\n"
        # Ensure separation from whatever came before
        if not existing.endswith("\n\n"):
            existing = existing.rstrip() + "\n\n"
        self.notes_path.write_text(existing + entry.strip() + "\n", encoding="utf-8")

    def master_filename(self, timestamp: float | None = None) -> str:
        """Timestamped master filename (history is kept)."""
        t = int(timestamp if timestamp is not None else time.time())
        return f"master_{t}.wav"

    def reconcile_with_filesystem(self, manifest: Manifest) -> bool:
        """
        Verify that every 'ready' or 'stale' entry in the manifest still has
        its file on disk.

        - Stems with missing files → reset to 'pending' (track still in FL,
          just needs re-rendering).
        - Masters with missing files → removed entirely (they are timestamped
          one-off snapshots; a 'pending' entry for a past timestamp makes no
          sense).

        Returns True if the manifest was modified so the caller can save.
        Called on every workspace-status poll so the UI is always in sync
        with the filesystem, even when files are deleted manually.
        """
        changed = False

        for rec in list(manifest.stems.values()):
            if rec.status in (STATUS_READY, STATUS_STALE):
                path = self.stems_dir / rec.filename
                if rec.filename and not path.exists():
                    rec.status = STATUS_PENDING
                    rec.rendered_at = None
                    rec.analysis = None
                    changed = True

        # Masters: remove missing entries outright
        original_count = len(manifest.masters)
        manifest.masters = [
            rec for rec in manifest.masters
            if rec.status not in (STATUS_READY, STATUS_STALE)
            or (rec.filename and (self.masters_dir / rec.filename).exists())
        ]
        if len(manifest.masters) != original_count:
            changed = True

        return changed

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


class WorkspaceSession:
    """
    Stateful session around an active Project.

    Responsibilities:
      - Prepare pending renders (solo the track, write a pending manifest entry,
        return a user-facing instruction).
      - Run a background file-watcher that detects when a pending file lands
        and flips it to READY.
      - Block on collect() until a pending render is READY, then run audio
        analysis, un-solo, and persist the analysis to the manifest.

    Threading:
      - All manifest mutations are guarded by a single lock.
      - The watcher thread never calls into FLStudio (MIDI transport is serialized
        through the agent thread only). FL state hashes are captured at
        prepare-time, not at file-ready time.
    """

    WATCH_INTERVAL_S = 0.5
    STABLE_POLLS_NEEDED = 2  # File size must be stable for this many polls
    DEFAULT_COLLECT_TIMEOUT_S = 180.0

    def __init__(
        self,
        fl: Any,  # FLStudio — loose type to avoid circular import
        project: Project,
        analyze_fn: Callable[[Path], dict] | None = None,
    ) -> None:
        self._fl = fl
        self._project = project
        self._manifest = project.load_manifest()
        self._lock = threading.Lock()
        self._watcher_stop = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        # Track (path -> (last_size, stable_count)) for size-stability detection
        self._pending_sizes: dict[str, tuple[int, int]] = {}
        # Transient retry counter for _adopt_batch_master (WinError 32 / EBUSY)
        self._master_adopt_attempts: dict[str, int] = {}
        # Analyzer injection — defaults to studiomind.analyzer.spectral.analyze_audio
        self._analyze_fn = analyze_fn
        # Files we've tried to ingest (cache-warm scan), to avoid hammering
        # broken/locked files every tick. Cleared if the file's mtime moves.
        self._ingest_attempts: dict[str, tuple[float, int]] = {}
        # path -> mtime at which we last verified the file was cached. Lets the
        # watcher skip a full `np.load` of the .npz on every tick once a file's
        # cache state is known. Invalidated automatically by mtime drift.
        self._ingest_verified_mtime: dict[str, float] = {}
        # Persistent skip list — (filename, mtime) pairs that exhausted all
        # retry attempts. Written to .studiomind/ingest_skip.json so the next
        # server startup doesn't re-hammer locked/broken files. Cleared per-key
        # when the file's mtime changes (re-export refreshes the content).
        self._ingest_skip: dict[str, float] = self._load_ingest_skip()

    @property
    def project(self) -> Project:
        return self._project

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def start(self) -> None:
        """Start the background file-watcher thread."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_stop.clear()
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, name="studiomind-watcher", daemon=True
        )
        self._watcher_thread.start()

    def stop(self) -> None:
        """Stop the watcher thread. Idempotent."""
        self._watcher_stop.set()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=2.0)
        self._watcher_thread = None

    def status(self) -> dict:
        """Return a JSON-safe snapshot of the current workspace state."""
        with self._lock:
            stems = [rec.to_dict() for _tid, rec in sorted(self._manifest.stems.items())]
            masters = [rec.to_dict() for rec in self._manifest.masters]

        def _list_dir(d: Path) -> list[str]:
            if not d.exists():
                return []
            return sorted(p.name for p in d.iterdir() if p.is_file())

        return {
            "project_name": self._project.name,
            "root": str(self._project.root),
            "fl_project_path": self._manifest.fl_project_path,
            "stems_dir": str(self._project.stems_dir),
            "masters_dir": str(self._project.masters_dir),
            "references_dir": str(self._project.references_dir),
            "drops_dir": str(self._project.drops_dir),
            "stems": stems,
            "masters": masters,
            "references": _list_dir(self._project.references_dir),
            "drops": _list_dir(self._project.drops_dir),
        }

    def prepare_stem(self, track_id: int) -> dict:
        """Solo the track, write a pending stem entry, return the user instruction."""
        track_state = self._fl.read_mixer_track(track_id)
        track_name = track_state.get("name") or f"track_{track_id}"
        filename = self._project.stem_filename(track_id, track_name)
        full_path = self._project.stems_dir / filename
        # Delete ALL files in stems/ that match this track's slug — not just our
        # canonical name. FL exports use its own naming scheme (e.g.
        # "project_KICK ▼ RAYANE.wav") which the watcher fuzzy-matches to the
        # pending entry. If that file already exists from a previous session, the
        # watcher would mark it READY immediately and return stale data.
        track_slug = slugify(track_name)
        if self._project.stems_dir.exists():
            for wav in list(self._project.stems_dir.glob("*.wav")):
                if track_slug in slugify(wav.stem):
                    try:
                        wav.unlink()
                        logger.debug("Deleted stale stem: %s", wav.name)
                    except OSError as e:
                        logger.warning("Could not delete %s: %s", wav.name, e)
        # Canonical name (may not exist if FL named it differently, but belt-and-suspenders)
        if full_path.exists():
            try:
                full_path.unlink()
            except OSError as e:
                logger.warning("Could not remove %s: %s", full_path, e)

        # Solo the track. If this fails the pending entry is still written so
        # the user can manually solo + render.
        try:
            self._fl.solo_track(track_id, solo=True)
        except Exception as e:
            logger.warning("solo_track failed for %d: %s", track_id, e)

        state_hash = hash_track_state(track_state)

        with self._lock:
            self._manifest.stems[track_id] = RenderRecord(
                kind=KIND_STEM,
                filename=filename,
                status=STATUS_PENDING,
                track_id=track_id,
                track_name=track_name,
                fl_state_hash=state_hash,
            )
            self._project.save_manifest(self._manifest)

        instruction = (
            f"Track {track_id} ({track_name}) is soloed in FL. "
            f"In FL Studio: Ctrl+R → Start → save as '{filename}' "
            f"into: {self._project.stems_dir}"
        )

        return {
            "ok": True,
            "pending": True,
            "mode": "stem",
            "track_id": track_id,
            "track_name": track_name,
            "filename": filename,
            "full_path": str(full_path),
            "stems_dir": str(self._project.stems_dir),
            "instruction": instruction,
        }

    def prepare_batch_render(self, include_master: bool = True) -> dict:
        """
        Write pending entries for every active mixer track (and optionally master)
        so the user can do one FL batch export instead of 20 per-track renders.

        Filenames produced by FL's 'Tracks as separate audio files' mode come from
        the mixer track names. The watcher does fuzzy slug matching, so the user
        doesn't have to rename anything — whatever FL writes gets bound to the
        matching pending record.
        """
        try:
            state = self._fl.read_project_state()
        except Exception as e:
            raise RuntimeError(f"Could not read FL project state: {e}") from e

        # Un-solo every track so the batch renders reflect the full mix of each
        for t in state.get("mixer_tracks", []):
            if t.get("solo"):
                try:
                    self._fl.solo_track(t["index"], solo=False)
                except Exception as e:
                    logger.warning("Un-solo failed for %d: %s", t["index"], e)

        tracks_prepared: list[dict] = []
        with self._lock:
            for t in state.get("mixer_tracks", []):
                tid = t.get("index")
                if tid is None or tid == 0:  # skip master (handled below via include_master)
                    continue
                if not t.get("enabled", True):
                    continue
                track_name = t.get("name") or f"track_{tid}"
                canonical_filename = self._project.stem_filename(tid, track_name)

                try:
                    full = self._fl.read_mixer_track(tid)
                    state_hash = hash_track_state(full)
                except Exception:
                    state_hash = None

                self._manifest.stems[tid] = RenderRecord(
                    kind=KIND_STEM,
                    filename=canonical_filename,
                    status=STATUS_PENDING,
                    track_id=tid,
                    track_name=track_name,
                    fl_state_hash=state_hash,
                )
                tracks_prepared.append(
                    {"track_id": tid, "track_name": track_name, "suggested_filename": canonical_filename}
                )
            self._project.save_manifest(self._manifest)

        master_info = self.prepare_master() if include_master else None

        instruction = (
            f"Batch-render {len(tracks_prepared)} tracks in one FL export:\n"
            f"  1. File -> Export -> WAV\n"
            f"  2. Mode: 'Tracks (separate audio files)'\n"
            f"  3. Output folder: {self._project.stems_dir}\n"
            f"  4. Start."
        )

        return {
            "ok": True,
            "pending": True,
            "mode": "batch",
            "tracks_prepared": tracks_prepared,
            "track_count": len(tracks_prepared),
            "master_included": include_master,
            "master": master_info,
            "stems_dir": str(self._project.stems_dir),
            "masters_dir": str(self._project.masters_dir),
            "instruction": instruction,
        }

    def prepare_master(self) -> dict:
        """Un-solo everything, write a pending master entry, return the user instruction."""
        # Clear any solo state so the master reflects the full mix
        try:
            state = self._fl.read_project_state()
            for t in state.get("mixer_tracks", []):
                if t.get("solo"):
                    try:
                        self._fl.solo_track(t["index"], solo=False)
                    except Exception as e:
                        logger.warning("Un-solo failed for %d: %s", t["index"], e)
        except Exception as e:
            logger.warning("Could not read project state to un-solo: %s", e)

        filename = self._project.master_filename()
        full_path = self._project.masters_dir / filename
        state_hash = hash_state(self._fl.read_project_state())

        rec = RenderRecord(
            kind=KIND_MASTER,
            filename=filename,
            status=STATUS_PENDING,
            fl_state_hash=state_hash,
        )
        with self._lock:
            # Drop any existing pending masters so they don't all try to claim
            # the same file during a batch render. Keep READY/STALE entries
            # (history is useful for master comparison).
            self._manifest.masters = [
                m for m in self._manifest.masters if m.status != STATUS_PENDING
            ]
            self._manifest.masters.append(rec)
            self._project.save_manifest(self._manifest)

        return {
            "ok": True,
            "pending": True,
            "mode": "master",
            "filename": filename,
            "full_path": str(full_path),
            "masters_dir": str(self._project.masters_dir),
            "instruction": (
                f"All tracks are un-soloed. In FL Studio: File -> Export -> WAV (or "
                f"Ctrl+R), Start, and save as '{filename}' into the folder: "
                f"{self._project.masters_dir}"
            ),
        }

    def collect(
        self,
        track_id: int | None = None,
        filename: str | None = None,
        timeout_s: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> dict:
        """
        Block until the matching pending render is READY, analyze it, and return.

        Identify the target by `track_id` (stem) or `filename` (either kind).
        Un-solos the stem's track before returning so the mix is playable again.
        """
        timeout = timeout_s or self.DEFAULT_COLLECT_TIMEOUT_S
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("Stopped by user during render wait.")
            rec = self._find_record(track_id=track_id, filename=filename)
            if rec is None:
                raise ValueError(
                    f"No pending render matches track_id={track_id!r} filename={filename!r}"
                )
            if rec.status == STATUS_READY:
                # Watcher flipped it ready — always run fresh analysis.
                # Clear any previous analysis so the collect_render call always
                # reflects the file that JUST landed, not a cached old result.
                rec.analysis = None
                break
            time.sleep(0.25)
        else:
            raise TimeoutError(
                f"Render did not land within {timeout:.0f}s. "
                f"track_id={track_id}, filename={filename}."
            )

        path = self._record_path(rec)
        analysis_dict = self._run_analysis(path)

        with self._lock:
            rec.analysis = analysis_dict
            self._project.save_manifest(self._manifest)

        # Un-solo the track if this was a stem
        if rec.kind == KIND_STEM and rec.track_id is not None:
            try:
                self._fl.solo_track(rec.track_id, solo=False)
            except Exception as e:
                logger.warning("Un-solo failed for track %d: %s", rec.track_id, e)

        return self._build_collect_result(rec)

    def detect_external_changes(self) -> dict:
        """
        Compare current FL state per mixer track to the fl_state_hash recorded
        at each stem's last render. Reports which tracks were edited outside
        StudioMind (e.g., user changed EQ in FL without involving the agent).

        Returns:
            {
              "tracks_changed": [{"track_id": 3, "track_name": "Bass",
                                   "last_seen_at": 1745..., "was_stale_before": false}],
              "tracks_unchanged": [5, 7, ...],
              "tracks_never_rendered": [{"track_id": 9, "track_name": "Guitar"}]
            }
        """
        try:
            state = self._fl.read_project_state()
        except Exception as e:
            return {"error": f"Could not read FL state: {e}"}

        tracks_changed: list[dict] = []
        tracks_unchanged: list[int] = []
        tracks_never_rendered: list[dict] = []

        # Build a set of existing stem track_ids in the manifest
        with self._lock:
            manifest_stems = dict(self._manifest.stems)

        for t in state.get("mixer_tracks", []):
            tid = t.get("index")
            if tid is None or tid == 0:  # skip master here
                continue
            if not t.get("enabled", True):
                continue

            rec = manifest_stems.get(tid)
            if rec is None or rec.fl_state_hash is None:
                tracks_never_rendered.append(
                    {"track_id": tid, "track_name": t.get("name") or ""}
                )
                continue

            # Re-hash current track state (same function used at render-time)
            try:
                full = self._fl.read_mixer_track(tid)
                current_hash = hash_track_state(full)
            except Exception:
                continue

            if current_hash == rec.fl_state_hash:
                tracks_unchanged.append(tid)
            else:
                tracks_changed.append(
                    {
                        "track_id": tid,
                        "track_name": rec.track_name or t.get("name") or "",
                        "last_seen_at": rec.rendered_at,
                        "was_stale_before": rec.status == STATUS_STALE,
                    }
                )

        return {
            "tracks_changed": tracks_changed,
            "tracks_unchanged": tracks_unchanged,
            "tracks_never_rendered": tracks_never_rendered,
            "summary": (
                f"{len(tracks_changed)} track(s) changed externally, "
                f"{len(tracks_unchanged)} unchanged, "
                f"{len(tracks_never_rendered)} never rendered."
            ),
        }

    def refresh_staleness(self) -> list[int]:
        """
        Re-hash current FL state per track; flag stems whose track changed.

        Returns the list of newly-stale track_ids.
        """
        try:
            state = self._fl.read_project_state()
        except Exception:
            return []

        # Per-track hash of the FULL mixer track state (plugins + volume + pan + eq)
        current_hashes: dict[int, str] = {}
        for t in state.get("mixer_tracks", []):
            tid = t.get("index")
            if tid is None:
                continue
            # Use read_mixer_track for the full detail (plugins + params)
            try:
                full = self._fl.read_mixer_track(tid)
                current_hashes[tid] = hash_track_state(full)
            except Exception:
                continue

        with self._lock:
            newly_stale = self._project.mark_stale(self._manifest, current_hashes)
            if newly_stale:
                self._project.save_manifest(self._manifest)
        return newly_stale

    # ── internals ──────────────────────────────────────────────────────────

    def _record_path(self, rec: RenderRecord) -> Path:
        if rec.kind == KIND_STEM:
            return self._project.stems_dir / rec.filename
        return self._project.masters_dir / rec.filename

    def _find_record(
        self, track_id: int | None = None, filename: str | None = None
    ) -> RenderRecord | None:
        with self._lock:
            if track_id is not None:
                return self._manifest.stems.get(track_id)
            if filename is not None:
                for r in self._manifest.stems.values():
                    if r.filename == filename:
                        return r
                for r in self._manifest.masters:
                    if r.filename == filename:
                        return r
                # Master-filename fallback: the agent often asks for a
                # timestamped name it expected (master_<ts>.wav) but FL writes
                # its own name ("<project>_Master.wav") which then gets
                # adopted. If no exact match AND the request looks like a
                # master name, bind to the most recent READY master.
                if filename.startswith("master_") or "master" in filename.lower():
                    ready_masters = [
                        r for r in self._manifest.masters if r.status == STATUS_READY
                    ]
                    if ready_masters:
                        # Newest first — rendered_at is set when watcher flips
                        # to READY, so this is the freshest master we have.
                        return max(
                            ready_masters,
                            key=lambda r: r.rendered_at or 0.0,
                        )
        return None

    def _build_collect_result(self, rec: RenderRecord) -> dict:
        path = self._record_path(rec)
        return {
            "ok": True,
            "filename": rec.filename,
            "path": str(path),
            "track_id": rec.track_id,
            "track_name": rec.track_name,
            "kind": rec.kind,
            "rendered_at": rec.rendered_at,
            "fl_state_hash": rec.fl_state_hash,
            "analysis": rec.analysis,
        }

    def _run_analysis(self, path: Path) -> dict:
        if self._analyze_fn is not None:
            return self._analyze_fn(path)
        from studiomind.analyzer.pipeline import analyze_and_cache

        # File-lock retry: FL may still hold the WAV open right after the
        # batch export finishes. Retry a few times before giving up.
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                analysis = analyze_and_cache(path, self._project.analyses_dir)
                return analysis.to_dict()
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "system error" in msg or "permission" in msg or "being used" in msg:
                    time.sleep(0.5 + attempt * 0.5)  # 0.5, 1.0, 1.5, 2.0 = 5s total
                    continue
                raise
        raise last_err if last_err else RuntimeError("analyze_audio failed")

    def _watch_loop(self) -> None:
        """Background poller.

        Two passes per tick:
          1. `_poll_pending` — match files in stems/ and masters/ to pending
             render records (the agent-driven flow).
          2. `_scan_for_ingest` — analyze and cache any audio file in the
             workspace that doesn't yet have a current cache entry, regardless
             of how it got there (drag-and-drop into chat, FL native drag,
             external copy). Keeps the cache warm so drill-down tools never
             have to wait.
        """
        while not self._watcher_stop.is_set():
            try:
                self._poll_pending()
            except Exception as e:
                logger.exception("Watcher poll error: %s", e)
            try:
                self._scan_for_ingest()
            except Exception as e:
                logger.exception("Watcher ingest-scan error: %s", e)
            self._watcher_stop.wait(self.WATCH_INTERVAL_S)

    # Don't retry an ingest attempt more than this many times per (path, mtime).
    # Files that fail repeatedly (corrupt MP3, etc.) shouldn't burn CPU forever.
    _INGEST_MAX_ATTEMPTS = 3
    _INGEST_SKIP_FILE = ".studiomind/ingest_skip.json"

    def _load_ingest_skip(self) -> dict[str, float]:
        """Load persisted (filename → mtime) skip entries from disk."""
        path = self._project.root / self._INGEST_SKIP_FILE
        try:
            import json as _json
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_ingest_skip(self) -> None:
        """Persist the current skip list to disk (atomic write)."""
        try:
            import json as _json
            path = self._project.root / self._INGEST_SKIP_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(self._ingest_skip), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.debug("Could not save ingest skip list: %s", e)

    def _scan_for_ingest(self) -> None:
        """Walk all four ingest dirs and analyze+cache anything not already cached.

        Honors:
          - skips files we wrote ourselves (`*.decoded.wav`) — they're cached
            via their original (non-WAV) filename
          - skips formats we can't decode (`.txt`, `.png`, etc.)
          - applies a per-file attempt limit keyed on (path, mtime) so a
            corrupt file doesn't get retried every tick
        """
        from studiomind.analyzer.pipeline import is_cached, analyze_and_cache
        from studiomind.ingest.decode import is_supported

        analyses_dir = self._project.analyses_dir
        roots = [
            self._project.stems_dir,
            self._project.masters_dir,
            self._project.references_dir,
            self._project.drops_dir,
        ]
        for root in roots:
            if not root.exists():
                continue
            for f in root.iterdir():
                if not f.is_file():
                    continue
                if f.name.endswith(".decoded.wav"):
                    continue  # our own output, cached via the original
                if not is_supported(f):
                    continue

                key = str(f)
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue

                # Fast path: we already verified this (path, mtime) is cached.
                # `is_cached` opens and parses the .npz; calling it every 0.5 s
                # for an unchanged file is pure I/O waste.
                if self._ingest_verified_mtime.get(key) == mtime:
                    continue

                if is_cached(f, analyses_dir):
                    self._ingest_verified_mtime[key] = mtime
                    continue

                # Skip files that exhausted all attempts in a prior startup,
                # unless the file has been re-exported (mtime changed).
                if self._ingest_skip.get(key) == mtime:
                    continue

                last_mtime, attempts = self._ingest_attempts.get(key, (0.0, 0))
                if mtime != last_mtime:
                    attempts = 0  # file changed → fresh attempt budget
                    self._ingest_skip.pop(key, None)  # file refreshed, try again
                if attempts >= self._INGEST_MAX_ATTEMPTS:
                    # Persist exhaustion so next startup skips immediately.
                    self._ingest_skip[key] = mtime
                    self._save_ingest_skip()
                    continue

                try:
                    analyze_and_cache(f, analyses_dir)
                    self._ingest_attempts.pop(key, None)
                    self._ingest_skip.pop(key, None)
                    self._ingest_verified_mtime[key] = mtime
                    logger.debug("Auto-analyzed %s", f.name)
                except Exception as e:
                    self._ingest_attempts[key] = (mtime, attempts + 1)
                    logger.debug(
                        "Ingest scan: %s failed (attempt %d/%d): %s",
                        f.name, attempts + 1, self._INGEST_MAX_ATTEMPTS, e,
                    )

    def _is_fl_batch_master(self, filename: str) -> bool:
        """
        Return True ONLY if this filename is FL's auto-named master for a batch
        export. FL generates '<project>_Master.wav' (the Master track, index 0).

        Strict match: slug must END with '_master' (or equal 'master'). This avoids
        matching bus stems like 'Drums ► Mix', 'PreMaster MS', 'Kick ► Mix' etc.
        which contain 'mix' or 'master' in their names but are stems, not the master.
        """
        stem_slug = slugify(Path(filename).stem)
        return stem_slug == "master" or stem_slug.endswith("_master")

    # WinError 32 (file in use) + ERROR_ACCESS_DENIED (5) + EBUSY: transient.
    # FL can still hold a write handle for a moment after the bytes have
    # flushed and the file-size-stability check has passed. We'd rather
    # defer the move than destroy an in-flight master. Give up only after
    # this many consecutive failed adopt attempts.
    _MASTER_ADOPT_MAX_RETRIES = 10

    def _adopt_batch_master(self, wav_path: Path) -> None:
        """
        Move an FL-batch-exported master WAV from stems/ to masters/ and register
        it in the manifest. If a file already exists at the destination (e.g.
        from a previous session), we overwrite it — the just-rendered version
        is the fresh one we want. If FL still has the file locked (`WinError 32`),
        defer and retry on the next watcher tick rather than deleting the source.
        """
        dest = self._project.masters_dir / wav_path.name
        key = str(wav_path)
        try:
            # shutil.move overwrites on Windows; Path.rename does not.
            shutil.move(str(wav_path), str(dest))
            self._master_adopt_attempts.pop(key, None)
        except OSError as e:
            attempts = self._master_adopt_attempts.get(key, 0) + 1
            self._master_adopt_attempts[key] = attempts
            # winerror 32 (sharing violation), errno EACCES/EBUSY are all
            # transient — FL still has the handle open. Back off, retry later.
            transient = (
                getattr(e, "winerror", None) in (32, 5)
                or e.errno in (errno.EACCES, errno.EBUSY)
            )
            if transient and attempts < self._MASTER_ADOPT_MAX_RETRIES:
                logger.debug(
                    "Master adopt deferred (attempt %d/%d) — FL still holds the handle: %s",
                    attempts, self._MASTER_ADOPT_MAX_RETRIES, wav_path.name,
                )
                return
            logger.warning(
                "Could not move batch master %s → %s after %d attempts: %s",
                wav_path, dest, attempts, e,
            )
            # Non-transient failure or retries exhausted. Delete so the watcher
            # stops seeing a file it can't act on. A genuine master will have
            # been written again on the next render anyway.
            try:
                wav_path.unlink()
            except OSError:
                pass
            self._master_adopt_attempts.pop(key, None)
            return

        state_hash = None
        try:
            state_hash = hash_state(self._fl.read_project_state())
        except Exception:
            pass

        rec = RenderRecord(
            kind=KIND_MASTER,
            filename=dest.name,
            status=STATUS_READY,
            fl_state_hash=state_hash,
            rendered_at=time.time(),
        )
        with self._lock:
            self._manifest.masters.append(rec)
            self._project.save_manifest(self._manifest)
        logger.info("Batch master adopted from stems/ → masters/: %s", dest.name)

    def _poll_pending(self) -> None:
        """
        Match files in stems_dir / masters_dir to pending records.

        Matching rules (in priority order, each record binds at most one file):
          1. Exact filename match at the expected path.
          2. Fuzzy slug match: the track's slug appears in the WAV's basename.
             Longer slugs match first, so 'sub_bass' beats 'bass' for contested names.
        When a file matches, we track its size across polls; once stable for
        STABLE_POLLS_NEEDED polls, the record flips to READY.
        """
        with self._lock:
            pending_stems = [rec for rec in self._manifest.stems.values() if rec.status == STATUS_PENDING]
            pending_masters = [rec for rec in self._manifest.masters if rec.status == STATUS_PENDING]

        if not pending_stems and not pending_masters:
            self._pending_sizes.clear()
            return

        # Gather candidate files per directory
        stem_wavs = (
            [p for p in self._project.stems_dir.glob("*.wav") if p.is_file()]
            if self._project.stems_dir.exists() else []
        )
        master_wavs = (
            [p for p in self._project.masters_dir.glob("*.wav") if p.is_file()]
            if self._project.masters_dir.exists() else []
        )

        # Sort pending records by slug length desc so specific names bind before generic ones
        pending_stems_sorted = sorted(
            pending_stems,
            key=lambda r: len(slugify(r.track_name or "")),
            reverse=True,
        )

        # Track which files are already claimed this poll
        claimed_files: set[Path] = set()

        def try_match(rec: RenderRecord, candidates: list[Path]) -> Path | None:
            target_dir = self._project.stems_dir if rec.kind == KIND_STEM else self._project.masters_dir
            exact = target_dir / rec.filename
            if exact.exists() and exact not in claimed_files:
                return exact
            slug = slugify(rec.track_name or "") if rec.kind == KIND_STEM else "master"
            if not slug:
                return None
            # Require the track slug to match at a word boundary — either the
            # whole filename slug equals the track slug, or it ends in
            # "_<track_slug>". Plain substring matching let "koto_thing"
            # collide with "koto_thing_2" because the latter contains the
            # former. FL batch exports look like "<project>_<track>.wav",
            # so the track slug is always the suffix.
            target_suffix = "_" + slug
            for wav in candidates:
                if wav in claimed_files:
                    continue
                wav_slug = slugify(wav.stem)
                if wav_slug == slug or wav_slug.endswith(target_suffix):
                    return wav
            return None

        changed = False

        for rec in pending_stems_sorted:
            matched = try_match(rec, stem_wavs)
            if matched is None:
                continue
            claimed_files.add(matched)
            if self._check_file_stable(matched):
                with self._lock:
                    rec.filename = matched.name  # bind to the actual name FL wrote
                    rec.status = STATUS_READY
                    rec.rendered_at = time.time()
                    changed = True
                logger.info("Stem ready: %s (track %s)", matched.name, rec.track_id)

        for rec in pending_masters:
            matched = try_match(rec, master_wavs)
            if matched is None:
                continue
            claimed_files.add(matched)
            if self._check_file_stable(matched):
                with self._lock:
                    rec.filename = matched.name
                    rec.status = STATUS_READY
                    rec.rendered_at = time.time()
                    changed = True
                logger.info("Master ready: %s", matched.name)

        # Auto-adopt: FL batch exports include a master named "ProjectName - Master.wav"
        # in the stems folder.  Move it to masters/ and register it automatically so the
        # user doesn't have to do a separate master export.
        for wav in stem_wavs:
            if wav in claimed_files:
                continue
            if self._is_fl_batch_master(wav.name) and self._check_file_stable(wav):
                self._adopt_batch_master(wav)
                claimed_files.add(wav)
                changed = False  # manifest already saved inside _adopt_batch_master

        if changed:
            with self._lock:
                self._project.save_manifest(self._manifest)

    def _check_file_stable(self, path: Path) -> bool:
        """Return True once the file's size has been unchanged for STABLE_POLLS_NEEDED polls."""
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        key = str(path)
        prev = self._pending_sizes.get(key)
        if prev is None or prev[0] != size:
            self._pending_sizes[key] = (size, 1)
            return False
        stable_count = prev[1] + 1
        if stable_count >= self.STABLE_POLLS_NEEDED:
            self._pending_sizes.pop(key, None)
            return True
        self._pending_sizes[key] = (size, stable_count)
        return False


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
