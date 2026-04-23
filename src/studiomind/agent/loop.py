"""
The StudioMind agent loop.

Orchestrates the plan → act → verify → iterate cycle using Claude's tool use.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from studiomind.agent.prompt import build_system_prompt
from studiomind.agent.tools import (
    DESTRUCTIVE_TOOLS,
    TOOL_SCHEMAS,
    ToolExecutor,
)
from studiomind.bridge.commands import FLStudio
from studiomind.workspace import WorkspaceSession

logger = logging.getLogger(__name__)

# Default model is sourced from studiomind.config at AgentConfig construction time
# so config changes take effect on the next agent session without a restart.
MAX_TURNS = 30  # Safety limit on agent loop iterations


@dataclass
class ActionLog:
    """Record of actions taken during an agent session."""

    entries: list[dict] = field(default_factory=list)

    def add(self, tool_name: str, tool_input: dict, result: Any, duration_ms: int) -> None:
        self.entries.append({
            "tool": tool_name,
            "input": tool_input,
            "result": result,
            "duration_ms": duration_ms,
            "timestamp": time.time(),
        })

    def summary(self) -> str:
        """Human-readable summary of actions taken."""
        if not self.entries:
            return "No actions taken."
        lines = []
        for i, e in enumerate(self.entries, 1):
            tool = e["tool"]
            duration = e["duration_ms"]
            lines.append(f"  {i}. {tool} ({duration}ms)")
        return f"Actions taken ({len(self.entries)} total):\n" + "\n".join(lines)


def _default_model() -> str:
    """Pull the active model out of persistent config each time an AgentConfig is made."""
    from studiomind.config import get_model

    return get_model()


@dataclass
class AgentConfig:
    """Configuration for the agent loop."""

    model: str = field(default_factory=_default_model)
    max_turns: int = MAX_TURNS
    auto_approve: bool = False  # If True, skip user confirmation for destructive actions
    on_message: Callable[[str], None] | None = None  # Callback for agent text output
    on_tool_call: Callable[[str, dict], bool] | None = None  # Callback before tool execution, return False to block
    on_tool_result: Callable[[str, Any], None] | None = None  # Callback after tool execution


class AgentLoop:
    """
    The core StudioMind agent loop.

    Connects Claude (via Anthropic API) to FL Studio (via the bridge),
    executing the plan → act → verify → iterate cycle.
    """

    def __init__(
        self,
        fl: FLStudio,
        config: AgentConfig | None = None,
        workspace: WorkspaceSession | None = None,
    ) -> None:
        self._fl = fl
        self._config = config or AgentConfig()
        self._workspace = workspace
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package is required for the agent loop. "
                "Install it with: pip install anthropic"
            )
        from studiomind.config import get_anthropic_key
        api_key = get_anthropic_key()
        if not api_key:
            raise RuntimeError(
                "No Anthropic API key configured. Set the ANTHROPIC_API_KEY "
                "environment variable, or save one in the web UI settings."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._stop_event = threading.Event()
        self._executor = ToolExecutor(fl, workspace=workspace, stop_event=self._stop_event)
        self._action_log = ActionLog()

    @property
    def action_log(self) -> ActionLog:
        return self._action_log

    def request_stop(self) -> None:
        """Signal the agent to stop at the next check point (between turns and inside blocking tools)."""
        self._stop_event.set()

    def run(self, user_goal: str, continue_conversation: bool = False) -> str:
        """
        Run the agent loop for a user goal.

        Args:
            user_goal: Natural language instruction (e.g., "Mix this professionally")
            continue_conversation: If True, append to existing conversation history

        Returns:
            The agent's final text response (summary of what was done)
        """
        self._action_log = ActionLog()
        self._stop_event.clear()

        system = build_system_prompt()

        if continue_conversation and hasattr(self, "_conversation_history"):
            self._conversation_history.append({"role": "user", "content": user_goal})
            messages = self._conversation_history
        else:
            messages = [{"role": "user", "content": user_goal}]
            self._conversation_history = messages

        # Prompt caching: wrap the system prompt as a cached content block and mark
        # the last tool with cache_control so the whole tool list is cached too.
        # This drops per-turn input token cost ~90% for cached bytes and dramatically
        # lowers rate-limit pressure since cache hits don't count against token-rate
        # limits the same way. Cache lives for 5 minutes from last use.
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in TOOL_SCHEMAS
        ]
        if tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

        final_text = ""

        for turn in range(self._config.max_turns):
            if self._stop_event.is_set():
                logger.info("Agent stopped by user at turn %d", turn + 1)
                if self._config.on_message:
                    self._config.on_message("[Stopped by user.]")
                break
            logger.info("Agent turn %d/%d", turn + 1, self._config.max_turns)

            response = self._api_call_with_retry(system_blocks, tools, messages)

            # Collect text and tool_use blocks
            assistant_content = response.content
            text_parts = []
            tool_calls = []

            for block in assistant_content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            # Stream text to user
            if text_parts:
                text = "\n".join(text_parts)
                final_text = text
                if self._config.on_message:
                    self._config.on_message(text)

            # If no tool calls, the agent is done
            if not tool_calls:
                logger.info("Agent finished (no more tool calls)")
                break

            # Process tool calls
            tool_results = []
            for tool_call in tool_calls:
                result = self._execute_tool(tool_call.name, tool_call.input, tool_call.id)
                tool_results.append(result)

            # Append assistant message + tool results to conversation
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            # Check stop reason
            if response.stop_reason == "end_turn":
                logger.info("Agent finished (end_turn)")
                break
        else:
            logger.warning("Agent hit max turns limit (%d)", self._config.max_turns)
            if self._config.on_message:
                self._config.on_message(
                    f"\n[StudioMind reached the {self._config.max_turns}-turn limit. "
                    "Stopping here. You can continue with another command.]"
                )

        return final_text

    def _api_call_with_retry(self, system: Any, tools: list, messages: list, max_retries: int = 5) -> Any:
        """
        Call the Anthropic API, honoring the server's retry-after on rate limits
        and overloaded responses instead of blind exponential backoff.
        """
        import anthropic

        last_error: Exception | None = None
        for attempt in range(max_retries):
            if self._stop_event.is_set():
                # User pressed Stop during a retry wait — bail out fast
                raise RuntimeError("Stopped by user during retry wait.")
            try:
                return self._client.messages.create(
                    model=self._config.model,
                    max_tokens=4096,
                    system=system,
                    tools=tools,
                    # Force one tool call per turn. Without this, the model can
                    # emit 5 parallel tool_use blocks in a single response —
                    # which on destructive edits fires a flurry of rapid API
                    # round-trips and trips RPM limits. Sequential is safer and
                    # gives us turn-by-turn control over pacing and Stop.
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                    messages=messages,
                )
            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                # Only retry on 429 (rate limit) and 529 (overloaded)
                status = getattr(e, "status_code", None)
                if status not in (429, 529) and not isinstance(e, anthropic.RateLimitError):
                    raise
                last_error = e
                if attempt == max_retries - 1:
                    raise
                wait = self._retry_wait_seconds(e, attempt)
                reason = "Rate limited" if status == 429 or isinstance(e, anthropic.RateLimitError) else "API overloaded"
                logger.warning(
                    "%s. Waiting %.1fs before retry %d/%d...", reason, wait, attempt + 1, max_retries
                )
                if self._config.on_message:
                    self._config.on_message(
                        f"[{reason} — waiting {wait:.0f}s before retry ({attempt + 1}/{max_retries})...]"
                    )
                # Sleep in small increments so a Stop click can interrupt quickly
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    if self._stop_event.is_set():
                        raise RuntimeError("Stopped by user during retry wait.")
                    time.sleep(min(0.5, deadline - time.monotonic()))
        # Exhausted retries
        if last_error is not None:
            raise last_error

    @staticmethod
    def _retry_wait_seconds(err: Exception, attempt: int) -> float:
        """
        Prefer the server's retry-after header; fall back to exponential backoff.
        Capped at 120s to avoid wedging the UI on a buggy upstream.
        """
        retry_after: str | None = None
        response = getattr(err, "response", None)
        if response is not None:
            headers = getattr(response, "headers", None) or {}
            # httpx headers are case-insensitive; fall back if exotic
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except (TypeError, ValueError):
                pass
        # Exponential backoff: 5s, 10s, 20s, 40s, 80s
        return min(2 ** attempt * 5, 120.0)

    def _execute_tool(self, tool_name: str, tool_input: dict, tool_use_id: str) -> dict:
        """Execute a single tool call with safety checks."""

        # Preview gate: ask user before destructive actions
        if tool_name in DESTRUCTIVE_TOOLS and not self._config.auto_approve:
            if self._config.on_tool_call:
                approved = self._config.on_tool_call(tool_name, tool_input)
                if not approved:
                    return {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps({"error": "User declined this action."}),
                    }

        # Execute
        start = time.monotonic()
        try:
            result = self._executor.execute(tool_name, tool_input)
            duration_ms = int((time.monotonic() - start) * 1000)

            self._action_log.add(tool_name, tool_input, result, duration_ms)

            if self._config.on_tool_result:
                self._config.on_tool_result(tool_name, result)

            logger.info("Tool %s completed in %dms", tool_name, duration_ms)

            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps(result, default=str),
            }

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error("Tool %s failed: %s", tool_name, e)

            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": json.dumps({"error": str(e)}),
                "is_error": True,
            }
