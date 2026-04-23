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
import logging
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

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
    META_DIR = ".studiomind"
    MANIFEST_FILE = "session.json"
    HISTORY_FILE = "history.md"
    NOTES_FILE = "notes.md"
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
    def meta_dir(self) -> Path:
        return self.root / self.META_DIR

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
        # Analyzer injection — defaults to studiomind.analyzer.spectral.analyze_audio
        self._analyze_fn = analyze_fn

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
        # Reference tracks are files physically present in references/
        references = (
            sorted(p.name for p in self._project.references_dir.iterdir() if p.is_file())
            if self._project.references_dir.exists()
            else []
        )
        return {
            "project_name": self._project.name,
            "root": str(self._project.root),
            "fl_project_path": self._manifest.fl_project_path,
            "stems_dir": str(self._project.stems_dir),
            "masters_dir": str(self._project.masters_dir),
            "stems": stems,
            "masters": masters,
            "references": references,
        }

    # ── Auto-render ───────────────────────────────────────────────

    def _interruptible_sleep(self, seconds: float, stop_event: threading.Event | None = None) -> bool:
        """Sleep for `seconds`, checking stop_event every 100ms. Returns True if stopped."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return True
            time.sleep(min(0.1, deadline - time.monotonic()))
        return False

    def _configure_export_dialog(
        self,
        desktop: Any,
        dialog_hwnd: int,
        output_dir: Path,
        batch: bool = False,
    ) -> bool:
        """
        Type the output path into the Save As dialog and press Enter/Save.

        The dialog already has keyboard focus (detected via foreground change).
        We use SendInput to type each character directly — this is guaranteed
        to land in the focused control regardless of window class or framework.
        """
        try:
            import ctypes

            user32     = ctypes.windll.user32  # type: ignore[attr-defined]
            WM_SETTEXT = 0x000C
            BM_CLICK   = 0x00F5
            path_str   = str(output_dir).rstrip("\\") + "\\"

            print(f"[AutoRender] Setting path in Save As: {path_str}", flush=True)
            logger.info("Configuring Save As 0x%x — path: %s", dialog_hwnd, path_str)

            # Enumerate children once (re-using _children from _try_auto_render scope
            # is not possible here, so inline the same pattern)
            child_info: list[dict] = []
            ChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

            def _enum(hwnd, _):
                cls  = ctypes.create_unicode_buffer(128)
                text = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 128)
                user32.GetWindowTextW(hwnd, text, 256)
                child_info.append({"hwnd": hwnd, "cls": cls.value, "text": text.value})
                return True

            user32.EnumChildWindows(dialog_hwnd, ChildProc(_enum), 0)
            for c in child_info:
                print(f"  [child] cls={c['cls']!r} text={c['text']!r} hwnd=0x{c['hwnd']:x}", flush=True)
                logger.info("Dialog child: cls=%r text=%r hwnd=0x%x", c["cls"], c["text"], c["hwnd"])

            # ── Set the filename / folder path ─────────────────────────
            # The Windows Save As has one or two Edit controls.  The filename
            # field is always the FIRST 'Edit' child (NOT inside the address bar).
            # We set the folder path: typing just a folder path + Enter navigates
            # there; the second Enter (or Save click) confirms.
            filename_edit = None
            for c in child_info:
                if c["cls"].lower() == "edit":
                    filename_edit = c["hwnd"]
                    break

            # Check current folder. If already correct, skip path entry.
            target_folder_name = Path(path_str.rstrip("\\")).name.lower()
            already_correct_folder = False
            for c in child_info:
                if "address:" in c["text"].lower():
                    if c["text"].lower().rstrip("\\").endswith(target_folder_name):
                        already_correct_folder = True
                        print(f"[AutoRender] Already in correct folder", flush=True)
                    break

            from pywinauto.keyboard import send_keys  # type: ignore[import-untyped]

            # When a Save As dialog opens, the filename Edit has focus by default.
            # Use pywinauto send_keys (which routes through SendInput) so the
            # dialog properly processes navigation + Save. WM_SETTEXT + WM_KEYDOWN
            # bypasses Windows' dialog processing and doesn't actually navigate.
            if not already_correct_folder:
                # Select all in filename field, type full path, Enter to navigate
                send_keys("^a")
                time.sleep(0.05)
                # Escape special chars for send_keys: (){}[]+^%~
                # Paths contain none of these typically, but backslash is safe
                send_keys(path_str, with_spaces=True, pause=0.005)
                print(f"[AutoRender] Typed path into filename field", flush=True)
                time.sleep(0.1)
                send_keys("{ENTER}")          # navigate to folder
                time.sleep(0.8)                # wait for navigation
                print("[AutoRender] Navigated via Enter", flush=True)

            # Now click Save by pressing Enter again (default button is Save)
            # OR use the Alt+S accelerator shortcut (&Save)
            send_keys("%s")   # Alt+S — reliably clicks &Save button
            print("[AutoRender] Sent Alt+S to click Save", flush=True)
            logger.info("Sent Alt+S to trigger &Save")
            return True

        except Exception as e:
            logger.warning("Dialog configuration failed: %s", e)
            return False

    def _try_auto_render(self, stop_event: threading.Event | None = None, batch: bool = False) -> tuple[bool, str]:
        """
        Attempt to trigger FL Studio's WAV export using Windows PostMessage,
        which sends key events directly to FL's window handle without changing
        global keyboard focus. This avoids the browser-refresh bug caused by
        pywinauto's send_keys / type_keys (both use global keyboard injection
        under the hood and can hit the browser).

        Requires pywinauto for window discovery only (finding FL's hwnd).
        The actual key delivery uses ctypes PostMessage.

        Returns (triggered: bool, message: str).
        """
        import sys
        if sys.platform != "win32":
            return False, "auto-render is Windows-only"

        try:
            from pywinauto import Desktop  # type: ignore[import-untyped]
        except ImportError:
            return False, "pywinauto not installed — using manual export flow"

        try:
            import ctypes
            from pywinauto.keyboard import send_keys  # type: ignore[import-untyped]

            desktop = Desktop(backend="uia")
            fl_wins = [
                w for w in desktop.windows()
                if "FL Studio" in (w.window_text() or "")
            ]
            if not fl_wins:
                return False, "FL Studio window not found"

            fl_win = fl_wins[0]

            # FL uses low-level input hooks and ignores PostMessage for shortcuts,
            # so we must use global SendInput / send_keys. The risk was Ctrl+R
            # hitting the browser instead of FL. We prevent that by:
            # 1. Finding the Python console / taskbar window and confirming it's
            #    not focused (belt-and-suspenders)
            # 2. Explicitly setting FL as foreground via SetForegroundWindow (WIN32)
            #    rather than pywinauto set_focus which can be unreliable
            # 3. Waiting long enough for the focus to propagate before sending keys

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            hwnd = fl_win.handle

            # Windows blocks SetForegroundWindow for background processes.
            # Workaround: AttachThreadInput temporarily joins this thread to FL's
            # input queue, giving us permission to steal the foreground.
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            fg_hwnd     = user32.GetForegroundWindow()
            my_tid      = kernel32.GetCurrentThreadId()
            fl_tid      = user32.GetWindowThreadProcessId(hwnd, None)
            fg_tid      = user32.GetWindowThreadProcessId(fg_hwnd, None)

            user32.AttachThreadInput(my_tid, fg_tid, True)
            user32.AttachThreadInput(my_tid, fl_tid, True)
            try:
                user32.ShowWindow(hwnd, 9)        # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                user32.AttachThreadInput(my_tid, fg_tid, False)
                user32.AttachThreadInput(my_tid, fl_tid, False)

            if self._interruptible_sleep(0.6, stop_event):
                return False, "Stopped by user"

            # Confirm FL actually has focus before sending keys
            fg = user32.GetForegroundWindow()
            if fg != hwnd:
                logger.warning("FL did not take foreground after AttachThreadInput — aborting auto-render")
                return False, "Could not bring FL Studio to foreground — please export manually"

            # initial_fg must be set AFTER FL is the foreground window.
            initial_fg = user32.GetForegroundWindow()
            print(f"[AutoRender] FL hwnd=0x{initial_fg:x}", flush=True)
            logger.info("FL in foreground hwnd=0x%x", initial_fg)

            def _children(hwnd: int) -> list[dict]:
                info: list[dict] = []
                Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

                def _cb(h, _):
                    cls  = ctypes.create_unicode_buffer(128)
                    text = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(h, cls, 128)
                    user32.GetWindowTextW(h, text, 256)
                    info.append({"hwnd": h, "cls": cls.value, "text": text.value})
                    return True

                user32.EnumChildWindows(hwnd, Proc(_cb), 0)
                return info

            def _is_save_dialog(hwnd: int) -> bool:
                """True if this hwnd is a Windows Save As file dialog."""
                for c in _children(hwnd):
                    if c["text"].strip().lower() in ("&save", "save", "&open"):
                        return True
                return False

            def _wait_for_save_dialog(timeout_s: float = 8.0) -> int | None:
                """Poll until a Windows Save As dialog appears (any foreground window with &Save)."""
                deadline = time.monotonic() + timeout_s
                seen: set[int] = set()
                while time.monotonic() < deadline:
                    if stop_event and stop_event.is_set():
                        return None
                    time.sleep(0.15)
                    fg = user32.GetForegroundWindow()
                    if fg and fg not in seen:
                        seen.add(fg)
                        if _is_save_dialog(fg):
                            print(f"[AutoRender] Save As dialog found: hwnd=0x{fg:x}", flush=True)
                            logger.info("Save As dialog found: hwnd=0x%x", fg)
                            return fg
                return None

            # BEFORE pressing Ctrl+R, check if there's already a stale Save As
            # dialog open from a previous failed attempt. Check the CURRENT
            # foreground (don't poll — just look at the state right now).
            fg_now = user32.GetForegroundWindow()
            if fg_now != initial_fg and _is_save_dialog(fg_now):
                print(f"[AutoRender] Stale dialog open (0x{fg_now:x}) — dismissing", flush=True)
                logger.info("Stale dialog detected, dismissing with Escape")
                send_keys("{ESCAPE}")
                time.sleep(0.5)
                # Re-focus FL for the Ctrl+R
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.3)

            print("[AutoRender] Pressing Ctrl+R to open export dialog", flush=True)
            send_keys("^r")

            print("[AutoRender] Waiting for Windows Save As dialog...", flush=True)
            dialog_hwnd = _wait_for_save_dialog(timeout_s=8.0)

            if dialog_hwnd is None:
                print("[AutoRender] Save As dialog not detected — trying Enter fallback", flush=True)
                logger.warning("Save As dialog not detected after 8s")
                send_keys("{ENTER}")
                dialog_hwnd = _wait_for_save_dialog(timeout_s=4.0)
                if dialog_hwnd is None:
                    print("[AutoRender] Giving up — no Save As dialog appeared", flush=True)
                    return True, "Ctrl+R sent but Save As dialog not detected"

            print(f"[AutoRender] Configuring dialog hwnd=0x{dialog_hwnd:x}", flush=True)

            # Give the dialog a moment to fully render
            if self._interruptible_sleep(0.4, stop_event):
                return False, "Stopped by user"

            # Determine the output folder based on render type
            output_dir = self._project.stems_dir if batch else self._project.masters_dir

            configured = self._configure_export_dialog(desktop, dialog_hwnd, output_dir, batch=batch)
            if not configured:
                print("[AutoRender] Dialog config failed — sending Enter", flush=True)
                send_keys("{ENTER}")

            # ── STAGE 2: FL's render settings dialog (if any) ──────────
            # After Save As closes, FL MAY show a render settings dialog with
            # Mode/Quality and a Start button. Rather than fight with complex
            # window detection (the dialog often doesn't take foreground reliably),
            # we just send Enter after a delay. Most dialogs default to the Start
            # button, so Enter confirms. If no dialog, Enter to FL's main window
            # is harmless.
            print("[AutoRender] STAGE 2: Waiting for FL render settings (if any)", flush=True)
            if self._interruptible_sleep(1.5, stop_event):
                return True, "Save As closed, but stopped"

            # Send Enter — confirms the Start button in FL's settings dialog if open
            print("[AutoRender] STAGE 2: Sending Enter to click Start (default button)", flush=True)
            send_keys("{ENTER}")
            time.sleep(0.3)
            # Belt-and-suspenders: send Enter a second time in case there's any
            # additional confirmation dialog (e.g., "overwrite existing file?")
            send_keys("{ENTER}")
            logger.info("Stage 2: sent Enter twice to click Start / confirm")

            logger.info("Auto-render triggered via SetForegroundWindow + dialog config (batch=%s)", batch)
            return True, "Export triggered automatically — watching for the file to land."
        except Exception as e:
            return False, f"Auto-render failed ({e}) — please export manually"

    def prepare_stem(self, track_id: int, stop_event: threading.Event | None = None) -> dict:
        """Solo the track, write a pending stem entry, return the user instruction."""
        track_state = self._fl.read_mixer_track(track_id)
        track_name = track_state.get("name") or f"track_{track_id}"
        filename = self._project.stem_filename(track_id, track_name)
        full_path = self._project.stems_dir / filename
        # Delete ALL files in stems/ that match this track's slug — not just our
        # canonical name. FL exports use its own naming scheme (e.g.
        # "project_KICK ▼ RAYANE.wav") which the watcher fuzzy-matches to the
        # pending entry. If that file already exists from a previous session, the
        # watcher marks it READY before auto-render even runs, returning stale data.
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

        auto_ok, auto_msg = self._try_auto_render(stop_event=stop_event, batch=False)

        if auto_ok:
            instruction = (
                f"Track {track_id} ({track_name}) is soloed and export was triggered automatically. "
                f"{auto_msg} Expected file: {filename}"
            )
        else:
            instruction = (
                f"Track {track_id} ({track_name}) is soloed in FL. "
                f"In FL Studio: Ctrl+R → Start → save as '{filename}' "
                f"into: {self._project.stems_dir}  "
                f"({auto_msg})"
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
            "auto_render_attempted": auto_ok,
            "instruction": instruction,
        }

    def prepare_batch_render(self, include_master: bool = True, stop_event: threading.Event | None = None) -> dict:
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

        auto_ok, auto_msg = self._try_auto_render(stop_event=stop_event, batch=True)

        if auto_ok:
            instruction = (
                f"Batch export triggered automatically for {len(tracks_prepared)} tracks. "
                f"{auto_msg} "
                "FL will create one file per mixer track. The master is auto-detected "
                "and moved to masters/."
            )
        else:
            instruction = (
                f"Batch-render {len(tracks_prepared)} tracks in one FL export:\n"
                f"  1. File -> Export -> WAV\n"
                f"  2. Mode: 'Tracks (separate audio files)'\n"
                f"  3. Output folder: {self._project.stems_dir}\n"
                f"  4. Start.\n"
                f"({auto_msg})"
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
            "auto_render_attempted": auto_ok,
            "instruction": instruction,
        }

    def prepare_master(self, stop_event: threading.Event | None = None) -> dict:
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
                rec = self._manifest.stems.get(-1)  # no-op
                for r in self._manifest.stems.values():
                    if r.filename == filename:
                        return r
                for r in self._manifest.masters:
                    if r.filename == filename:
                        return r
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
        from studiomind.analyzer.spectral import analyze_audio

        # File-lock retry: FL may still hold the WAV open right after the
        # batch export finishes. Retry a few times before giving up.
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                return analyze_audio(path).to_dict()
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "system error" in msg or "permission" in msg or "being used" in msg:
                    time.sleep(0.5 + attempt * 0.5)  # 0.5, 1.0, 1.5, 2.0 = 5s total
                    continue
                raise
        raise last_err if last_err else RuntimeError("analyze_audio failed")

    def _watch_loop(self) -> None:
        """Background poller: mark pending entries READY when their file lands and is stable."""
        while not self._watcher_stop.is_set():
            try:
                self._poll_pending()
            except Exception as e:
                logger.exception("Watcher poll error: %s", e)
            self._watcher_stop.wait(self.WATCH_INTERVAL_S)

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

    def _adopt_batch_master(self, wav_path: Path) -> None:
        """
        Move an FL-batch-exported master WAV from stems/ to masters/ and register
        it in the manifest. If a file already exists at the destination (e.g.
        from a previous session), we overwrite it — the just-rendered version
        is the fresh one we want.
        """
        dest = self._project.masters_dir / wav_path.name
        try:
            # shutil.move overwrites on Windows; Path.rename does not.
            shutil.move(str(wav_path), str(dest))
        except OSError as e:
            logger.warning("Could not move batch master %s → %s: %s", wav_path, dest, e)
            # Delete the orphan in stems/ so the watcher doesn't retry forever.
            try:
                wav_path.unlink()
            except OSError:
                pass
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
            for wav in candidates:
                if wav in claimed_files:
                    continue
                if slug in slugify(wav.stem):
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
