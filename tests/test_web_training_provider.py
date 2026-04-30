"""Tests for the websocket-driven ReadbackProvider.

Run a real asyncio loop in a background thread, drive ``request()``
from the test thread, ``submit()`` from the loop thread (or directly),
and confirm the bridge wakes correctly. No fastapi / websocket — just
the loop + a mock send_event coroutine.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from studiomind.web.training_provider import WsReadbackProvider


# ───────────────────────────── helpers ────────────────────────────────


class _LoopThread:
    """Spin up an asyncio loop in a background thread; tear down on stop()."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait for the loop to actually start running.
        for _ in range(100):
            if self.loop.is_running():
                break
            time.sleep(0.01)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)


@pytest.fixture
def loop_thread():
    lt = _LoopThread()
    try:
        yield lt
    finally:
        lt.stop()


# ───────────────────────────── happy path ─────────────────────────────


def test_request_blocks_then_submit_unblocks(loop_thread: _LoopThread) -> None:
    sent: list[dict] = []

    async def send_event(event: dict[str, Any]) -> None:
        sent.append(event)

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=2.0)

    result_holder: list[str] = []

    def agent_thread() -> None:
        result_holder.append(provider.request("Probe X at 0.25", expected_unit="dB"))

    t = threading.Thread(target=agent_thread)
    t.start()

    # Wait until the provider has actually sent the request_readback event.
    for _ in range(100):
        if sent:
            break
        time.sleep(0.01)
    assert sent, "send_event was never invoked"
    assert sent[0]["type"] == "request_readback"
    assert sent[0]["prompt"] == "Probe X at 0.25"
    assert sent[0]["expected_unit"] == "dB"

    assert provider.is_waiting

    # Submit the user's typed value.
    assert provider.submit("-30 dB") is True

    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result_holder == ["-30 dB"]
    assert not provider.is_waiting


def test_submit_with_no_pending_request_returns_false(loop_thread: _LoopThread) -> None:
    async def send_event(event: dict[str, Any]) -> None:
        pass

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=1.0)
    # Submit before any request — should be a no-op.
    assert provider.submit("ignored") is False


def test_cancel_unblocks_pending_request(loop_thread: _LoopThread) -> None:
    async def send_event(event: dict[str, Any]) -> None:
        pass

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=5.0)

    result: list[str] = []

    def agent_thread() -> None:
        result.append(provider.request("Probe"))

    t = threading.Thread(target=agent_thread)
    t.start()
    time.sleep(0.05)  # let the request start blocking
    provider.cancel()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert result == [""]


def test_request_after_cancel_returns_empty_immediately(loop_thread: _LoopThread) -> None:
    async def send_event(event: dict[str, Any]) -> None:
        pass

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=5.0)
    provider.cancel()
    assert provider.request("Probe") == ""


def test_request_timeout_returns_empty(loop_thread: _LoopThread) -> None:
    async def send_event(event: dict[str, Any]) -> None:
        pass

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=0.1)
    t0 = time.monotonic()
    result = provider.request("Probe with no answer")
    elapsed = time.monotonic() - t0
    assert result == ""
    assert 0.08 <= elapsed <= 1.5


def test_request_with_context_emits_structured_event(loop_thread: _LoopThread) -> None:
    """When a ReadbackContext is supplied, the request_readback event
    carries a structured `context` dict the UI can render as a richer
    prompt."""
    from studiomind.learning.calibration import ReadbackContext
    sent: list[dict] = []

    async def send_event(event):
        sent.append(event)

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=2.0)

    def agent_thread() -> None:
        ctx = ReadbackContext(
            phase="sweep", param_id=2, param_name="Mix",
            step="4/6", param_value=0.6, expected_unit="%",
        )
        provider.request("Sweep Mix step 0.60", expected_unit="%", context=ctx)

    t = threading.Thread(target=agent_thread)
    t.start()

    for _ in range(100):
        if sent: break
        time.sleep(0.01)

    provider.submit("60.0 %")
    t.join(timeout=2.0)

    assert sent[0]["type"] == "request_readback"
    assert "context" in sent[0]
    ctx = sent[0]["context"]
    assert ctx["phase"] == "sweep"
    assert ctx["param_id"] == 2
    assert ctx["param_name"] == "Mix"
    assert ctx["step"] == "4/6"
    assert abs(ctx["param_value"] - 0.6) < 1e-9
    assert ctx["expected_unit"] == "%"


def test_request_without_context_omits_field(loop_thread: _LoopThread) -> None:
    """If the caller doesn't pass a context, the WS event has no
    `context` key — UI falls back to the raw prompt string."""
    sent: list[dict] = []
    async def send_event(event): sent.append(event)
    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=2.0)

    def agent_thread() -> None:
        provider.request("Plain prompt")

    t = threading.Thread(target=agent_thread)
    t.start()
    for _ in range(100):
        if sent: break
        time.sleep(0.01)
    provider.submit("x")
    t.join(timeout=2.0)
    assert "context" not in sent[0]


def test_sequential_requests_each_get_their_own_value(loop_thread: _LoopThread) -> None:
    """The wizard makes one request at a time; each one should get
    exactly its own readback value."""
    sent: list[dict] = []

    async def send_event(event: dict[str, Any]) -> None:
        sent.append(event)

    provider = WsReadbackProvider(send_event, loop_thread.loop, timeout=2.0)
    answers = ["-30 dB", "-15 dB", "smooth"]
    received: list[str] = []

    def agent_thread() -> None:
        for _ in answers:
            received.append(provider.request("Probe"))

    t = threading.Thread(target=agent_thread)
    t.start()

    for expected in answers:
        # Wait for the next event to be sent.
        deadline = time.monotonic() + 2.0
        target_count = answers.index(expected) + 1
        while len(sent) < target_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert provider.submit(expected) is True

    t.join(timeout=3.0)
    assert not t.is_alive()
    assert received == answers
    assert len(sent) == len(answers)
