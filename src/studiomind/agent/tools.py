"""
Tool definitions for the StudioMind agent.

Each tool has:
- A JSON schema (sent to Claude as tool definitions)
- An executor function (bridges the tool call to the FL Studio command)
"""

from __future__ import annotations

import threading
from typing import Any

from studiomind.bridge.commands import FLStudio
from studiomind.workspace import WorkspaceSession

# ═══════════════════════════════════════════════════════════════════
# TOOL SCHEMAS (sent to Claude API)
# ═══════════════════════════════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "name": "read_project_state",
        "description": (
            "Read the full FL Studio project state. Returns BPM, channels (name, type, volume, "
            "pan, mute, solo, mixer routing), mixer tracks (name, volume, pan, mute, solo, EQ state, "
            "loaded plugins), and patterns (name, length). Call this first to understand the project "
            "before making any changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "read_mixer_track",
        "description": (
            "Read detailed info for a single mixer track, including all plugin parameters with "
            "names and current values, routing destinations, and EQ state. Use this when you need "
            "to inspect a specific track's plugin chain in detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index (0=master, 1-125=inserts)",
                },
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "read_channel",
        "description": (
            "Read detailed info for a single channel rack channel, including its instrument "
            "plugin and parameters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "integer",
                    "description": "Channel index (global, 0-based)",
                },
            },
            "required": ["channel_id"],
        },
    },
    {
        "name": "set_builtin_eq",
        "description": (
            "Set the built-in 3-band parametric EQ on a mixer track. Every mixer track has this "
            "EQ always available — no need to add any plugin. Band 0 is low, band 1 is mid, "
            "band 2 is high. All values are normalized 0.0-1.0. For gain, 0.5 is unity (0 dB), "
            "below 0.5 is cut, above 0.5 is boost. Always call snapshot() before making EQ changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index (0=master)",
                },
                "band": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": "EQ band: 0=low, 1=mid, 2=high",
                },
                "gain": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Gain (0.0-1.0 normalized, 0.5=unity/0dB, <0.5=cut, >0.5=boost)",
                },
                "frequency": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Center frequency (0.0-1.0 normalized, low to high)",
                },
                "bandwidth": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Bandwidth/Q (0.0=narrow, 1.0=wide)",
                },
            },
            "required": ["track_id", "band"],
        },
    },
    {
        "name": "set_plugin_param",
        "description": (
            "Set a parameter on any plugin loaded in a mixer insert slot. Use read_mixer_track() "
            "first to discover available plugins and their parameter IDs/names/values. "
            "Always call snapshot() before making changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index",
                },
                "slot": {
                    "type": "integer",
                    "description": "FX slot index (0-9)",
                },
                "param_id": {
                    "type": "integer",
                    "description": "Parameter index (from read_mixer_track results)",
                },
                "value": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Parameter value (0.0-1.0 normalized)",
                },
            },
            "required": ["track_id", "slot", "param_id", "value"],
        },
    },
    {
        "name": "set_mixer_volume",
        "description": (
            "Set a mixer track's volume level. 0.0 is silent, ~0.8 is 0dB (unity), 1.0 is max. "
            "Always call snapshot() before making changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer", "description": "Mixer track index"},
                "value": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Volume level (0.0-1.0, ~0.8 = 0dB)",
                },
            },
            "required": ["track_id", "value"],
        },
    },
    {
        "name": "set_mixer_pan",
        "description": (
            "Set a mixer track's stereo pan position. 0.0 is hard left, 0.5 is center, "
            "1.0 is hard right. Always call snapshot() before making changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "integer", "description": "Mixer track index"},
                "value": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Pan position (0.0=left, 0.5=center, 1.0=right)",
                },
            },
            "required": ["track_id", "value"],
        },
    },
    {
        "name": "snapshot",
        "description": (
            "Save FL Studio's undo state before making any destructive changes. ALWAYS call this "
            "before set_builtin_eq, set_plugin_param, set_mixer_volume, or set_mixer_pan. "
            "This creates a restore point the user can revert to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Short description of what you're about to change",
                },
            },
            "required": ["label"],
        },
    },
    {
        "name": "revert",
        "description": (
            "Undo the last change made to the FL Studio project. Use this if the user is "
            "unhappy with a change, or if analysis shows the change made things worse."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "analyze_audio",
        "description": (
            "Analyze a rendered audio file (WAV). Returns spectral balance across 7 frequency "
            "bands (sub, low, low_mid, mid, high_mid, presence, air), LUFS loudness, true peak, "
            "RMS, and spectral centroid. Use this after rendering to verify changes or diagnose issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the WAV file to analyze",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_workspace_status",
        "description": (
            "Return the current StudioMind project workspace: active project name, "
            "all known stems and masters with their status (pending/ready/stale), "
            "analysis data if available, and any reference tracks dropped into references/. "
            "ALWAYS call this at the start of a session before asking for new renders — "
            "existing fresh renders can be reused instead of re-rendered."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "prepare_batch_render",
        "description": (
            "PREFERRED for mixing work. Queues a batch render of every mixer track + "
            "optionally the master in a SINGLE FL export. The user triggers ONE Ctrl+R "
            "in 'Tracks (separate audio files)' mode and all stems land at once. "
            "Use this when you need a complete picture of the mix to make decisions. "
            "Returns an instruction string to speak to the user before calling wait_for_renders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_master": {
                    "type": "boolean",
                    "description": "Also queue a master render (default true)",
                },
            },
        },
    },
    {
        "name": "prepare_stem_render",
        "description": (
            "Queue a render for a SINGLE mixer track. Solos the track via MIDI and tells "
            "the user which filename to export. Use this only for targeted re-checks after "
            "making a change to one track — for initial analysis, prefer prepare_batch_render. "
            "Returns an instruction string to speak to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index (0 is master; use 1+ for inserts)",
                },
            },
            "required": ["track_id"],
        },
    },
    {
        "name": "prepare_master_render",
        "description": (
            "Queue a master render only. Un-solos any soloed tracks. Use this to verify the "
            "full mix after changes. Returns an instruction string to speak to the user."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "collect_render",
        "description": (
            "BLOCKS until the requested render's file lands in the workspace, then analyzes it. "
            "Call this AFTER telling the user what to export. Identify the target by track_id "
            "(for a stem) or filename (for a master). Returns full audio analysis — LUFS, peak, "
            "spectral balance, etc. — and un-solos the track if it was a stem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index — use this for stems",
                },
                "filename": {
                    "type": "string",
                    "description": "Render filename — use this for masters or by-name lookup",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Max seconds to wait (default 180)",
                },
            },
        },
    },
    {
        "name": "collect_all_renders",
        "description": (
            "Pairs with prepare_batch_render. BLOCKS until every pending render in the "
            "workspace has landed and been analyzed, then returns the full set of analyses "
            "at once. Use this after telling the user to do the batch export."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeout_s": {
                    "type": "number",
                    "description": "Max seconds to wait for the batch to complete (default 300)",
                },
            },
        },
    },
    {
        "name": "read_project_history",
        "description": (
            "Return the agent-maintained history.md for this project (last ~20 entries) "
            "AND the user-authored notes.md if it exists. Call this ONCE at session start "
            "so you remember what was done in prior sessions and any project-specific "
            "instructions the user wrote. The history is cumulative across sessions."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "write_history_entry",
        "description": (
            "Append a short markdown entry to this project's history.md. Use this after "
            "completing a meaningful change or at the end of a reasoning thread. Keep "
            "entries concise — a few bullet lines or a short paragraph. Include concrete "
            "numbers (LUFS, dB values, Hz) so future sessions can pick up cleanly.\n\n"
            "Good entry: 'Cut 2.5dB at 320 Hz on track 3 (Bass) via Pro-Q 3 band 4. "
            "Master LUFS -9.4 -> -9.7. User kept. Low-mid muddiness reduced.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "Markdown content to append. No heading needed — the tool adds a timestamp header.",
                },
            },
            "required": ["entry"],
        },
    },
    {
        "name": "append_to_project_notes",
        "description": (
            "Append a durable insight to the project's notes.md. Use this for facts "
            "that should apply to ALL future sessions of this project, not per-session "
            "events (those go in write_history_entry).\n\n"
            "Good candidates:\n"
            "- User-stated preferences that persist ('never boost above 10kHz')\n"
            "- Project-specific constraints ('master target -7 LUFS')\n"
            "- Recurring observations ('track 9 guitar has hot 2.5kHz resonance — check every session')\n"
            "- Sonic decisions that should stick ('bass sits 40-120 Hz, sub-bass below is intentional')\n"
            "- References/targets ('matches Travis Scott Goosebumps tonal balance')\n\n"
            "NOT for:\n"
            "- One-off changes ('cut 2dB at 320Hz on bass today') — those go in write_history_entry\n"
            "- Transient state (current staleness, pending renders) — that's in session.json\n\n"
            "Append-only — the user can prune by hand later. Be terse and opinionated. One "
            "or two bullet points is typical. Use markdown — a '## Observations' heading is "
            "appropriate on the first entry, not on every entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "Markdown content to append to notes.md. The tool preserves everything already in the file.",
                },
            },
            "required": ["entry"],
        },
    },
    {
        "name": "prune_project_history",
        "description": (
            "Compact history.md when it has grown large (typically >30 entries). "
            "Replaces everything except the last 30 entries with a brief archive "
            "summary that you write. Call this proactively when read_project_history "
            "reports a large entry count — it keeps the file readable and prevents "
            "future sessions from wading through hundreds of old entries.\n\n"
            "Write the summary to cover the key decisions and measurements from the "
            "archived portion, so nothing important is truly lost."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Compact markdown summary of the entries being archived.",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "read_user_preferences",
        "description": (
            "Return the global (cross-project) user preferences — durable facts about "
            "the user's taste and working style that apply to every project. "
            "Examples: 'never boost above 10kHz', 'prefers -1dBTP master ceiling', "
            "'always soft-knee compression on vocals'. These are distinct from "
            "per-project notes.md (project-scoped constraints) and decisions.json "
            "(per-action history). Call this at session start so your choices "
            "respect preferences the user has established in prior work. Returned "
            "sorted strongest-first."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_user_preference",
        "description": (
            "Record or reinforce a global user preference. Write one when the user "
            "states a clear rule ('I never want air shelf boosts on vocals') or "
            "when you observe a strong pattern across multiple sessions (user has "
            "reverted 3/3 of your +3dB boosts above 8kHz → record 'avoid boosts "
            "above 8kHz'). Similar existing statements are merged, so feel free "
            "to call this when in doubt — you won't duplicate. Be specific and "
            "quote numbers. 'User prefers cuts over boosts in 2-4kHz range.' is "
            "good. 'User likes clean mixes' is useless."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "statement": {
                    "type": "string",
                    "description": "One-sentence preference, specific and actionable.",
                },
                "source": {
                    "type": "string",
                    "enum": ["user_explicit", "derived", "agent_observation"],
                    "description": (
                        "user_explicit when the user stated it directly; derived when "
                        "you computed it from decisions.json patterns; "
                        "agent_observation for single-exchange inferences."
                    ),
                },
            },
            "required": ["statement"],
        },
    },
    {
        "name": "read_recent_decisions",
        "description": (
            "Return the recent decision log for this project — every destructive "
            "action StudioMind has made, with its params and outcome "
            "(pending / kept / reverted). Use this at session start (alongside "
            "read_project_history and detect_external_changes) to spot patterns: "
            "if the user has reverted 3 of your last 4 high-shelf boosts, be more "
            "conservative. Also useful when the user says 'what did you do last "
            "time?' — the decisions log has the exact params. Returns the last N "
            "decisions plus an outcome summary across the whole log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max decisions to return from the tail (default 20).",
                },
            },
        },
    },
    {
        "name": "detect_external_changes",
        "description": (
            "Compare the current FL project state to the snapshots StudioMind took at each "
            "stem's last render. Reports which mixer tracks were edited in FL WITHOUT "
            "StudioMind (between sessions, or while StudioMind wasn't running). Call this "
            "at session start alongside read_project_history so you know what the user "
            "changed on their own since you last worked together."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_staleness",
        "description": (
            "Re-check every rendered stem against the current FL mixer-track state. Any stem "
            "whose track has changed since the render (EQ, plugin, volume, etc.) gets flagged "
            "'stale'. Call this after making destructive changes so you know which stems need "
            "re-rendering before you trust their old analysis."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_proq3",
        "description": (
            "Set FabFilter Pro-Q 3 EQ bands using human-readable values (Hz, dB, Q). "
            "This is the PREFERRED tool for EQ adjustments when Pro-Q 3 is loaded on a track. "
            "It handles all parameter conversions automatically.\n\n"
            "Pro-Q 3 has 10 bands. Each band can be: bell, low_shelf, low_cut, high_shelf, "
            "high_cut, notch, band_pass, or tilt_shelf.\n\n"
            "ALWAYS call snapshot() before using this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_id": {
                    "type": "integer",
                    "description": "Mixer track index",
                },
                "slot": {
                    "type": "integer",
                    "description": "FX slot where Pro-Q 3 is loaded (0-9)",
                },
                "band": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Pro-Q 3 band number (1-10)",
                },
                "frequency_hz": {
                    "type": "number",
                    "minimum": 10,
                    "maximum": 30000,
                    "description": "Center frequency in Hz (10-30000)",
                },
                "gain_db": {
                    "type": "number",
                    "minimum": -30,
                    "maximum": 30,
                    "description": "Gain in dB (-30 to +30, 0=unity)",
                },
                "q": {
                    "type": "number",
                    "minimum": 0.025,
                    "maximum": 40,
                    "description": "Q factor / bandwidth (0.025=very wide, 40=very narrow, 1.0=default)",
                },
                "shape": {
                    "type": "string",
                    "enum": ["bell", "low_shelf", "low_cut", "high_shelf", "high_cut", "notch", "band_pass", "tilt_shelf"],
                    "description": "Filter shape (default: bell)",
                },
                "slope_db_oct": {
                    "type": "integer",
                    "enum": [6, 12, 18, 24, 36, 48, 72, 96],
                    "description": "Filter slope in dB/oct (for cut/shelf shapes, default: 12)",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the band is active (default: true)",
                },
            },
            "required": ["track_id", "slot", "band"],
        },
    },
]

# ═══════════════════════════════════════════════════════════════════
# TOOL EXECUTORS
# ═══════════════════════════════════════════════════════════════════

# Tools that modify FL Studio state — require snapshot first
DESTRUCTIVE_TOOLS = {
    "set_builtin_eq",
    "set_plugin_param",
    "set_proq3",
    "set_mixer_volume",
    "set_mixer_pan",
}

# Tools that only read state — safe to execute without confirmation
READ_ONLY_TOOLS = {
    "read_project_state",
    "read_mixer_track",
    "read_channel",
    "analyze_audio",
    "snapshot",
    "revert",
    "get_workspace_status",
    "prepare_batch_render",
    "prepare_stem_render",
    "prepare_master_render",
    "collect_render",
    "collect_all_renders",
    "refresh_staleness",
    "read_project_history",
    "write_history_entry",
    "append_to_project_notes",
    "prune_project_history",
    "detect_external_changes",
}


class ToolExecutor:
    """Executes tool calls by dispatching to FLStudio commands or local analysis."""

    def __init__(
        self,
        fl: FLStudio,
        workspace: WorkspaceSession | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._fl = fl
        self._workspace = workspace
        self._stop_event = stop_event or threading.Event()

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        """Execute a tool call and return the result."""
        handler = getattr(self, f"_exec_{tool_name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return handler(tool_input)

    def _exec_read_project_state(self, params: dict) -> Any:
        return self._fl.read_project_state()

    def _exec_read_mixer_track(self, params: dict) -> Any:
        return self._fl.read_mixer_track(params["track_id"])

    def _exec_read_channel(self, params: dict) -> Any:
        return self._fl.read_channel(params["channel_id"])

    def _log_decision(
        self,
        tool: str,
        params: dict,
        description: str,
    ) -> None:
        """Append a decision record to the project's decisions.json. No-op if
        no workspace is active (bare-agent mode, tests)."""
        if self._workspace is None:
            return
        try:
            project = self._workspace.project
            log = project.load_decisions()
            track_id = params.get("track_id")
            track_name = None
            if track_id is not None:
                try:
                    track_name = (self._fl.read_mixer_track(track_id) or {}).get("name")
                except Exception:
                    pass
            log.append(
                tool=tool,
                params={k: v for k, v in params.items() if v is not None},
                description=description,
                track_id=track_id,
                track_name=track_name,
                expected_delta=params.get("expected_delta"),
            )
        except Exception as e:
            # Decision logging is never allowed to break a tool call.
            logger.warning("Failed to log decision for %s: %s", tool, e)

    def _exec_set_builtin_eq(self, params: dict) -> Any:
        result = self._fl.set_eq(
            track_id=params["track_id"],
            band=params["band"],
            gain=params.get("gain"),
            frequency=params.get("frequency"),
            bandwidth=params.get("bandwidth"),
        )
        band_label = {0: "low", 1: "mid", 2: "high"}.get(params["band"], f"band {params['band']}")
        self._log_decision(
            tool="set_builtin_eq",
            params=params,
            description=f"built-in EQ {band_label} on track {params['track_id']}",
        )
        return result

    def _exec_set_plugin_param(self, params: dict) -> Any:
        result = self._fl.set_plugin_param(
            track_id=params["track_id"],
            slot=params["slot"],
            param_id=params["param_id"],
            value=params["value"],
        )
        self._log_decision(
            tool="set_plugin_param",
            params=params,
            description=f"plugin slot {params['slot']} param {params['param_id']} on track {params['track_id']}",
        )
        return result

    def _exec_set_mixer_volume(self, params: dict) -> Any:
        result = self._fl.set_mixer_volume(params["track_id"], params["value"])
        self._log_decision(
            tool="set_mixer_volume",
            params=params,
            description=f"mixer volume on track {params['track_id']} → {params['value']}",
        )
        return result

    def _exec_set_mixer_pan(self, params: dict) -> Any:
        result = self._fl.set_mixer_pan(params["track_id"], params["value"])
        self._log_decision(
            tool="set_mixer_pan",
            params=params,
            description=f"mixer pan on track {params['track_id']} → {params['value']}",
        )
        return result

    def _exec_snapshot(self, params: dict) -> Any:
        return self._fl.snapshot(label=params.get("label", "agent action"))

    def _exec_revert(self, params: dict) -> Any:
        result = self._fl.revert()
        if self._workspace is not None:
            try:
                log = self._workspace.project.load_decisions()
                reverted = log.mark_last_reverted()
                if reverted is not None:
                    logger.info("Marked decision %s as reverted", reverted.id)
            except Exception as e:
                logger.warning("Failed to mark decision reverted: %s", e)
        return result

    def _exec_analyze_audio(self, params: dict) -> Any:
        from studiomind.analyzer.spectral import analyze_audio

        result = analyze_audio(params["path"])
        return result.to_dict()

    def _exec_set_proq3(self, params: dict) -> Any:
        from studiomind.plugins.fabfilter_proq3 import build_eq_commands, param_to_freq, param_to_gain, param_to_q

        commands = build_eq_commands(
            track_id=params["track_id"],
            slot=params["slot"],
            band=params["band"],
            frequency_hz=params.get("frequency_hz"),
            gain_db=params.get("gain_db"),
            q=params.get("q"),
            shape=params.get("shape"),
            slope_db_oct=params.get("slope_db_oct"),
            enabled=params.get("enabled", True),
        )

        results = []
        for cmd in commands:
            result = self._fl.set_plugin_param(
                track_id=cmd["track_id"],
                slot=cmd["slot"],
                param_id=cmd["param_id"],
                value=cmd["value"],
            )
            results.append(result)

        self._log_decision(
            tool="set_proq3",
            params=params,
            description=(
                f"Pro-Q 3 band {params['band']} on track {params['track_id']} "
                f"({params.get('shape', 'bell')} {params.get('frequency_hz', '?')}Hz "
                f"{params.get('gain_db', 0)}dB Q={params.get('q', '?')})"
            ),
        )
        return {
            "ok": True,
            "band": params["band"],
            "params_set": len(commands),
            "frequency_hz": params.get("frequency_hz"),
            "gain_db": params.get("gain_db"),
            "q": params.get("q"),
            "shape": params.get("shape"),
        }

    def _require_workspace(self) -> WorkspaceSession:
        if self._workspace is None:
            raise RuntimeError(
                "No active workspace. This session was started without a project — "
                "run `studiomind project` first, or the agent shell did not initialize one."
            )
        return self._workspace

    def _exec_get_workspace_status(self, params: dict) -> Any:
        return self._require_workspace().status()

    def _exec_prepare_batch_render(self, params: dict) -> Any:
        include_master = params.get("include_master", True)
        return self._require_workspace().prepare_batch_render(
            include_master=include_master,
            stop_event=self._stop_event,
        )

    def _exec_prepare_stem_render(self, params: dict) -> Any:
        return self._require_workspace().prepare_stem(
            track_id=params["track_id"],
            stop_event=self._stop_event,
        )

    def _exec_prepare_master_render(self, params: dict) -> Any:
        return self._require_workspace().prepare_master(stop_event=self._stop_event)

    def _exec_collect_render(self, params: dict) -> Any:
        return self._require_workspace().collect(
            track_id=params.get("track_id"),
            filename=params.get("filename"),
            timeout_s=params.get("timeout_s"),
            stop_event=self._stop_event,
        )

    # First auto-render of a session frequently produces unreadable WAVs for a
    # handful of tracks (FL still holding file handles / mid-overwrite). A single
    # retry 10s later almost always succeeds. Retrying more aggressively just
    # thrashes; retrying more than once rarely helps.
    _RETRY_DELAY_S = 10.0

    def _exec_collect_all_renders(self, params: dict) -> Any:
        """Wait for every pending render in the workspace, analyze them all, return as a list."""
        import time as _time

        workspace = self._require_workspace()
        timeout = params.get("timeout_s", 300.0)
        deadline = _time.monotonic() + timeout

        results: list[dict] = []
        failures: list[dict] = []
        collected_ids: set[tuple[str, object]] = set()
        # key -> {"retry_at": float, "kind": "stem"|"master", "data": dict, "first_error": str}
        pending_retries: dict[tuple[str, object], dict] = {}

        def _try_collect(kind: str, key_value: object, call_kwargs: dict, data: dict) -> None:
            """Attempt one collect; on failure, schedule a retry or finalize as failure."""
            key = (kind, key_value)
            try:
                r = workspace.collect(
                    timeout_s=5.0,
                    stop_event=self._stop_event,
                    **call_kwargs,
                )
                results.append(r)
                collected_ids.add(key)
                pending_retries.pop(key, None)
            except Exception as e:
                if key in pending_retries:
                    # Second failure — give up on this one.
                    failures.append({
                        **({"track_id": key_value} if kind == "stem" else {"filename": key_value}),
                        "filename": data.get("filename") if kind == "stem" else key_value,
                        "error": str(e),
                        "first_error": pending_retries[key]["first_error"],
                        "attempts": 2,
                    })
                    collected_ids.add(key)
                    pending_retries.pop(key, None)
                else:
                    pending_retries[key] = {
                        "retry_at": _time.monotonic() + self._RETRY_DELAY_S,
                        "kind": kind,
                        "data": data,
                        "first_error": str(e),
                    }

        while _time.monotonic() < deadline:
            if self._stop_event.is_set():
                # Anything still in pending_retries becomes a failure on stop.
                for key, info in pending_retries.items():
                    kind, key_value = key
                    failures.append({
                        **({"track_id": key_value} if kind == "stem" else {"filename": key_value}),
                        "error": info["first_error"],
                        "attempts": 1,
                        "note": "pending retry at stop",
                    })
                return {
                    "ok": False,
                    "error": "stopped",
                    "reason": "User cancelled the wait.",
                    "collected": results,
                    "failures": failures,
                    "failed_count": len(failures),
                }

            status = workspace.status()
            pending_stems = [s for s in status["stems"] if s["status"] == "pending"]
            pending_masters = [m for m in status["masters"] if m["status"] == "pending"]
            ready_stems = [
                s for s in status["stems"]
                if s["status"] == "ready" and ("stem", s["track_id"]) not in collected_ids
            ]
            ready_masters = [
                m for m in status["masters"]
                if m["status"] == "ready" and ("master", m["filename"]) not in collected_ids
            ]

            # Retries whose cooldown has elapsed. Iterate over a snapshot because
            # _try_collect mutates pending_retries.
            now = _time.monotonic()
            retry_now = [
                (key, info) for key, info in list(pending_retries.items())
                if now >= info["retry_at"]
            ]
            for key, info in retry_now:
                kind, key_value = key
                if kind == "stem":
                    _try_collect("stem", key_value, {"track_id": key_value}, info["data"])
                else:
                    _try_collect("master", key_value, {"filename": key_value}, info["data"])

            for s in ready_stems:
                _try_collect("stem", s["track_id"], {"track_id": s["track_id"]}, s)

            for m in ready_masters:
                _try_collect("master", m["filename"], {"filename": m["filename"]}, m)

            if not pending_stems and not pending_masters and not pending_retries:
                break
            _time.sleep(0.5)
        else:
            # Timeout: pending retries become failures.
            for key, info in pending_retries.items():
                kind, key_value = key
                failures.append({
                    **({"track_id": key_value} if kind == "stem" else {"filename": key_value}),
                    "error": info["first_error"],
                    "attempts": 1,
                    "note": "pending retry at timeout",
                })
            return {
                "ok": False,
                "error": "timeout",
                "collected": results,
                "failures": failures,
                "failed_count": len(failures),
                "still_pending": {
                    "stems": [s for s in workspace.status()["stems"] if s["status"] == "pending"],
                    "masters": [m for m in workspace.status()["masters"] if m["status"] == "pending"],
                },
            }

        return {
            "ok": True,
            "count": len(results),
            "results": results,
            "failures": failures,
            "failed_count": len(failures),
        }

    def _exec_refresh_staleness(self, params: dict) -> Any:
        newly_stale = self._require_workspace().refresh_staleness()
        return {"ok": True, "newly_stale_track_ids": newly_stale}

    def _exec_read_project_history(self, params: dict) -> Any:
        ws = self._require_workspace()
        history = ws.project.read_history()
        notes = ws.project.read_notes()
        entry_count = ws.project.history_entry_count()
        return {
            "ok": True,
            "project_name": ws.project.name,
            "history_markdown": history or "(no history yet — this is the first session)",
            "notes_markdown": notes or "(no user notes.md)",
            "has_history": bool(history),
            "has_notes": bool(notes),
            "total_history_entries": entry_count,
            "prune_suggested": entry_count > ws.project.HISTORY_PRUNE_KEEP,
        }

    def _exec_prune_project_history(self, params: dict) -> Any:
        ws = self._require_workspace()
        summary = params.get("summary", "").strip()
        if not summary:
            return {"ok": False, "error": "summary is required"}
        before = ws.project.history_entry_count()
        ws.project.prune_history(summary)
        after = ws.project.history_entry_count()
        return {
            "ok": True,
            "entries_before": before,
            "entries_after": after,
            "path": str(ws.project.history_path),
        }

    def _exec_write_history_entry(self, params: dict) -> Any:
        ws = self._require_workspace()
        entry = params.get("entry", "").strip()
        if not entry:
            return {"ok": False, "error": "entry is required"}
        header = ws.project.append_history_entry(entry)
        return {"ok": True, "timestamp": header, "path": str(ws.project.history_path)}

    def _exec_append_to_project_notes(self, params: dict) -> Any:
        ws = self._require_workspace()
        entry = params.get("entry", "").strip()
        if not entry:
            return {"ok": False, "error": "entry is required"}
        ws.project.append_notes_entry(entry)
        return {"ok": True, "path": str(ws.project.notes_path)}

    def _exec_detect_external_changes(self, params: dict) -> Any:
        return self._require_workspace().detect_external_changes()

    def _exec_read_recent_decisions(self, params: dict) -> Any:
        ws = self._require_workspace()
        log = ws.project.load_decisions()
        # Age out stale pending decisions from prior sessions before returning,
        # so the agent sees realistic outcome counts.
        aged = log.age_pending()
        limit = int(params.get("limit", 20))
        decisions = [d.to_dict() for d in log.recent(limit)]
        return {
            "ok": True,
            "decisions": decisions,
            "outcome_counts": log.summary_counts(),
            "aged_to_kept": aged,
            "total_decisions": len(log.decisions),
        }

    def _exec_read_user_preferences(self, params: dict) -> Any:
        from studiomind.user_prefs import UserPreferences

        prefs = UserPreferences.load()
        return {
            "ok": True,
            "preferences": [p.to_dict() for p in prefs.sorted_for_agent()],
            "count": len(prefs.preferences),
            "path": str(prefs.path),
        }

    def _exec_record_user_preference(self, params: dict) -> Any:
        from studiomind.user_prefs import SOURCE_OBSERVATION, UserPreferences

        statement = (params.get("statement") or "").strip()
        if not statement:
            return {"ok": False, "error": "statement is required"}
        source = params.get("source", SOURCE_OBSERVATION)
        prefs = UserPreferences.load()
        pref = prefs.record(statement=statement, source=source)
        return {
            "ok": True,
            "preference": pref.to_dict(),
            "total_preferences": len(prefs.preferences),
        }
