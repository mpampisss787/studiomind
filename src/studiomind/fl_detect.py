"""
Detect the active FL Studio project by reading its OS window title.

Rationale: FL Studio 2025's Python API doesn't expose project name/path through
any handler we've tried (general.getName / general.getFilename / ui.getProgTitle
all return empty or just the program name). But the OS-level window title does
include the project name — and the companion is a normal Windows process, so
it can read it directly via user32.

Windows-only. On other platforms this returns (None, None).

Window title format observed on FL Studio 2025:
    "Project_TEST.flp - FL Studio 2025"          (saved, clean)
    "Project_TEST.flp* - FL Studio 2025"         (modified; asterisk on filename)
    "FL Studio 2025"                              (no project / blank)

(Older FL versions may use "FL Studio X - ProjectName" with the order reversed;
we try both forms.)
"""

from __future__ import annotations

import re
import sys


# New FL 2025 order: "ProjectName.flp - FL Studio 2025"
_FL_TITLE_PROJECT_FIRST = re.compile(r"^(.+?)\s*-\s*FL Studio\b.*$")
# Older/alternate order: "FL Studio 2025 - ProjectName"
_FL_TITLE_VERSION_FIRST = re.compile(r"^FL Studio[^-]*-\s*(.+?)\s*$")


def parse_fl_title(title: str) -> str | None:
    """Extract the project name from a FL Studio window title, or None if no project."""
    if not title:
        return None

    # Case 1: FL is running but no project loaded — title is just "FL Studio <version>"
    stripped = title.strip()
    if stripped.lower().startswith("fl studio") and " - " not in stripped:
        return None

    # Case 2: "ProjectName.flp - FL Studio 2025"
    m = _FL_TITLE_PROJECT_FIRST.match(title)
    if m:
        name = m.group(1).strip().rstrip("*").strip()
        # Strip .flp extension and trailing asterisk (modified marker)
        if name.lower().endswith(".flp"):
            name = name[:-4]
        name = name.rstrip("*").strip()
        return name or None

    # Case 3: "FL Studio 2025 - ProjectName" (older versions)
    m = _FL_TITLE_VERSION_FIRST.match(title)
    if m:
        name = m.group(1).strip().rstrip("*").strip()
        if name.lower().endswith(".flp"):
            name = name[:-4]
        return name or None

    return None


def find_fl_window_title() -> str | None:
    """Return FL Studio's main window title, or None if not found / not on Windows."""
    if sys.platform != "win32":
        return None

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return None

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    found: list[str] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value
        # FL's title contains "FL Studio" somewhere; on 2025 it's at the end
        # ("Project.flp - FL Studio 2025"), older versions had it at the start.
        if "FL Studio" in title:
            found.append(title)
            return False  # Stop at first match
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), 0)
    except OSError:
        return None

    return found[0] if found else None


def detect_fl_project() -> tuple[str | None, str | None]:
    """
    Best-effort detection of FL's current project.

    Returns (project_name, window_title). Either may be None:
      - (name, title): FL is running with an open project named `name`
      - (None, title): FL is running but no project detected in the title
      - (None, None):  FL not running, or we're not on Windows
    """
    title = find_fl_window_title()
    if title is None:
        return None, None
    return parse_fl_title(title), title


def enumerate_all_visible_windows() -> list[str]:
    """Dump every visible top-level window title. Diagnostic aid when FL detection fails."""
    if sys.platform != "win32":
        return []

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return []

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    titles: list[str] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        titles.append(buff.value)
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(callback), 0)
    except OSError:
        pass

    return titles
