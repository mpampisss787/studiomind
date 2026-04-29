"""set_compressor tool spec + executor binding for the Fruity Compressor skill.

Schema ranges reflect the live-FL calibration from 2026-04-29 (six axes,
all linear) — these supersede the pre-calibration approximations that
were in agent/tools.py during the hand-written era.

The skill exposes four entry points consumed by the mixing agent's
registry-driven dispatch (see ``studiomind.agent.tools.ToolExecutor``):

  * ``TOOL`` — JSON schema sent to Claude.
  * ``build_commands_from_args(args)`` — pure args-to-SysEx-commands
    helper, useful in tests.
  * ``execute(fl, args)`` — full dispatch: builds commands, drives them
    through the FL bridge, verifies each write via the device script's
    new_value readback, returns the per-param result dict the mixing
    agent sees.
  * ``description_from_args(args)`` — one-line human description used
    by the centralized decision logger.
"""

from __future__ import annotations

from typing import Any

from studiomind.skills.fruity_compressor import wrapper as fruity_compressor


# Tolerance for the FL-readback equality check. Plugins quantize parameter
# values internally, so the round-trip can drift by a tiny amount even on
# a successful write. Anything beyond this is a real rejection, not
# quantization noise.
WRITE_TOLERANCE = 1e-3


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


def execute(fl: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive every command through the FL bridge and verify each write
    via the device script's ``new_value`` readback. Returns a result
    dict with per-param status: anything where new_value drifts beyond
    WRITE_TOLERANCE is reported as ``took=False`` so the agent can
    surface a "FL didn't accept the write" diagnostic instead of
    claiming success."""
    commands = build_commands_from_args(args)

    param_labels = {
        fruity_compressor.PARAM_THRESHOLD: "threshold",
        fruity_compressor.PARAM_RATIO: "ratio",
        fruity_compressor.PARAM_GAIN: "gain",
        fruity_compressor.PARAM_ATTACK: "attack",
        fruity_compressor.PARAM_RELEASE: "release",
        fruity_compressor.PARAM_TYPE: "knee",
    }

    per_param: list[dict[str, Any]] = []
    for cmd in commands:
        requested = cmd["value"]
        result = fl.set_plugin_param(
            track_id=cmd["track_id"],
            slot=cmd["slot"],
            param_id=cmd["param_id"],
            value=requested,
        )
        # Device script returns {"ok": ..., "param_id": ..., "new_value": ..., "display": ...}
        # — new_value is plugins.getParamValue() called immediately after the write.
        # If new_value == requested (within tolerance), the write took. If not, FL
        # silently rejected it (or the wrapper's curve clamped to a no-op).
        new_value = result.get("new_value") if isinstance(result, dict) else None
        label = param_labels.get(cmd["param_id"], f"param_{cmd['param_id']}")
        took = (
            isinstance(new_value, (int, float))
            and abs(float(new_value) - float(requested)) <= WRITE_TOLERANCE
        )
        per_param.append({
            "param": label,
            "param_id": cmd["param_id"],
            "requested_value": requested,
            "new_value": new_value,
            "display": result.get("display") if isinstance(result, dict) else None,
            "took": took,
        })

    succeeded = [p for p in per_param if p["took"]]
    failed = [p for p in per_param if not p["took"]]

    return {
        # ok = True only when every requested param actually moved in FL.
        # The agent prompt should treat ok=False as a hard signal that the
        # write didn't take and stop the user before claiming success.
        "ok": len(failed) == 0,
        "params_attempted": len(commands),
        "params_accepted": len(succeeded),
        "params_rejected": len(failed),
        "per_param": per_param,
        "threshold_db": args.get("threshold_db"),
        "ratio": args.get("ratio"),
        "gain_db": args.get("gain_db"),
        "attack_ms": args.get("attack_ms"),
        "release_ms": args.get("release_ms"),
        "knee": args.get("knee"),
    }


def description_from_args(args: dict[str, Any]) -> str:
    """Compact, one-line description for the decision log."""
    bits = []
    if args.get("threshold_db") is not None:
        bits.append(f"thresh {args['threshold_db']:+.1f}dB")
    if args.get("ratio") is not None:
        bits.append(f"ratio {args['ratio']:.1f}:1")
    if args.get("attack_ms") is not None:
        bits.append(f"attack {args['attack_ms']:.1f}ms")
    if args.get("release_ms") is not None:
        bits.append(f"release {args['release_ms']:.0f}ms")
    if args.get("gain_db") is not None:
        bits.append(f"gain {args['gain_db']:+.1f}dB")
    if args.get("knee") is not None:
        bits.append(f"knee {args['knee']}")
    bits_str = ", ".join(bits) if bits else "no changes"
    return (
        f"Fruity Compressor on track {args['track_id']} slot "
        f"{args['slot']}: {bits_str}"
    )
