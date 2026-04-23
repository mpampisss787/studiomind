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

# Compaction: when the estimated token count of the conversation history exceeds
# this threshold, summarise old turns with Haiku (cheap + fast) and replace them
# with a compact context block. Keeps cost and attention quality stable over long
# sessions without losing the important decisions.
COMPACTION_TOKEN_THRESHOLD = 12_000
COMPACTION_KEEP_RECENT_TURNS = 4   # always keep the last N turn-pairs verbatim
COMPACTION_MODEL = "claude-haiku-4-5-20251001"  # cheap summariser


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

    @property
    def last_text_response(self) -> str:
        """The most recent text the agent sent to the user. Empty if nothing yet."""
        return self._last_text_response

    def request_stop(self) -> None:
        """Signal the agent to stop at the next check point (between turns and inside blocking tools)."""
        self._stop_event.set()

    # ── Conversation compaction ──────────────────────────────────

    @staticmethod
    def _estimate_tokens(messages: list) -> int:
        """Rough token estimate: 1 token ≈ 4 chars of JSON."""
        try:
            return len(json.dumps(messages, default=str)) // 4
        except Exception:
            return 0

    def _compact_history(self, messages: list) -> list:
        """
        Summarise the older portion of the conversation using Haiku, then return
        a trimmed message list:
          [summarised-context-user-msg, ack-assistant-msg, ...last N turn-pairs verbatim]

        The summary preserves: measurements (LUFS, Hz, dB), changes applied and
        whether they were accepted/reverted, current project state, open issues.
        Cheap and fast — Haiku is used so compaction barely registers on the bill.
        """
        import anthropic

        # Separate history into "old" (to compact) and "recent" (to keep verbatim).
        # Messages come in pairs: user + assistant. A turn-pair = 2 messages.
        keep_count = COMPACTION_KEEP_RECENT_TURNS * 2
        if len(messages) <= keep_count + 2:
            return messages  # not enough to compact

        old_messages   = messages[:-keep_count]
        recent_messages = messages[-keep_count:]

        old_text = json.dumps(old_messages, default=str, ensure_ascii=False)

        compaction_prompt = (
            "You are summarising a StudioMind mixing session for context compaction.\n"
            "Produce a concise summary (≤400 words) that preserves:\n"
            "- Audio measurements: LUFS, dB values, spectral band readings, true peak\n"
            "- Changes applied: which track, what was changed, exact values\n"
            "- User decisions: which changes were kept vs reverted\n"
            "- Current project state: what's been fixed, what's still open\n"
            "- Any user preferences or constraints stated during the session\n"
            "Discard: verbose tool outputs, re-reads of unchanged state, conversational filler.\n\n"
            f"Session to summarise:\n{old_text}"
        )

        try:
            resp = self._client.messages.create(
                model=COMPACTION_MODEL,
                max_tokens=600,
                messages=[{"role": "user", "content": compaction_prompt}],
            )
            summary = resp.content[0].text.strip()
        except Exception as e:
            logger.warning("Compaction failed: %s — keeping full history", e)
            return messages

        # Persist the summary to history.md BEFORE discarding the old turns.
        # This is the safety net: if the connection drops right after compaction,
        # the next session reads history.md and recovers this context instead of
        # starting completely blind.
        try:
            workspace = getattr(self._executor, "_workspace", None)
            if workspace is not None:
                workspace.project.append_history_entry(
                    "[Auto-saved on context compaction]\n\n" + summary
                )
                logger.info("Compaction summary saved to history.md")
        except Exception as e:
            logger.debug("Could not save compaction to history.md: %s", e)

        compacted = [
            {
                "role": "user",
                "content": (
                    "[Context from earlier in this session — auto-compacted to save tokens]\n\n"
                    + summary
                ),
            },
            {
                "role": "assistant",
                "content": "Understood. I have the context from earlier. Continuing.",
            },
        ] + recent_messages

        logger.info(
            "History compacted: %d → %d messages (was ~%d tokens)",
            len(messages),
            len(compacted),
            self._estimate_tokens(old_messages),
        )
        return compacted

    def _maybe_compact(self, messages: list) -> list:
        """Compact the history if it exceeds the token threshold. Returns updated list."""
        if self._estimate_tokens(messages) < COMPACTION_TOKEN_THRESHOLD:
            return messages
        logger.info("History approaching token limit — compacting...")
        compacted = self._compact_history(messages)
        if compacted is not messages and self._config.on_message:
            self._config.on_message(
                "[Session history auto-compacted to keep responses fast and focused.]"
            )
        return compacted

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
        self._last_text_response: str = ""
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
                self._last_text_response = text
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

            # Compact history if it's grown too large
            messages = self._maybe_compact(messages)
            self._conversation_history = messages

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
