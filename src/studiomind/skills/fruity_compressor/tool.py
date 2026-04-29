"""set_compressor tool spec + executor binding for the Fruity Compressor skill.

Schema ranges reflect the live-FL calibration from 2026-04-29 (six axes,
all linear) — these supersede the pre-calibration approximations that
were in agent/tools.py during the hand-written era."""

from __future__ import annotations

from typing import Any

from studiomind.skills.fruity_compressor import wrapper as fruity_compressor


TOOL: dict[str, Any] = {
    "name": "set_compressor",
    "description": (
        "Set Fruity Compressor parameters using human-readable units (dB, "
        "ratio, ms). PREFERRED for any Fruity Compressor adjustment over "
        "the generic set_plugin_param.\n\n"
        "Pass only the parameters you want to change — anything omitted is "
        "left at its current FL value. Use read_mixer_track first to find "
        "the slot the compressor is loaded in.\n\n"
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
                "description": "FX slot where Fruity Compressor is loaded (0-9)",
            },
            "threshold_db": {
                "type": "number",
                "minimum": -60,
                "maximum": 0,
                "description": "Threshold in dB (-60 to 0). Lower = more compression.",
            },
            "ratio": {
                "type": "number",
                "minimum": 1,
                "maximum": 30,
                "description": "Compression ratio (1=no comp, 4=4:1, 20+=≈limiting). Calibrated max 30:1.",
            },
            "gain_db": {
                "type": "number",
                "minimum": -30,
                "maximum": 30,
                "description": "Makeup gain in dB (-30 to +30, 0=unity).",
            },
            "attack_ms": {
                "type": "number",
                "minimum": 0,
                "maximum": 400,
                "description": "Attack time in ms (0-400, linear).",
            },
            "release_ms": {
                "type": "number",
                "minimum": 0,
                "maximum": 4000,
                "description": "Release time in ms (0-4000, linear).",
            },
            "knee": {
                "type": "string",
                "enum": ["hard", "smooth"],
                "description": "Knee shape (hard = punchy, smooth = transparent).",
            },
        },
        "required": ["track_id", "slot"],
    },
}


def build_commands_from_args(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the agent's tool-call arguments into a list of
    set_plugin_param SysEx commands. Pure function — no I/O."""
    return fruity_compressor.build_compressor_commands(
        track_id=args["track_id"],
        slot=args["slot"],
        threshold_db=args.get("threshold_db"),
        ratio=args.get("ratio"),
        gain_db=args.get("gain_db"),
        attack_ms=args.get("attack_ms"),
        release_ms=args.get("release_ms"),
        knee=args.get("knee"),
    )
