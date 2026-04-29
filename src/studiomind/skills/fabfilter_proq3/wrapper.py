"""
FabFilter Pro-Q 3 parameter mapping.

Pro-Q 3 has 10 EQ bands, each with 13 parameters (stride=13).
All values are normalized 0.0-1.0 in the VST interface.

Parameter layout per band (offset from band base):
  +0  Band Used       0.0=unused, 1.0=used
  +1  Enabled         0.0=disabled, 1.0=enabled
  +2  Frequency       log scale: 10 Hz (0.0) to 30000 Hz (1.0)
  +3  Gain            linear: -30 dB (0.0) to +30 dB (1.0), 0.5=0dB
  +4  Dynamic Range   -30 to +30 dB, 0.5=0dB
  +5  Dynamics Enabled
  +6  Threshold       1.0=auto
  +7  Q               log scale: 0.025 (0.0) to 40.0 (1.0), 0.5=1.0
  +8  Shape           see SHAPES dict
  +9  Slope           see SLOPES dict
  +10 Stereo Placement see STEREO dict
  +11 Speakers
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

PLUGIN_NAME = "FabFilter Pro-Q 3"
NUM_BANDS = 10
BAND_STRIDE = 13  # Parameter IDs per band

# Offsets within each band
OFF_USED = 0
OFF_ENABLED = 1
OFF_FREQ = 2
OFF_GAIN = 3
OFF_DYN_RANGE = 4
OFF_DYN_ENABLED = 5
OFF_THRESHOLD = 6
OFF_Q = 7
OFF_SHAPE = 8
OFF_SLOPE = 9
OFF_STEREO = 10
OFF_SPEAKERS = 11

# Shape values (normalized 0.0-1.0)
SHAPES = {
    "bell": 0.0,
    "low_shelf": 0.125,
    "low_cut": 0.25,
    "high_shelf": 0.375,
    "high_cut": 0.5,
    "notch": 0.625,
    "band_pass": 0.75,
    "tilt_shelf": 0.875,
}
SHAPES_REVERSE = {round(v, 3): k for k, v in SHAPES.items()}

# Slope values
SLOPES = {
    "6": 0.0,
    "12": 0.111,
    "18": 0.222,
    "24": 0.333,
    "36": 0.444,
    "48": 0.556,
    "72": 0.778,
    "96": 1.0,
}

# Stereo placement
STEREO = {
    "left": 0.0,
    "right": 0.167,
    "mid": 0.333,
    "side": 0.5,
    "stereo": 0.667,
}

# Frequency range
FREQ_MIN = 10.0
FREQ_MAX = 30000.0
FREQ_RATIO = FREQ_MAX / FREQ_MIN  # 3000

# Gain range
GAIN_MIN = -30.0
GAIN_MAX = 30.0

# Q range
Q_MIN = 0.025
Q_MAX = 40.0
Q_RATIO = Q_MAX / Q_MIN  # 1600


# ═══════════════════════════════════════════════════════════════════
# CONVERSION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def freq_to_param(hz: float) -> float:
    """Convert frequency in Hz to Pro-Q 3 normalized parameter (0.0-1.0)."""
    hz = max(FREQ_MIN, min(FREQ_MAX, hz))
    return math.log(hz / FREQ_MIN) / math.log(FREQ_RATIO)


def param_to_freq(value: float) -> float:
    """Convert Pro-Q 3 normalized parameter to frequency in Hz."""
    return FREQ_MIN * (FREQ_RATIO ** value)


def gain_to_param(db: float) -> float:
    """Convert gain in dB to Pro-Q 3 normalized parameter (0.0-1.0)."""
    db = max(GAIN_MIN, min(GAIN_MAX, db))
    return (db - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)


def param_to_gain(value: float) -> float:
    """Convert Pro-Q 3 normalized parameter to gain in dB."""
    return GAIN_MIN + value * (GAIN_MAX - GAIN_MIN)


def q_to_param(q: float) -> float:
    """Convert Q value to Pro-Q 3 normalized parameter (0.0-1.0)."""
    q = max(Q_MIN, min(Q_MAX, q))
    return math.log(q / Q_MIN) / math.log(Q_RATIO)


def param_to_q(value: float) -> float:
    """Convert Pro-Q 3 normalized parameter to Q value."""
    return Q_MIN * (Q_RATIO ** value)


def shape_to_param(shape: str) -> float:
    """Convert shape name to Pro-Q 3 parameter value."""
    shape = shape.lower().replace(" ", "_").replace("-", "_")
    if shape not in SHAPES:
        raise ValueError(f"Unknown shape: {shape}. Valid: {list(SHAPES.keys())}")
    return SHAPES[shape]


def slope_to_param(db_per_oct: int) -> float:
    """Convert slope in dB/oct to Pro-Q 3 parameter value."""
    key = str(db_per_oct)
    if key not in SLOPES:
        raise ValueError(f"Unknown slope: {db_per_oct}. Valid: {list(SLOPES.keys())}")
    return SLOPES[key]


# ═══════════════════════════════════════════════════════════════════
# BAND PARAMETER ID CALCULATION
# ═══════════════════════════════════════════════════════════════════

def band_param_id(band: int, offset: int) -> int:
    """
    Get the VST parameter ID for a specific band parameter.

    Args:
        band: Band number (1-10)
        offset: Parameter offset within the band (use OFF_* constants)

    Returns:
        VST parameter ID
    """
    if not 1 <= band <= NUM_BANDS:
        raise ValueError(f"Band must be 1-{NUM_BANDS}, got {band}")
    return (band - 1) * BAND_STRIDE + offset


# ═══════════════════════════════════════════════════════════════════
# HIGH-LEVEL EQ OPERATIONS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ProQ3Band:
    """Human-readable representation of a Pro-Q 3 band."""

    band: int
    used: bool
    enabled: bool
    frequency_hz: float
    gain_db: float
    q: float
    shape: str
    slope_db_oct: int
    stereo: str

    def summary(self) -> str:
        if not self.used:
            return f"Band {self.band}: unused"
        status = "ON" if self.enabled else "OFF"
        gain_str = f"+{self.gain_db:.1f}" if self.gain_db > 0 else f"{self.gain_db:.1f}"
        return (
            f"Band {self.band} [{status}]: {self.shape} @ {self.frequency_hz:.0f} Hz, "
            f"{gain_str} dB, Q={self.q:.2f}, {self.slope_db_oct} dB/oct, {self.stereo}"
        )


def build_eq_commands(
    track_id: int,
    slot: int,
    band: int,
    frequency_hz: float | None = None,
    gain_db: float | None = None,
    q: float | None = None,
    shape: str | None = None,
    slope_db_oct: int | None = None,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """
    Build a list of set_plugin_param commands for a Pro-Q 3 EQ adjustment.

    Returns a list of dicts: [{"track_id", "slot", "param_id", "value"}, ...]
    """
    commands = []

    # Always mark band as used and enabled
    commands.append({
        "track_id": track_id,
        "slot": slot,
        "param_id": band_param_id(band, OFF_USED),
        "value": 1.0,
    })
    commands.append({
        "track_id": track_id,
        "slot": slot,
        "param_id": band_param_id(band, OFF_ENABLED),
        "value": 1.0 if enabled else 0.0,
    })

    if frequency_hz is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": band_param_id(band, OFF_FREQ),
            "value": freq_to_param(frequency_hz),
        })

    if gain_db is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": band_param_id(band, OFF_GAIN),
            "value": gain_to_param(gain_db),
        })

    if q is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": band_param_id(band, OFF_Q),
            "value": q_to_param(q),
        })

    if shape is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": band_param_id(band, OFF_SHAPE),
            "value": shape_to_param(shape),
        })

    if slope_db_oct is not None:
        commands.append({
            "track_id": track_id,
            "slot": slot,
            "param_id": band_param_id(band, OFF_SLOPE),
            "value": slope_to_param(slope_db_oct),
        })

    return commands
