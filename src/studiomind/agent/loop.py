"""
The StudioMind agent loop.

Orchestrates the plan → act → verify → iterate cycle using Claude's tool use.
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

# Default model — Sonnet for speed + tool use quality, Opus for complex decisions
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
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


@dataclass
class AgentConfig:
    """Configuration for the agent loop."""

    model: str = DEFAULT_MODEL
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
    ) -> None:
        self._fl = fl
        self._config = config or AgentConfig()
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package is required for the agent loop. "
                "Install it with: pip install anthropic"
            )
        self._client = anthropic.Anthropic()
        self._executor = ToolExecutor(fl)
        self._action_log = ActionLog()

    @property
    def action_log(self) -> ActionLog:
        return self._action_log

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

        system = build_system_prompt()

        if continue_conversation and hasattr(self, "_conversation_history"):
            self._conversation_history.append({"role": "user", "content": user_goal})
            messages = self._conversation_history
        else:
            messages = [{"role": "user", "content": user_goal}]
            self._conversation_history = messages

        # Convert our tool schemas to Anthropic format
        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in TOOL_SCHEMAS
        ]

        final_text = ""

        for turn in range(self._config.max_turns):
            logger.info("Agent turn %d/%d", turn + 1, self._config.max_turns)

            response = self._api_call_with_retry(system, tools, messages)

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

    def _api_call_with_retry(self, system: str, tools: list, messages: list, max_retries: int = 5) -> Any:
        """Call the Anthropic API with exponential backoff on rate limits."""
        import anthropic

        for attempt in range(max_retries):
            try:
                return self._client.messages.create(
                    model=self._config.model,
                    max_tokens=4096,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except anthropic.RateLimitError as e:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt * 5  # 5s, 10s, 20s, 40s, 80s
                logger.warning("Rate limited. Waiting %ds before retry %d/%d...", wait, attempt + 1, max_retries)
                if self._config.on_message:
                    self._config.on_message(f"[Rate limited — waiting {wait}s before retry...]")
                time.sleep(wait)

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
