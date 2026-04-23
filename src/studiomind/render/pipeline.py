"""
Render pipeline: orchestrates FL Studio audio export + analysis.

Since FL Studio's API doesn't expose rendering, we use a two-pronged approach:

1. **UI automation (pywinauto)** — triggers the export dialog, sets output path,
   starts render, waits for completion. This is the "escape hatch" for Windows.

2. **Keyboard shortcut bridge** — sends Ctrl+Shift+R (or configurable shortcut)
   via the FL device script's UI module. Lighter than pywinauto but less control.

For either approach, the flow is:
  solo track (optional) → trigger render → poll for output file → analyze → unsolo
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studiomind.analyzer.spectral import AudioAnalysis, analyze_audio, detect_masking
from studiomind.bridge.commands import FLStudio

logger = logging.getLogger(__name__)

# Default render output directory (on the Windows machine running FL Studio)
DEFAULT_RENDER_DIR = Path.home() / "Documents" / "StudioMind" / "renders"


@dataclass
class RenderConfig:
    """Configuration for the render pipeline."""

    output_dir: Path = field(default_factory=lambda: DEFAULT_RENDER_DIR)
    format: str = "wav"  # wav or mp3
    bit_depth: int = 24
    sample_rate: int = 44100
    poll_interval_s: float = 0.5
    poll_timeout_s: float = 120.0  # Max wait for render to complete


@dataclass
class RenderResult:
    """Result of a render + analysis operation."""

    path: Path
    analysis: AudioAnalysis
    track_id: int | None  # None = master
    render_duration_s: float

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "track_id": self.track_id,
            "render_duration_s": round(self.render_duration_s, 1),
            "analysis": self.analysis.to_dict(),
        }


class RenderPipeline:
    """
    Orchestrates rendering and audio analysis.

    Provides two modes:
    1. render_master() — render the full mix
    2. render_stem(track_id) — solo a track, render, unsolo
    3. render_all_stems() — render every active mixer track
    4. analyze_existing(path) — analyze an already-rendered file
    """

    def __init__(self, fl: FLStudio, config: RenderConfig | None = None) -> None:
        self._fl = fl
        self._config = config or RenderConfig()
        self._config.output_dir.mkdir(parents=True, exist_ok=True)

    def analyze_existing(self, path: str | Path) -> AudioAnalysis:
        """Analyze an existing audio file without rendering."""
        return analyze_audio(path)

    def render_master(self) -> RenderResult:
        """Render the full master mix and analyze it."""
        output_path = self._config.output_dir / f"master_{int(time.time())}.wav"

        start = time.monotonic()
        self._trigger_render(output_path)
        self._wait_for_file(output_path)
        duration = time.monotonic() - start

        analysis = analyze_audio(output_path)
        return RenderResult(
            path=output_path,
            analysis=analysis,
            track_id=None,
            render_duration_s=duration,
        )

    def render_stem(self, track_id: int) -> RenderResult:
        """Solo a mixer track, render it, unsolo, and analyze."""
        output_path = self._config.output_dir / f"stem_{track_id}_{int(time.time())}.wav"

        # Solo the target track
        self._fl.solo_track(track_id, solo=True)
        time.sleep(0.1)  # Let FL process the solo

        start = time.monotonic()
        try:
            self._trigger_render(output_path)
            self._wait_for_file(output_path)
        finally:
            # Always unsolo, even if render fails
            self._fl.solo_track(track_id, solo=False)

        duration = time.monotonic() - start
        analysis = analyze_audio(output_path)

        return RenderResult(
            path=output_path,
            analysis=analysis,
            track_id=track_id,
            render_duration_s=duration,
        )

    def render_all_stems(self, track_ids: list[int] | None = None) -> list[RenderResult]:
        """
        Render multiple stems and return their analyses.

        If track_ids is None, reads the project state to find all active mixer tracks.
        """
        if track_ids is None:
            state = self._fl.read_project_state()
            track_ids = [
                t["index"]
                for t in state.get("mixer_tracks", [])
                if t["index"] != 0 and t.get("enabled", True)  # Skip master
            ]

        results = []
        for tid in track_ids:
            logger.info("Rendering stem for mixer track %d...", tid)
            result = self.render_stem(tid)
            results.append(result)

        return results

    def analyze_mix(self, stems: list[RenderResult] | None = None) -> dict[str, Any]:
        """
        Full mix analysis: render master + stems, detect masking conflicts.

        Returns a comprehensive analysis dict suitable for the agent.
        """
        # Render master
        logger.info("Rendering master mix...")
        master = self.render_master()

        # Render stems if not provided
        if stems is None:
            stems = self.render_all_stems()

        # Detect masking
        stem_analyses = [s.analysis for s in stems]
        masking = detect_masking(stem_analyses)

        return {
            "master": master.to_dict(),
            "stems": [s.to_dict() for s in stems],
            "masking_conflicts": masking,
            "summary": self._build_summary(master, stems, masking),
        }

    def _build_summary(
        self,
        master: RenderResult,
        stems: list[RenderResult],
        masking: list[dict],
    ) -> str:
        """Build a human-readable summary for the agent."""
        lines = [
            f"Master: LUFS={master.analysis.lufs:.1f}, Peak={master.analysis.true_peak_db:.1f}dB",
            f"Stems analyzed: {len(stems)}",
        ]

        if masking:
            lines.append(f"Masking conflicts found: {len(masking)}")
            for conflict in masking:
                band = conflict["band"]
                freq = conflict["frequency_range"]
                severity = conflict["severity"]
                stem_names = ", ".join(s["path"] for s in conflict["stems"][:3])
                lines.append(f"  - {band} ({freq[0]}-{freq[1]}Hz) [{severity}]: {stem_names}")
        else:
            lines.append("No significant masking conflicts detected.")

        return "\n".join(lines)

    def _trigger_render(self, output_path: Path) -> None:
        """
        Trigger FL Studio to render audio to output_path.

        Strategy priority:
        1. pywinauto (full control over export dialog) — Windows only
        2. Keyboard shortcut via FL device script
        3. Manual: ask the user to export
        """
        try:
            self._render_via_pywinauto(output_path)
        except ImportError:
            logger.warning(
                "pywinauto not available. Attempting keyboard shortcut method."
            )
            self._render_via_shortcut(output_path)
        except Exception as e:
            logger.error("Render failed: %s", e)
            raise RuntimeError(
                f"Could not trigger FL Studio render: {e}. "
                "You may need to export manually and provide the file path."
            ) from e

    def _render_via_pywinauto(self, output_path: Path) -> None:
        """
        Full dialog control via pywinauto: sets the output path explicitly.

        More reliable than the shortcut path because it can navigate the Save
        dialog and type the output path. Requires pywinauto (optional dep).

        NOTE: FL 2025 export shortcut is Ctrl+R (not Ctrl+Shift+R as earlier
        versions used). The dialog title pattern may need adjustment per version.
        """
        from pywinauto import Application, Desktop  # type: ignore[import-untyped]
        from pywinauto.keyboard import send_keys  # type: ignore[import-untyped]

        desktop = Desktop(backend="uia")
        fl_windows = [w for w in desktop.windows() if "FL Studio" in (w.window_text() or "")]
        if not fl_windows:
            raise RuntimeError("FL Studio window not found. Is FL Studio running?")

        fl_win = fl_windows[0]
        fl_win.set_focus()
        time.sleep(0.2)

        # Ctrl+R = Export WAV in FL 2025 (older versions may use Ctrl+Shift+R)
        send_keys("^r")
        time.sleep(1.5)

        # Find the Save/Export dialog
        try:
            app = Application(backend="uia").connect(title_re=".*FL Studio.*")
            export_dialog = app.window(title_re="(?i).*export.*|.*render.*|.*save.*wav.*")
            export_dialog.wait("visible", timeout=5)
        except Exception:
            raise RuntimeError(
                "Export dialog did not appear after Ctrl+R. "
                "Check FL's export shortcut setting, or use the user-assisted flow."
            )

        # Set the filename — clear existing and type the full path
        try:
            filename_edit = export_dialog.child_window(control_type="Edit", found_index=0)
            filename_edit.set_focus()
            send_keys("^a")
            filename_edit.type_keys(str(output_path), with_spaces=True)
        except Exception as e:
            raise RuntimeError(f"Could not set export path in dialog: {e}") from e

        # Click Start / Save
        for btn_title in ("Start", "Save", "OK", "Export"):
            try:
                btn = export_dialog.child_window(title=btn_title, control_type="Button")
                btn.click()
                break
            except Exception:
                continue
        else:
            raise RuntimeError("Could not find Start/Save button in export dialog.")

        logger.info("Render triggered via pywinauto dialog → %s", output_path)

    def _render_via_shortcut(self, output_path: Path) -> None:
        """
        Light-touch auto-render: focus FL, send Ctrl+R, send Enter.

        Relies on FL remembering the last export directory from a prior manual
        export. Works reliably once the output folder has been set at least once.

        FIRST-TIME SETUP (one-time per project):
          1. In FL: File → Export → WAV
          2. Navigate to:  <workspace>/stems/   (or masters/)
          3. Click Start once — FL saves this as the default path
          After that, Ctrl+R + Enter always exports to the same folder.

        Requirements: pywinauto (pip install studiomind[render])
        """
        try:
            from pywinauto import Desktop  # type: ignore[import-untyped]
            from pywinauto.keyboard import send_keys  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "Auto-render requires pywinauto. Install it with: "
                "pip install pywinauto\n"
                "Or export manually and use the user-assisted render flow."
            )

        # Find FL's window handle
        try:
            desktop = Desktop(backend="uia")
            fl_wins = [w for w in desktop.windows() if "FL Studio" in (w.window_text() or "")]
            if not fl_wins:
                raise RuntimeError("FL Studio window not found.")
            hwnd = fl_wins[0].handle
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Could not find FL Studio: {e}") from e

        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        WM_KEYDOWN = 0x0100
        WM_KEYUP   = 0x0101
        VK_CONTROL = 0x11
        VK_R       = 0x52
        VK_RETURN  = 0x0D

        # PostMessage sends keys directly to FL's message queue — no global keyboard
        # injection, browser focus is unaffected.
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_R, 0)
        time.sleep(0.05)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_R, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_CONTROL, 0)
        time.sleep(1.5)

        # Find and confirm the export dialog
        dialog_hwnd = None
        for w in desktop.windows():
            if any(kw in (w.window_text() or "").lower() for kw in ("export", "render", "save")):
                dialog_hwnd = w.handle
                break
        target = dialog_hwnd if dialog_hwnd else hwnd
        user32.PostMessageW(target, WM_KEYDOWN, VK_RETURN, 0)
        user32.PostMessageW(target, WM_KEYUP,   VK_RETURN, 0)

        logger.info(
            "Auto-render triggered via keyboard shortcut (Ctrl+R + Enter). "
            "Output should land in FL's last-used export folder. "
            "Expected path: %s",
            output_path,
        )

    def _wait_for_file(self, path: Path) -> None:
        """Poll the filesystem until the render output file appears and is complete."""
        deadline = time.monotonic() + self._config.poll_timeout_s
        last_size = -1

        while time.monotonic() < deadline:
            if path.exists():
                size = path.stat().st_size
                if size > 0 and size == last_size:
                    # File exists and size hasn't changed — render is done
                    logger.info("Render complete: %s (%d bytes)", path, size)
                    return
                last_size = size

            time.sleep(self._config.poll_interval_s)

        raise TimeoutError(
            f"Render did not complete within {self._config.poll_timeout_s}s. "
            f"Expected file at: {path}"
        )
