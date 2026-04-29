"""set_proq3 tool spec + executor binding for the FabFilter Pro-Q 3 skill.

The skill exposes four entry points consumed by the mixing agent's
registry-driven dispatch (see ``studiomind.agent.tools.ToolExecutor``):

  * ``TOOL`` — JSON schema sent to Claude.
  * ``build_commands_from_args(args)`` — pure args-to-SysEx-commands
    helper, useful in tests.
  * ``execute(fl, args)`` — full dispatch: builds commands, drives them
    through the FL bridge, returns the result dict the mixing agent
    sees as the tool's output.
  * ``description_from_args(args)`` — one-line human description used
    by the centralized decision logger.
"""

from __future__ import annotations

from typing import Any

from studiomind.skills.fabfilter_proq3 import wrapper as fabfilter_proq3


TOOL: dict[str, Any] = {
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
}


def build_commands_from_args(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the agent's tool-call arguments into a list of
    set_plugin_param SysEx commands. Pure function — no I/O — so the
    mixing agent's executor can dispatch them through whatever bridge
    it has."""
    return fabfilter_proq3.build_eq_commands(
        track_id=args["track_id"],
        slot=args["slot"],
        band=args["band"],
        frequency_hz=args.get("frequency_hz"),
        gain_db=args.get("gain_db"),
        q=args.get("q"),
        shape=args.get("shape"),
        slope_db_oct=args.get("slope_db_oct"),
        enabled=args.get("enabled", True),
    )


def execute(fl: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive every command through the FL bridge and return the result
    dict the mixing agent sees. Mirrors the shape the hand-written
    executor produced before P3-C, so callers and prompt fragments that
    expect ``params_set`` keep working."""
    commands = build_commands_from_args(args)
    for cmd in commands:
        fl.set_plugin_param(
            track_id=cmd["track_id"],
            slot=cmd["slot"],
            param_id=cmd["param_id"],
            value=cmd["value"],
        )
    return {
        "ok": True,
        "band": args["band"],
        "params_set": len(commands),
        "frequency_hz": args.get("frequency_hz"),
        "gain_db": args.get("gain_db"),
        "q": args.get("q"),
        "shape": args.get("shape"),
    }


def description_from_args(args: dict[str, Any]) -> str:
    """Compact, one-line description for the decision log."""
    return (
        f"Pro-Q 3 band {args['band']} on track {args['track_id']} "
        f"({args.get('shape', 'bell')} {args.get('frequency_hz', '?')}Hz "
        f"{args.get('gain_db', 0)}dB Q={args.get('q', '?')})"
    )
