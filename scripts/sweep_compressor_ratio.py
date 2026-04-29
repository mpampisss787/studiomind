"""
Six-point ratio sweep for the Fruity Compressor.

The wrapper currently uses a two-point quadratic fit for ratio. To pin
the actual FL curve, we set the ratio param to six evenly-spaced values
(0.0, 0.2, 0.4, 0.6, 0.8, 1.0) and ask the user to read the FL display
for each. With six points we can fit a proper polynomial / piecewise
curve and update the constants in fruity_compressor.py.

Usage (from repo root, with FL open + Fruity Compressor on a mixer track):
    python scripts/sweep_compressor_ratio.py --track 1 --slot 0
"""

from __future__ import annotations

import argparse
import sys
import time

from studiomind.bridge.commands import FLStudio
from studiomind.plugins.fruity_compressor import PARAM_RATIO


SWEEP_POINTS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--track", type=int, required=True)
    ap.add_argument("--slot", type=int, required=True)
    ap.add_argument("--dwell", type=float, default=2.0,
                    help="Seconds to wait at each value so you can read the knob (default 2.0)")
    args = ap.parse_args()

    fl = FLStudio()
    fl.connect()
    print("\n=== Fruity Compressor RATIO sweep ===")
    print("Right-click the RATIO knob -> 'Edit value' at each step.\n")
    try:
        for p in SWEEP_POINTS:
            fl.set_plugin_param(track_id=args.track, slot=args.slot, param_id=PARAM_RATIO, value=p)
            print(f"  param = {p:.2f}   <- read the RATIO knob now")
            time.sleep(args.dwell)
    finally:
        fl.disconnect()

    print(
        "\nPaste the six readback values (FL displayed ratio) like this:\n"
        "  param 0.0 -> ____ : 1\n"
        "  param 0.2 -> ____ : 1\n"
        "  param 0.4 -> ____ : 1\n"
        "  param 0.6 -> ____ : 1\n"
        "  param 0.8 -> ____ : 1\n"
        "  param 1.0 -> ____ : 1\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
