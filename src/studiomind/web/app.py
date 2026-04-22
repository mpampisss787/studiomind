"""
FastAPI + WebSocket server for StudioMind chat UI.

Streams agent messages in real-time to the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from studiomind.agent.loop import AgentConfig, AgentLoop
from studiomind.agent.tools import DESTRUCTIVE_TOOLS
from studiomind.bridge.commands import FLStudio

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="StudioMind")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_event_loop()

    # Connect to FL Studio
    try:
        fl = FLStudio()
        fl.connect()
        await ws.send_json({"type": "system", "content": "Connected to FL Studio"})
    except Exception as e:
        await ws.send_json({"type": "error", "content": f"Could not connect to FL Studio: {e}"})
        await ws.close()
        return

    # Message queue for streaming from sync agent to async websocket
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
    first_message = True

    try:
        while True:
            data = await ws.receive_json()

            if data.get("type") == "message":
                user_msg = data["content"]

                # Run agent in thread pool (it's synchronous + blocking)
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

                # Stream messages from queue to websocket until done
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
