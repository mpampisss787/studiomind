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
from pathlib import Path
from typing import Any

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

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="StudioMind")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


def _resolve_active_project():
    """Detect the active FL project and return (project, error_reason).

    Reads the workspace manifest from disk only — does not require a live MIDI
    connection, so this is safe to call from a poll endpoint. If FL isn't
    running or doesn't expose a project, returns (None, reason).
    """
    from studiomind.fl_detect import detect_fl_project
    from studiomind.workspace import open_project

    os_name, os_title = detect_fl_project()
    if not os_name:
        if os_title:
            return None, f"FL is running but no project loaded (title: '{os_title}')."
        return None, "FL Studio not detected on this machine."
    return open_project(os_name), None


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

    return {
        "active": True,
        "project_name": project.name,
        "root": str(project.root),
        "fl_project_path": manifest.fl_project_path,
        "stems_dir": str(project.stems_dir),
        "masters_dir": str(project.masters_dir),
        "references_dir": str(project.references_dir),
        "stems": stems,
        "masters": masters,
        "references": references,
    }


@app.post("/api/workspace/reference")
async def upload_reference(file: UploadFile = File(...)):
    project, err = _resolve_active_project()
    if project is None:
        raise HTTPException(status_code=404, detail=err or "No active project.")

    # Only accept audio files. Lightweight extension check.
    allowed = {".wav", ".mp3", ".flac", ".aiff", ".aif", ".ogg", ".m4a"}
    filename = Path(file.filename or "reference.wav").name  # strip any path components
    if Path(filename).suffix.lower() not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio type. Allowed: {', '.join(sorted(allowed))}",
        )

    target = project.references_dir / filename
    contents = await file.read()
    # 100 MB cap for safety (long reference tracks are ~50 MB in WAV)
    if len(contents) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB).")
    target.write_bytes(contents)

    return {"ok": True, "filename": filename, "path": str(target), "size": len(contents)}


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


# ───────────────────────── Chat WebSocket ──────────────────────────

@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
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

                # Concurrently: drain the agent's event queue and watch for a stop message
                async def pump_queue():
                    while True:
                        msg = await queue.get()
                        await ws.send_json(msg)
                        if msg["type"] == "done":
                            return

                async def watch_for_stop():
                    """While the agent runs, read further WS frames. A 'stop' frame
                    signals cancellation; anything else we just ignore (the UI
                    shouldn't be sending new messages mid-turn)."""
                    while True:
                        frame = await ws.receive_json()
                        if frame.get("type") == "stop":
                            agent.request_stop()
                            await queue.put({"type": "stopping", "content": "Stopping..."})
                            return

                pump_task = asyncio.create_task(pump_queue())
                stop_task = asyncio.create_task(watch_for_stop())

                done, pending = await asyncio.wait(
                    {pump_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                # Whichever task didn't finish, cancel it
                for t in pending:
                    t.cancel()
                # Let the agent wrap up naturally so the conversation history stays consistent
                try:
                    await agent_task
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
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
