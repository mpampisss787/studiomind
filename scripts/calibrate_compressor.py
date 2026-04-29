"""
One-shot calibration helper for Fruity Compressor.

Drives our wrapper to set Run A or Run B values, then prints exactly which
knobs to read in FL. The user types the FL-displayed numbers back, we feed
them into a curve fit and update fruity_compressor.py constants.

Usage (from repo root, with FL open + Fruity Compressor on a mixer track):
    python scripts/calibrate_compressor.py --track 4 --slot 0 --run A
    python scripts/calibrate_compressor.py --track 4 --slot 0 --run B

Run A (light comp):    threshold -12 dB, ratio 2:1, attack 10 ms, release 80 ms, gain +1 dB, hard knee
Run B (aggressive):    threshold -30 dB, ratio 8:1, attack 1 ms, release 200 ms, gain  0 dB, hard knee
"""

from __future__ import annotations

import argparse
import sys

from studiomind.bridge.commands import FLStudio
from studiomind.plugins.fruity_compressor import (
    PARAM_ATTACK,
    PARAM_GAIN,
    PARAM_RATIO,
    PARAM_RELEASE,
    PARAM_THRESHOLD,
    PARAM_TYPE,
    attack_to_param,
    gain_to_param,
    knee_to_param,
    ratio_to_param,
    release_to_param,
    threshold_to_param,
)


RUNS = {
    "A": {
        "label": "Run A (light)",
        "threshold_db": -12.0,
        "ratio": 2.0,
        "attack_ms": 10.0,
        "release_ms": 80.0,
        "gain_db": 1.0,
        "knee": "hard",
    },
    "B": {
        "label": "Run B (aggressive)",
        "threshold_db": -30.0,
        "ratio": 8.0,
        "attack_ms": 1.0,
        "release_ms": 200.0,
        "gain_db": 0.0,
        "knee": "hard",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--track", type=int, required=True, help="Mixer track index (visible in FL mixer)")
    ap.add_argument("--slot", type=int, required=True, help="FX slot the Fruity Compressor sits in (0-9)")
    ap.add_argument("--run", choices=["A", "B"], required=True, help="Which calibration run to set")
    args = ap.parse_args()

    spec = RUNS[args.run]
    print(f"\n=== {spec['label']} on track {args.track} slot {args.slot} ===\n")

    fl = FLStudio()
    fl.connect()
    try:
        writes = [
            ("threshold",  PARAM_THRESHOLD, threshold_to_param(spec["threshold_db"]),  f"{spec['threshold_db']:+.1f} dB"),
            ("ratio",      PARAM_RATIO,     ratio_to_param(spec["ratio"]),             f"{spec['ratio']:.1f}:1"),
            ("attack",     PARAM_ATTACK,    attack_to_param(spec["attack_ms"]),        f"{spec['attack_ms']:.1f} ms"),
            ("release",    PARAM_RELEASE,   release_to_param(spec["release_ms"]),      f"{spec['release_ms']:.0f} ms"),
            ("gain",       PARAM_GAIN,      gain_to_param(spec["gain_db"]),            f"{spec['gain_db']:+.1f} dB"),
            ("type/knee",  PARAM_TYPE,      knee_to_param(spec["knee"]),               spec["knee"]),
        ]
        for name, pid, value, intended in writes:
            fl.set_plugin_param(track_id=args.track, slot=args.slot, param_id=pid, value=value)
            print(f"  set {name:10s} (param {pid}) -> norm {value:.6f}   intended: {intended}")
    finally:
        fl.disconnect()

    print(
        "\nNow in FL Studio, right-click each knob -> 'Edit value' (or hover for tooltip)\n"
        "and read what it actually shows. Tell me these six numbers:\n"
        "  - THRESHOLD reads:  ____ dB\n"
        "  - RATIO     reads:  ____ : 1\n"
        "  - ATTACK    reads:  ____ ms\n"
        "  - RELEASE   reads:  ____ ms\n"
        "  - GAIN      reads:  ____ dB\n"
        "  - TYPE/KNEE reads:  ____ (hard/smooth)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
