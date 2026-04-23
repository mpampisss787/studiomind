"""
Dump the advertised parameters of a plugin loaded in FL Studio.

Run this ONCE per plugin you want a typed wrapper for (Fruity Compressor,
Fruity Limiter, Fruity Reeverb 2, etc.). The output is a JSON file that
gets committed into src/studiomind/plugins/<name>_params.json and consumed
by the typed wrapper module.

Why this exists: FL's stock plugin VST param IDs aren't documented
publicly. Every wrapper needs them. Rather than guess or reverse-engineer,
load the plugin in a spare mixer slot, run this, commit the JSON.

Usage:
    # 1. In FL: load Fruity Compressor on any mixer track
    # 2. Note the track ID (visible in mixer) and the slot it's in (0-9)
    # 3. Run this from the project dir with StudioMind ping'able:
    python scripts/enumerate_plugin_params.py --track 4 --slot 0 --name fruity_compressor
    # 4. Commit src/studiomind/plugins/fruity_compressor_params.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from studiomind.bridge.commands import FLStudio


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "src" / "studiomind" / "plugins"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--track", type=int, required=True, help="Mixer track index where the plugin is loaded")
    ap.add_argument("--slot", type=int, required=True, help="Plugin slot on that track (0–9)")
    ap.add_argument(
        "--name",
        required=True,
        help="File-safe name for the plugin (e.g. fruity_compressor, fruity_reeverb_2)",
    )
    ap.add_argument("--out", type=Path, default=None, help="Output JSON path (default: <plugins>/<name>_params.json)")
    args = ap.parse_args()

    fl = FLStudio()
    fl.connect()

    try:
        track = fl.read_mixer_track(args.track)
    except Exception as e:
        print(f"Could not read track {args.track}: {e}", file=sys.stderr)
        return 1
    finally:
        fl.disconnect()

    plugins = track.get("plugins") or []
    if args.slot >= len(plugins) or plugins[args.slot] is None:
        print(f"No plugin in track {args.track} slot {args.slot}.", file=sys.stderr)
        return 1

    plugin = plugins[args.slot]
    plugin_name = plugin.get("name", "<unknown>")
    params = plugin.get("params") or []

    record = {
        "plugin_file_name": args.name,
        "plugin_reported_name": plugin_name,
        "source_track_id": args.track,
        "source_slot": args.slot,
        "num_params": len(params),
        # Flat array: index = param_id. Each entry captures the advertised name
        # + the current (post-load default) value so the typed wrapper has both
        # the ID lookup and a sanity check for its unit conversions.
        "params": [
            {
                "id": i,
                "name": p.get("name", ""),
                "default_value": p.get("value"),
            }
            for i, p in enumerate(params)
        ],
    }

    out_path = args.out or (PLUGINS_DIR / f"{args.name}_params.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    print(f"Wrote {len(params)} params for '{plugin_name}' → {out_path}")
    # Surface likely-interesting names so the human can sanity-check at a glance.
    interesting_keywords = (
        "threshold", "ratio", "attack", "release", "gain", "knee",
        "wet", "dry", "size", "decay", "damp", "pre", "delay",
        "width", "mix",
    )
    hits = [
        p for p in record["params"]
        if any(kw in p["name"].lower() for kw in interesting_keywords)
    ]
    if hits:
        print("\nParams worth verifying (matched common effect keywords):")
        for p in hits:
            print(f"  {p['id']:4d}  {p['name']!r:40s}  default={p['default_value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
