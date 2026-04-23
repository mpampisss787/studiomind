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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from studiomind.agent.loop import AgentConfig, AgentLoop
from studiomind.bridge.commands import FLStudio
from studiomind.config import (
    get_anthropic_key,
    key_preview,
    key_source,
    set_anthropic_key,
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


class ApiKeyPayload(BaseModel):
    anthropic_api_key: str = Field(..., min_length=8)


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    return SettingsResponse(
        configured=bool(get_anthropic_key()),
        source=key_source(),
        key_preview=key_preview(),
    )


@app.post("/api/settings")
async def post_settings(payload: ApiKeyPayload):
    key = payload.anthropic_api_key.strip()
    if not key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Anthropic API keys should start with 'sk-'.")
    set_anthropic_key(key)
    return {
        "ok": True,
        "source": key_source(),
        "key_preview": key_preview(),
    }


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

        config = AgentConfig(
            model="claude-sonnet-4-5-20250929",
            auto_approve=True,
            on_message=on_message,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )

        agent = AgentLoop(fl, config)
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

                while True:
                    msg = await queue.get()
                    if msg["type"] == "done":
                        await ws.send_json(msg)
                        break
                    await ws.send_json(msg)

                await agent_task

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    finally:
        try:
            fl.disconnect()
        except Exception:
            pass


def start(host: str = "127.0.0.1", port: int = 8040):
    """Start the StudioMind web server."""
    import uvicorn
    import webbrowser

    print(f"\n  StudioMind Web UI: http://{host}:{port}\n")
    webbrowser.open(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
