"""
Batch-enumerate every plugin currently loaded in the FL mixer.

Walks all mixer tracks (1..max_track) and every plugin slot (0..9), writes
one JSON per unique plugin name into src/studiomind/plugins/. Already-captured
plugins are skipped unless --overwrite is passed.

Intended use:

    1. Open FL, load each plugin you want wrappers for onto any free mixer
       tracks (one per track is fine; duplicates are detected and skipped).
    2. Run once:
           python scripts/batch_enumerate_plugins.py
    3. Commit everything in src/studiomind/plugins/.

The JSONs are the param-ID source of truth. Typed wrappers are built offline
against these files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from studiomind.bridge.commands import FLStudio


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "src" / "studiomind" / "plugins"


def slugify(name: str) -> str:
    """Turn 'Fruity Compressor' → 'fruity_compressor'."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower())
    return s.strip("_") or "unknown"


def write_record(plugin: dict, track_id: int, overwrite: bool) -> tuple[Path, bool]:
    """Write one plugin's JSON. Returns (path, was_written)."""
    name = plugin.get("name") or "unknown"
    slug = slugify(name)
    out_path = PLUGINS_DIR / f"{slug}_params.json"

    if out_path.exists() and not overwrite:
        return out_path, False

    params = plugin.get("params") or []
    record = {
        "plugin_file_name": slug,
        "plugin_reported_name": name,
        "source_track_id": track_id,
        "source_slot": plugin.get("slot"),
        "num_params": len(params),
        "params": [
            {
                "id": i,
                "name": p.get("name", ""),
                "default_value": p.get("value"),
                "default_display": p.get("display", ""),
            }
            for i, p in enumerate(params)
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return out_path, True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-track", type=int, default=125, help="Highest mixer track index to scan (default: 125 — FL's max)")
    ap.add_argument("--min-track", type=int, default=1, help="Lowest mixer track index to scan (default: 1 — skip Master)")
    ap.add_argument("--overwrite", action="store_true", help="Replace existing plugin JSONs instead of skipping")
    args = ap.parse_args()

    fl = FLStudio()
    fl.connect()

    written: list[tuple[str, Path]] = []
    skipped_existing: list[str] = []
    skipped_duplicate: list[str] = []
    seen_slugs: set[str] = set()
    tracks_with_plugins = 0
    empty_tracks = 0
    errors: list[tuple[int, str]] = []

    try:
        for track_id in range(args.min_track, args.max_track + 1):
            try:
                track = fl.read_mixer_track(track_id)
            except Exception as e:
                errors.append((track_id, str(e)))
                continue

            track_plugins = track.get("plugins") or []
            if not track_plugins:
                empty_tracks += 1
                continue
            tracks_with_plugins += 1

            for plugin in track_plugins:
                if plugin is None:
                    continue
                name = plugin.get("name") or "unknown"
                slug = slugify(name)

                if slug in seen_slugs:
                    skipped_duplicate.append(f"{name} (track {track_id})")
                    continue
                seen_slugs.add(slug)

                path, was_written = write_record(plugin, track_id, args.overwrite)
                if was_written:
                    num_params = len(plugin.get("params") or [])
                    written.append((f"{name} [{num_params} params]", path))
                else:
                    skipped_existing.append(name)
    finally:
        fl.disconnect()

    print(f"\nScanned tracks {args.min_track}..{args.max_track}")
    print(f"  Tracks with plugins:      {tracks_with_plugins}")
    print(f"  Empty tracks:             {empty_tracks}")
    print(f"  Unique plugins written:   {len(written)}")
    print(f"  Skipped (already exist):  {len(skipped_existing)}")
    print(f"  Skipped (duplicates):     {len(skipped_duplicate)}")
    if errors:
        print(f"  Track read errors:        {len(errors)}")

    if written:
        print("\nWritten:")
        for label, path in written:
            print(f"  + {label} → {path.relative_to(REPO_ROOT)}")
    if skipped_existing:
        print("\nSkipped (use --overwrite to refresh):")
        for name in skipped_existing:
            print(f"  = {name}")
    if errors:
        print("\nTrack read errors (usually safe to ignore — FL rejects indices above track count):")
        for tid, msg in errors[:5]:
            print(f"  ! track {tid}: {msg}")
        if len(errors) > 5:
            print(f"  ... and {len(errors) - 5} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
