"""Websocket-driven ReadbackProvider for training mode.

The training agent calls ``provider.request(prompt, expected_unit)``
from an executor thread (the agent loop runs synchronously). This
provider bridges that sync call to the asyncio side of the FastAPI
WebSocket so the UI can render an input box and POST the user's
typed value back over the same socket.

Flow:

  1. Agent thread calls ``request(prompt)``.
  2. We allocate a per-call :class:`concurrent.futures.Future`,
     schedule a ``request_readback`` event over the WS via
     ``asyncio.run_coroutine_threadsafe``, then ``future.result()``
     blocks the agent thread.
  3. UI shows the input. User types a value. UI sends
     ``{type: "readback", value: "..."}`` over the WS.
  4. The WS receive coroutine calls ``provider.submit(value)``, which
     sets the future's result. The agent thread unblocks and returns
     the value.

Cancellation: on WS disconnect, agent stop, or timeout, the future
is satisfied with an empty string so ``request()`` always returns —
the agent treats empty as a parse failure and surfaces it to the user.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


SendEventFn = Callable[[dict[str, Any]], Awaitable[None]]


class WsReadbackProvider:
    """Sync→async bridge implementing the ReadbackProvider protocol.

    Single-flight: the wizard is sequential, so at most one
    ``request()`` is pending at a time. Concurrent ``request()`` from
    multiple threads is *not* supported — the agent thread is the only
    caller.
    """

    def __init__(
        self,
        send_event: SendEventFn,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = 600.0,
    ) -> None:
        self._send_event = send_event
        self._loop = loop
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pending: "Future[str] | None" = None
        self._cancelled = False

    # ───────────────────────────── ReadbackProvider ───────────────────

    def request(self, prompt: str, *, expected_unit: str = "") -> str:
        """Block the agent thread until the UI submits a readback.

        Returns the empty string on cancel or timeout — the calibration
        layer treats an unparseable readback as actionable feedback the
        agent can surface back to the user, not a hard error.
        """
        with self._lock:
            if self._cancelled:
                return ""
            future: "Future[str]" = Future()
            self._pending = future

        event = {
            "type": "request_readback",
            "prompt": prompt,
            "expected_unit": expected_unit,
        }
        try:
            asyncio.run_coroutine_threadsafe(self._send_event(event), self._loop)
        except RuntimeError as e:
            # Loop already shut down — nothing to wait on.
            log.warning("WsReadbackProvider: failed to schedule send: %s", e)
            with self._lock:
                if self._pending is future:
                    self._pending = None
            return ""

        try:
            return future.result(timeout=self._timeout)
        except TimeoutError:
            log.warning("WsReadbackProvider: timed out after %.0fs", self._timeout)
            return ""
        except Exception as e:  # pragma: no cover — defensive
            log.exception("WsReadbackProvider: unexpected wait error: %s", e)
            return ""
        finally:
            with self._lock:
                if self._pending is future:
                    self._pending = None

    # ───────────────────────────── UI hooks ───────────────────────────

    def submit(self, value: str) -> bool:
        """The WS receive coroutine calls this when ``{type:"readback",
        value}`` lands. Returns True if a pending request was waiting
        for it; False when the message arrives without a request open
        (likely a UI bug or a stale message — log + drop)."""
        with self._lock:
            future = self._pending
        if future is None or future.done():
            log.debug(
                "WsReadbackProvider: submit(%r) with no pending request — dropping",
                value,
            )
            return False
        future.set_result(value)
        return True

    def cancel(self) -> None:
        """Disconnect / stop hook. Wakes any pending request with an
        empty value and refuses future requests."""
        with self._lock:
            self._cancelled = True
            future = self._pending
            self._pending = None
        if future is not None and not future.done():
            future.set_result("")

    # ───────────────────────────── introspection ──────────────────────

    @property
    def is_waiting(self) -> bool:
        with self._lock:
            return self._pending is not None and not self._pending.done()
