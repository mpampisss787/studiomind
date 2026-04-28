"""
FastAPI + WebSocket server for StudioMind chat UI.

Streams agent messages in real-time to the browser. Also exposes a small
settings API for the browser to configure the Anthropic API key without
touching env vars.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal, get_args

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from studiomind.agent.loop import AgentConfig, AgentLoop
from studiomind.bridge.commands import FLStudio
from studiomind.config import (
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    get_anthropic_key,
    get_model,
    key_preview,
    key_source,
    model_source,
    set_anthropic_key,
    set_model,
)
from studiomind.logging_setup import configure_session_logging

# Idempotent — covers uvicorn reload-mode forks where the child never went
# through the CLI's main(). Returns immediately if the file handler is
# already installed.
configure_session_logging()

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# One active agent session at a time. The Stop endpoint uses this to signal
# the running agent without touching the WebSocket.
_active_stop_event: threading.Event | None = None

# Every connected client's WebSocket — used to broadcast `file_dropped`
# events so any open tab sees the routing pill in its chat thread.
_active_websockets: "set[WebSocket]" = set()

app = FastAPI(title="StudioMind")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


DropFolder = Literal["stems", "masters", "references", "drops"]
_VALID_DROP_FOLDERS: tuple[DropFolder, ...] = get_args(DropFolder)


def _folder_dir(project, folder: DropFolder) -> Path:
    """Resolve a folder name (`stems`, `masters`, etc.) to its Path on the project."""
    return {
        "stems": project.stems_dir,
        "masters": project.masters_dir,
        "references": project.references_dir,
        "drops": project.drops_dir,
    }[folder]


def _classify_drop(
    filename: str,
    contents: bytes,
    intent_hint: str | None,
    project_name: str | None = None,
) -> tuple[DropFolder, str]:
    """Return `(folder, classification_reason)` for a freshly-dropped file.

    Order of precedence:
      1. Explicit `intent_hint` from the UI / agent context
      2. Filename heuristics — only the high-confidence ones:
          - `<project>_<anything>.wav` (FL native export) → stems
          - master/mix/bounce/mixdown in stem              → masters
          - ref_/`_ref` / reference in stem               → references
      3. Audio-info heuristics for native formats:
          - short mono (< 5 s)                            → drops (likely a sample)
      4. Default                                           → drops/

    NOTE: there is intentionally no auto-route to ``references/`` based on
    duration / channel count. Anything dragged in from FL Studio (a stem,
    a clip, an in-progress mix) is typically stereo and longer than a
    minute — routing such files to ``references/`` was the wrong default.
    References are *opt-in*: drop on the sidebar's references zone, name
    the file with ``ref_*`` / ``reference*``, or pass ``intent_hint``.
    Everything else lands in ``drops/`` where the user can move it to its
    real home in one click via the Drops sidebar's Move menu.
    """
    if intent_hint == "reference":
        return "references", "agent_or_ui_hinted_reference"
    if intent_hint == "master":
        return "masters", "agent_or_ui_hinted_master"
    if intent_hint == "stem":
        return "stems", "agent_or_ui_hinted_stem"

    stem_lower = Path(filename).stem.lower()

    # FL Studio exports stems as "<ProjectName>_<TrackName>.wav".
    # If the active project name is known and the filename starts with
    # "<project>_", this is almost certainly an FL-rendered stem.
    if project_name:
        project_slug = (
            project_name.lower()
            .replace(" ", "_")
            .replace("-", "_")
        )
        if stem_lower.startswith(project_slug + "_"):
            return "stems", "fl_project_prefix_match"

    if (
        "master" in stem_lower
        or stem_lower.endswith("_mix")
        or "bounce" in stem_lower
        or "_mixdown" in stem_lower
    ):
        return "masters", "filename_match_master"

    if stem_lower.startswith("ref_") or "_ref" in stem_lower or "reference" in stem_lower:
        return "references", "filename_match_reference"

    # Audio-info heuristics (native formats only — non-WAV needs ffmpeg-decode
    # to inspect duration; we skip that on the hot path).
    suffix = Path(filename).suffix.lower()
    if suffix in (".wav", ".flac", ".aiff", ".aif", ".ogg"):
        try:
            import io
            import soundfile as _sf
            info = _sf.info(io.BytesIO(contents))
            duration = float(info.duration)
            channels = int(info.channels)
            if channels == 1 and duration < 5:
                return "drops", "short_mono_likely_sample"
        except Exception:
            pass  # fall through to default

    return "drops", "no_clear_signal"


async def _broadcast_event(event: dict) -> None:
    """Push a JSON event to every connected client. Drops dead sockets."""
    logger.debug(
        "WS broadcast: type=%s clients=%d payload_keys=%s",
        event.get("type"),
        len(_active_websockets),
        sorted(event.keys()),
    )
    dead: list[WebSocket] = []
    for ws in list(_active_websockets):
        try:
            await ws.send_json(event)
        except Exception as exc:
            logger.debug("WS send failed (will discard): %s", exc)
            dead.append(ws)
    for ws in dead:
        _active_websockets.discard(ws)


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ───────────────────────── Settings API ──────────────────────────

class SettingsResponse(BaseModel):
    configured: bool
    source: str  # "env" | "config" | "none"
    key_preview: str | None
    model: str
    model_source: str  # "env" | "config" | "default"
    available_models: list[dict[str, str]]


class SettingsPayload(BaseModel):
    anthropic_api_key: str | None = Field(default=None, min_length=8)
    model: str | None = Field(default=None, min_length=1)


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    return SettingsResponse(
        configured=bool(get_anthropic_key()),
        source=key_source(),
        key_preview=key_preview(),
        model=get_model(),
        model_source=model_source(),
        available_models=AVAILABLE_MODELS,
    )


@app.post("/api/stop")
async def stop_agent():
    """Signal the currently running agent to stop at its next check point."""
    global _active_stop_event
    if _active_stop_event:
        _active_stop_event.set()
    return {"ok": True}


@app.post("/api/settings")
async def post_settings(payload: SettingsPayload):
    updated: dict[str, str] = {}
    if payload.anthropic_api_key is not None:
        key = payload.anthropic_api_key.strip()
        if not key.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Anthropic API keys should start with 'sk-'.")
        set_anthropic_key(key)
        updated["api_key"] = "saved"
    if payload.model is not None:
        allowed_ids = {m["id"] for m in AVAILABLE_MODELS}
        model = payload.model.strip()
        # Power users can type their own model string; warn but allow
        if model not in allowed_ids and not model.startswith("claude-"):
            raise HTTPException(status_code=400, detail=f"Unrecognized model id: {model}")
        set_model(model)
        updated["model"] = model
    if not updated:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    return {
        "ok": True,
        "updated": updated,
        "source": key_source(),
        "key_preview": key_preview(),
        "model": get_model(),
        "model_source": model_source(),
    }


# ───────────────────────── Workspace API ──────────────────────────


# Last successfully detected project name + its workspace root. Used as a
# fallback when FL is temporarily undetectable (render dialog in foreground,
# main window minimised during export, etc.) so the sidebar doesn't blank out.
_last_detected_project_name: str | None = None
_last_detected_project_root: "Path | None" = None


def _resolve_active_project():
    """Detect the active FL project and return (project, error_reason).

    Reads the workspace manifest from disk only — does not require a live MIDI
    connection, so this is safe to call from a poll endpoint. If FL isn't
    running or doesn't expose a project, returns (None, reason).

    Falls back to the last known project when FL temporarily becomes
    undetectable — most commonly during Ctrl+R rendering, when FL minimises
    its main window and the title-scanner can't find it. As long as the
    workspace folder still exists on disk, we keep showing it rather than
    blanking the sidebar with "Waiting for FL Studio...".
    """
    global _last_detected_project_name, _last_detected_project_root

    from studiomind.fl_detect import detect_fl_project
    from studiomind.workspace import open_project

    os_name, os_title = detect_fl_project()

    if os_name:
        # Successful detection — update the cache.
        project = open_project(os_name)
        _last_detected_project_name = os_name
        _last_detected_project_root = project.root
        return project, None

    # Detection failed. Check whether to fall back to the cached project.
    if _last_detected_project_name and _last_detected_project_root:
        if _last_detected_project_root.exists():
            # Workspace folder still on disk → FL is likely mid-render.
            # Return the cached project so the sidebar stays populated.
            return open_project(_last_detected_project_name), None

    # No cache or the workspace folder was deleted (project changed/closed).
    if os_title:
        return None, f"FL is running but project title not recognised (title: '{os_title}'). Still rendering?"
    return None, "FL Studio not detected — open a project in FL first."


@app.get("/api/workspace/status")
async def workspace_status():
    project, err = _resolve_active_project()
    if project is None:
        return {"active": False, "reason": err}

    manifest = project.load_manifest()

    # Try reconciling manifest against disk. Catch any error so a broken
    # reconcile never prevents the status response from being returned.
    try:
        if project.reconcile_with_filesystem(manifest):
            project.save_manifest(manifest)
    except Exception as exc:
        logger.warning("reconcile_with_filesystem failed: %s", exc)

    # STEMS: filesystem-first — same logic as masters.  Scan stems/ directory
    # and look up manifest for metadata.  Deleted files simply don't appear.
    stems = []
    if project.stems_dir.exists():
        manifest_stems_by_name = {
            rec.filename: rec for rec in manifest.stems.values() if rec.filename
        }
        for wav in sorted(
            project.stems_dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            rec = manifest_stems_by_name.get(wav.name)
            if rec:
                stems.append(rec.to_dict())
            else:
                # WAV in stems/ with no manifest entry — show without analysis
                stems.append({
                    "kind": "stem",
                    "filename": wav.name,
                    "status": "ready",
                    "track_id": None,
                    "track_name": wav.stem,
                    "fl_state_hash": None,
                    "rendered_at": wav.stat().st_mtime,
                    "analysis": None,
                })

    # MASTERS: filesystem-first — scan the actual directory, then look up
    # manifest for analysis data.  If the file is gone it simply won't appear,
    # no matter what the manifest says.
    masters = []
    if project.masters_dir.exists():
        manifest_by_name = {r.filename: r for r in manifest.masters}
        for wav in sorted(
            project.masters_dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,   # newest first
        ):
            rec = manifest_by_name.get(wav.name)
            if rec:
                masters.append(rec.to_dict())
            else:
                # File on disk with no manifest entry (e.g., user copied it in)
                masters.append({
                    "kind": "master",
                    "filename": wav.name,
                    "status": "ready",
                    "track_id": None,
                    "track_name": None,
                    "fl_state_hash": None,
                    "rendered_at": wav.stat().st_mtime,
                    "analysis": None,
                })
    references = (
        sorted(p.name for p in project.references_dir.iterdir() if p.is_file())
        if project.references_dir.exists()
        else []
    )
    drops = (
        sorted(p.name for p in project.drops_dir.iterdir() if p.is_file())
        if project.drops_dir.exists()
        else []
    )

    # Sanitize any legacy -inf/nan values in existing analysis dicts so JSON
    # encoding doesn't explode on records written before the analyzer had a
    # floor at -120. Walks nested dicts; scalars only.
    import math as _math
    def _sanitize(obj):
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, float) and not _math.isfinite(obj):
            return -120.0
        return obj

    return _sanitize({
        "active": True,
        "project_name": project.name,
        "root": str(project.root),
        "fl_project_path": manifest.fl_project_path,
        "stems_dir": str(project.stems_dir),
        "masters_dir": str(project.masters_dir),
        "references_dir": str(project.references_dir),
        "drops_dir": str(project.drops_dir),
        "stems": stems,
        "masters": masters,
        "references": references,
        "drops": drops,
    })


@app.post("/api/workspace/upload")
async def upload_audio(
    file: UploadFile = File(...),
    target_folder: str | None = None,
    intent_hint: str | None = None,
):
    """Smart-routed audio drop endpoint.

    `target_folder` (optional, one of `stems` / `masters` / `references` /
    `drops`) skips the classifier. `intent_hint` lets the UI pass agent
    context (e.g. "reference") for soft routing without hard-coding a
    folder. Default behaviour is the auto-classifier.

    Returns `{folder, filename, classification_reason, alternative_folders,
    size_bytes}`. Broadcasts a `file_dropped` event on the WebSocket so any
    open chat thread can render its routing pill.
    """
    from studiomind.ingest.decode import is_supported

    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")

    filename = Path(file.filename or "drop.wav").name  # strip path components
    if not is_supported(filename):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio type: {Path(filename).suffix or '(none)'}.",
        )

    contents = await file.read()
    # 200 MB cap (covers a long stereo reference WAV)
    if len(contents) > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 200 MB).")

    if target_folder and target_folder in _VALID_DROP_FOLDERS:
        folder, reason = target_folder, "user_override"
    else:
        folder, reason = _classify_drop(filename, contents, intent_hint, project_name=project.name)

    logger.info(
        "drop classified: filename=%r size=%dB intent_hint=%r → folder=%s reason=%s",
        filename, len(contents), intent_hint, folder, reason,
    )

    target_dir = _folder_dir(project, folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    dest.write_bytes(contents)

    payload = {
        "ok": True,
        "filename": filename,
        "folder": folder,
        "path": str(dest),
        "classification_reason": reason,
        "alternative_folders": [f for f in _VALID_DROP_FOLDERS if f != folder],
        "size_bytes": len(contents),
    }

    await _broadcast_event({"type": "file_dropped", **payload})
    return payload


@app.post("/api/workspace/reference")
async def upload_reference(file: UploadFile = File(...)):
    """Backward-compatible alias: always routes the file to `references/`."""
    return await upload_audio(file=file, target_folder="references", intent_hint=None)


class RelocateBody(BaseModel):
    filename: str
    from_folder: DropFolder
    to_folder: DropFolder


@app.post("/api/workspace/relocate")
async def relocate_drop(body: RelocateBody):
    """Move a file between drop folders (used by the UI's override pill)."""
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")

    name = Path(body.filename).name  # path-traversal guard
    src = _folder_dir(project, body.from_folder) / name
    dst_dir = _folder_dir(project, body.to_folder)
    dst = dst_dir / name

    if not src.exists() or not src.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"File not found: {body.from_folder}/{name}",
        )
    dst_dir.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    logger.info(
        "drop relocated: %s → %s (filename=%r)",
        body.from_folder, body.to_folder, name,
    )

    await _broadcast_event({
        "type": "file_relocated",
        "filename": name,
        "from_folder": body.from_folder,
        "to_folder": body.to_folder,
    })
    return {"ok": True, "filename": name, "from": body.from_folder, "to": body.to_folder}


@app.get("/api/workspace/notes")
async def get_notes():
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")
    return {
        "ok": True,
        "project_name": project.name,
        "notes": project.read_notes(),
        "path": str(project.notes_path),
    }


@app.put("/api/workspace/notes")
async def put_notes(body: dict):
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")
    content = body.get("content", "")
    project.ensure_dirs()
    project.notes_path.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.get("/api/workspace/history")
async def get_history():
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")
    return {
        "ok": True,
        "project_name": project.name,
        "history": project.read_history(max_entries=50),
        "path": str(project.history_path),
    }


@app.delete("/api/workspace/reference/{filename}")
async def delete_reference(filename: str):
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")

    # Prevent path-traversal; only allow deletion of files in references/
    clean = Path(filename).name
    target = project.references_dir / clean
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in references.")
    target.unlink()
    return {"ok": True, "filename": clean}


@app.delete("/api/workspace/drop/{filename}")
async def delete_drop(filename: str):
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")

    # Prevent path-traversal; only allow deletion of files in drops/
    clean = Path(filename).name
    target = project.drops_dir / clean
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in drops.")
    target.unlink()
    return {"ok": True, "filename": clean}


# ───────────────────────── Chat WebSocket ──────────────────────────

@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    _active_websockets.add(ws)
    loop = asyncio.get_event_loop()

    # Gate 1: API key must be configured before we even try to connect.
    if not get_anthropic_key():
        await ws.send_json({
            "type": "needs_setup",
            "content": "Please configure your Anthropic API key to start chatting.",
        })
        await ws.close()
        return

    # Gate 2: FL Studio must be reachable over MIDI.
    try:
        fl = FLStudio()

        # Wire reconnect notifications so the user sees status changes in the chat
        # rather than a silent freeze or a cryptic error.
        def _on_midi_disconnect(attempt: int) -> None:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({
                    "type": "system",
                    "content": f"FL Studio disconnected — reconnecting (attempt {attempt})...",
                }),
                loop,
            )

        def _on_midi_reconnect() -> None:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "system", "content": "Reconnected to FL Studio."}),
                loop,
            )

        fl._client._on_disconnect = _on_midi_disconnect
        fl._client._on_reconnect  = _on_midi_reconnect

        fl.connect()
        await ws.send_json({"type": "system", "content": "Connected to FL Studio"})
    except Exception as e:
        await ws.send_json({"type": "error", "content": f"Could not connect to FL Studio: {e}"})
        await ws.close()
        return

    # Gate 3: agent init (can fail if API key was just deleted or is bad).
    try:
        queue: asyncio.Queue = asyncio.Queue()

        def on_message(text: str) -> None:
            asyncio.run_coroutine_threadsafe(queue.put({"type": "assistant", "content": text}), loop)

        def on_tool_call(tool_name: str, tool_input: dict) -> bool:
            preview = json.dumps(tool_input, default=str)
            if len(preview) > 150:
                preview = preview[:150] + "..."
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "tool_call", "tool": tool_name, "input": preview}), loop
            )
            return True  # Auto-approve in web UI

        def on_tool_result(tool_name: str, result: Any) -> None:
            preview = json.dumps(result, default=str)
            if len(preview) > 300:
                preview = preview[:300] + "..."
            asyncio.run_coroutine_threadsafe(
                queue.put({"type": "tool_result", "tool": tool_name, "result": preview}), loop
            )

        # Don't pass a model explicitly — AgentConfig pulls from persistent config,
        # so model changes in the web UI take effect on the next agent session.
        config = AgentConfig(
            auto_approve=True,
            on_message=on_message,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )
        await ws.send_json(
            {"type": "system", "content": f"Model: {get_model()}"}
        )

        # Open (or create) the active project workspace so render tools work
        workspace = None
        try:
            from studiomind.cli import _open_active_workspace

            workspace = _open_active_workspace(fl)
            await ws.send_json(
                {"type": "system", "content": f"Active project: {workspace.project.name}"}
            )
        except Exception as e:
            await ws.send_json(
                {"type": "system", "content": f"Project auto-detect failed ({e}); render tools will error until fixed."}
            )

        agent = AgentLoop(fl, config, workspace=workspace)
    except Exception as e:
        await ws.send_json({"type": "error", "content": f"Agent init failed: {e}"})
        try:
            fl.disconnect()
        except Exception:
            pass
        await ws.close()
        return

    # Register this session's stop event so POST /api/stop can reach it
    global _active_stop_event
    _active_stop_event = agent._stop_event

    first_message = True

    try:
        while True:
            data = await ws.receive_json()

            if data.get("type") == "message":
                user_msg = data["content"]

                async def run_agent():
                    nonlocal first_message
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda: agent.run(user_msg, continue_conversation=not first_message),
                        )
                        first_message = False
                    except Exception as e:
                        await queue.put({"type": "error", "content": str(e)})
                    finally:
                        await queue.put({"type": "done"})

                agent_task = asyncio.create_task(run_agent())

                # Drain the agent's event queue to the client. Stop signals arrive
                # via POST /api/stop (not via WebSocket), so there is no concurrent
                # ws.receive_json() here — that was the source of connection drops.
                while True:
                    msg = await queue.get()
                    await ws.send_json(msg)
                    if msg["type"] == "done":
                        break

                await agent_task

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
        _active_websockets.discard(ws)
        # Auto-save the agent's last response to history.md so the NEXT session
        # can read it and pick up where this one left off — even if the agent
        # never got a chance to call write_history_entry itself (e.g., connection
        # dropped right after presenting options A/B/C/D).
        try:
            if workspace is not None and "agent" in dir():
                last = agent.last_text_response
                if last and len(last.strip()) > 20:
                    # Truncate very long responses; keep enough for the next
                    # session to understand the context (options, diagnoses, etc.)
                    snippet = last.strip()[:1200]
                    if len(last.strip()) > 1200:
                        snippet += "\n...[truncated]"
                    workspace.project.append_history_entry(
                        f"[Auto-saved on disconnect — last agent response:]\n\n{snippet}"
                    )
        except Exception as _e:
            logger.debug("Auto-save on disconnect failed: %s", _e)
        try:
            if workspace is not None:
                workspace.stop()
        except Exception:
            pass
        try:
            fl.disconnect()
        except Exception:
            pass


def start(host: str = "127.0.0.1", port: int = 8040, reload: bool = False):
    """Start the StudioMind web server."""
    import uvicorn
    import webbrowser

    print(f"\n  StudioMind Web UI: http://{host}:{port}\n")
    if reload:
        print("  Auto-reload ON — server restarts on code changes.\n")
    webbrowser.open(f"http://{host}:{port}")
    uvicorn.run(
        "studiomind.web.app:app",
        host=host,
        port=port,
        log_level="warning",
        reload=reload,
    )
