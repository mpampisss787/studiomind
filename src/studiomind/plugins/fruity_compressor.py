"""
Fruity Compressor parameter mapping.

Fruity Compressor exposes 6 VST parameters, enumerated against a live FL
session and committed to ``fruity_compressor_params.json``:

  id 0 Threshold    default 1.0
  id 1 Ratio        default 0.02027
  id 2 Gain         default 0.5    (unity makeup)
  id 3 Attack       default 0.0375
  id 4 Release      default 0.049762
  id 5 Type         default 0.0    (Hard knee)

All values are normalized 0.0-1.0 in the VST interface.

CALIBRATION NOTE — the mappings below are best-effort approximations.
FL Studio does not publish the exact normalized → human-units curves for
its stock Fruity Compressor. The constants here were chosen so that:

  * ``threshold_to_param(0)`` rounds to 1.0 (the documented "no
    compression" default).
  * ``gain_to_param(0)`` is exactly 0.5 (the documented unity makeup).
  * ``ratio_to_param(1.0)`` is exactly 0.0 (1:1 = no ratio).
  * ``attack_to_param`` / ``release_to_param`` are logarithmic (matches
    most DAW comp UIs).
  * The defaults in ``fruity_compressor_params.json`` round-trip back to
    plausible "factory default" human values (threshold 0 dB, gain 0 dB,
    ratio ≈1.4:1, attack ≈1 ms, release ≈100 ms, hard knee).

If a vertical-slice test shows the agent's predicted dynamic delta
consistently off by more than ~1.5 dB on crest_factor, retune the
constants here against a measurement sweep — pick three points per axis
(min/mid/max), set them via ``set_plugin_param``, render, and read the
resulting compression curve out of the rendered audio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

PLUGIN_NAME = "Fruity Compressor"
NUM_PARAMS = 6

PARAM_THRESHOLD = 0
PARAM_RATIO = 1
PARAM_GAIN = 2
PARAM_ATTACK = 3
PARAM_RELEASE = 4
PARAM_TYPE = 5

# Threshold range (linear in dB)
THRESHOLD_MIN_DB = -60.0
THRESHOLD_MAX_DB = 0.0

# Ratio range (linear in ratio space — NOT compression-ratio-units, just
# normalized [1.0, RATIO_MAX] linearly interpolated against [0.0, 1.0])
RATIO_MIN = 1.0
RATIO_MAX = 20.0

# Gain range (linear in dB; 0 dB lands at param=0.5)
GAIN_MIN_DB = -20.0
GAIN_MAX_DB = 20.0

# Attack range (logarithmic in ms)
ATTACK_MIN_MS = 0.1
ATTACK_MAX_MS = 1000.0

# Release range (logarithmic in ms)
RELEASE_MIN_MS = 1.0
RELEASE_MAX_MS = 5000.0

# Type (knee). Hard / Smooth are the two FL stock knee modes.
KNEE_HARD = 0.0
KNEE_SMOOTH = 1.0
KNEES = {"hard": KNEE_HARD, "smooth": KNEE_SMOOTH}
KNEES_REVERSE = {0.0: "hard", 1.0: "smooth"}


# ═══════════════════════════════════════════════════════════════════
# CONVERSION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def threshold_to_param(db: float) -> float:
    """Convert threshold in dB to normalized parameter (0.0-1.0)."""
    db = _clamp(db, THRESHOLD_MIN_DB, THRESHOLD_MAX_DB)
    return (db - THRESHOLD_MIN_DB) / (THRESHOLD_MAX_DB - THRESHOLD_MIN_DB)


def param_to_threshold(value: float) -> float:
    """Convert normalized parameter to threshold in dB."""
    return THRESHOLD_MIN_DB + value * (THRESHOLD_MAX_DB - THRESHOLD_MIN_DB)


def ratio_to_param(ratio: float) -> float:
    """Convert ratio (e.g. 4.0 for 4:1) to normalized parameter (0.0-1.0)."""
    ratio = _clamp(ratio, RATIO_MIN, RATIO_MAX)
    return (ratio - RATIO_MIN) / (RATIO_MAX - RATIO_MIN)


def param_to_ratio(value: float) -> float:
    """Convert normalized parameter to compression ratio (1:1 → 20:1)."""
    return RATIO_MIN + value * (RATIO_MAX - RATIO_MIN)


def gain_to_param(db: float) -> float:
    """Convert makeup gain in dB to normalized parameter (0.0-1.0)."""
    db = _clamp(db, GAIN_MIN_DB, GAIN_MAX_DB)
    return (db - GAIN_MIN_DB) / (GAIN_MAX_DB - GAIN_MIN_DB)


def param_to_gain(value: float) -> float:
    """Convert normalized parameter to makeup gain in dB."""
    return GAIN_MIN_DB + value * (GAIN_MAX_DB - GAIN_MIN_DB)


def attack_to_param(ms: float) -> float:
    """Convert attack time in ms to normalized parameter (logarithmic)."""
    ms = _clamp(ms, ATTACK_MIN_MS, ATTACK_MAX_MS)
    return math.log(ms / ATTACK_MIN_MS) / math.log(ATTACK_MAX_MS / ATTACK_MIN_MS)


def param_to_attack(value: float) -> float:
    """Convert normalized parameter to attack time in ms."""
    return ATTACK_MIN_MS * ((ATTACK_MAX_MS / ATTACK_MIN_MS) ** value)


def release_to_param(ms: float) -> float:
    """Convert release time in ms to normalized parameter (logarithmic)."""
    ms = _clamp(ms, RELEASE_MIN_MS, RELEASE_MAX_MS)
    return math.log(ms / RELEASE_MIN_MS) / math.log(RELEASE_MAX_MS / RELEASE_MIN_MS)


def param_to_release(value: float) -> float:
    """Convert normalized parameter to release time in ms."""
    return RELEASE_MIN_MS * ((RELEASE_MAX_MS / RELEASE_MIN_MS) ** value)


def knee_to_param(knee: str) -> float:
    """Convert knee name (``hard``/``smooth``) to normalized parameter."""
    key = knee.lower().strip()
    if key not in KNEES:
        raise ValueError(f"Unknown knee: {knee!r}. Valid: {list(KNEES.keys())}")
    return KNEES[key]


def param_to_knee(value: float) -> str:
    """Convert normalized parameter to knee name (rounds to nearest)."""
    return "smooth" if value >= 0.5 else "hard"


# ═══════════════════════════════════════════════════════════════════
# HIGH-LEVEL OPERATIONS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CompressorState:
    """Human-readable representation of a Fruity Compressor's current
    parameter values (decoded from a ``read_mixer_track`` response)."""

    threshold_db: float
    ratio: float
    gain_db: float
    attack_ms: float
    release_ms: float
    knee: str

    def summary(self) -> str:
        return (
            f"Comp: thresh {self.threshold_db:+.1f} dB, "
            f"ratio {self.ratio:.1f}:1, "
            f"attack {self.attack_ms:.1f} ms, "
            f"release {self.release_ms:.0f} ms, "
            f"gain {self.gain_db:+.1f} dB, "
            f"knee {self.knee}"
        )


def decode_state(param_values: dict[int, float]) -> CompressorState:
    """Decode a Fruity Compressor's normalized parameter dict into human units.

    ``param_values`` is keyed by VST parameter ID (0-5)."""
    return CompressorState(
        threshold_db=param_to_threshold(param_values[PARAM_THRESHOLD]),
        ratio=param_to_ratio(param_values[PARAM_RATIO]),
        gain_db=param_to_gain(param_values[PARAM_GAIN]),
        attack_ms=param_to_attack(param_values[PARAM_ATTACK]),
        release_ms=param_to_release(param_values[PARAM_RELEASE]),
        knee=param_to_knee(param_values[PARAM_TYPE]),
    )


def build_compressor_commands(
    track_id: int,
    slot: int,
    threshold_db: float | None = None,
    ratio: float | None = None,
    gain_db: float | None = None,
    attack_ms: float | None = None,
    release_ms: float | None = None,
    knee: str | None = None,
) -> list[dict[str, Any]]:
    """Build a list of ``set_plugin_param`` commands for a Fruity Compressor
    adjustment. Only the parameters explicitly passed are written — everything
    else is left at its current FL value.

    Returns a list of dicts: ``[{"track_id", "slot", "param_id", "value"}, ...]``.
    """
    commands: list[dict[str, Any]] = []

    if threshold_db is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_THRESHOLD,
            "value": threshold_to_param(threshold_db),
        })

    if ratio is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_RATIO,
            "value": ratio_to_param(ratio),
        })

    if gain_db is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_GAIN,
            "value": gain_to_param(gain_db),
        })

    if attack_ms is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_ATTACK,
            "value": attack_to_param(attack_ms),
        })

    if release_ms is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_RELEASE,
            "value": release_to_param(release_ms),
        })

    if knee is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": PARAM_TYPE,
            "value": knee_to_param(knee),
        })

    return commands
