"""
Tool definitions for the StudioMind agent.

Each tool has:
- A JSON schema (sent to Claude as tool definitions)
- An executor function (bridges the tool call to the FL Studio command)
"""

from __future__ import annotations

from typing import Any

from studiomind.bridge.commands import FLStudio

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
]

# ═══════════════════════════════════════════════════════════════════
# TOOL EXECUTORS
# ═══════════════════════════════════════════════════════════════════

# Tools that modify FL Studio state — require snapshot first
DESTRUCTIVE_TOOLS = {
    "set_builtin_eq",
    "set_plugin_param",
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
}


class ToolExecutor:
    """Executes tool calls by dispatching to FLStudio commands or local analysis."""

    def __init__(self, fl: FLStudio) -> None:
        self._fl = fl

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

    def _exec_set_builtin_eq(self, params: dict) -> Any:
        return self._fl.set_eq(
            track_id=params["track_id"],
            band=params["band"],
            gain=params.get("gain"),
            frequency=params.get("frequency"),
            bandwidth=params.get("bandwidth"),
        )

    def _exec_set_plugin_param(self, params: dict) -> Any:
        return self._fl.set_plugin_param(
            track_id=params["track_id"],
            slot=params["slot"],
            param_id=params["param_id"],
            value=params["value"],
        )

    def _exec_set_mixer_volume(self, params: dict) -> Any:
        return self._fl.set_mixer_volume(params["track_id"], params["value"])

    def _exec_set_mixer_pan(self, params: dict) -> Any:
        return self._fl.set_mixer_pan(params["track_id"], params["value"])

    def _exec_snapshot(self, params: dict) -> Any:
        return self._fl.snapshot(label=params.get("label", "agent action"))

    def _exec_revert(self, params: dict) -> Any:
        return self._fl.revert()

    def _exec_analyze_audio(self, params: dict) -> Any:
        from studiomind.analyzer.spectral import analyze_audio

        result = analyze_audio(params["path"])
        return result.to_dict()
