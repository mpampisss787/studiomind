"""
One-shot cleanup for the prepare_stem_render misrouting bug (fixed in 205ef00).

Before the fix, `prepare_stem_render` wrote its WAV to `masters/` instead of
`stems/`. Repeated test runs accumulate stems in masters/:

  - **Orphans**: in masters/ with NO matching file in stems/ → `MOVE` to stems/
  - **Duplicates**: in masters/ that ALSO exist in stems/ (the batch export
    landed them correctly later) → `DELETE` the masters/ copy (only if
    --delete-duplicates is set)

Actual master renders (slug ends `_master`) are never touched.

Usage:
    python scripts/rescue_orphan_stems.py                             # dry-run, plan only
    python scripts/rescue_orphan_stems.py --apply                     # move orphans
    python scripts/rescue_orphan_stems.py --delete-duplicates         # dry-run, also shows dupes
    python scripts/rescue_orphan_stems.py --apply --delete-duplicates # move orphans AND delete dupes
    python scripts/rescue_orphan_stems.py --project koto              # one project only
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Keep this in sync with workspace.py::WORKSPACE_ROOT and slugify.
WORKSPACE_ROOT = Path.home() / "StudioMind" / "projects"


def slugify(name: str) -> str:
    """Same shape as studiomind.workspace.slugify. Inlined to avoid imports."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug or "untitled"


def is_master_wav(wav: Path) -> bool:
    """True if this filename looks like an FL batch master (slug ends _master)."""
    slug = slugify(wav.stem)
    return slug == "master" or slug.endswith("_master")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Actually move/delete files (default: dry-run)")
    ap.add_argument(
        "--delete-duplicates",
        action="store_true",
        help="Also delete masters/ copies that already exist in stems/",
    )
    ap.add_argument("--project", default=None, help="Only scan this project name")
    args = ap.parse_args()

    if not WORKSPACE_ROOT.exists():
        print(f"No workspace at {WORKSPACE_ROOT}. Nothing to do.")
        return 0

    projects = (
        [WORKSPACE_ROOT / args.project]
        if args.project
        else sorted(p for p in WORKSPACE_ROOT.iterdir() if p.is_dir())
    )

    total_found = 0
    total_moved = 0
    total_deleted = 0
    total_skipped = 0

    for project in projects:
        masters_dir = project / "masters"
        stems_dir   = project / "stems"
        if not masters_dir.exists():
            continue
        orphans = [w for w in masters_dir.glob("*.wav") if not is_master_wav(w)]
        if not orphans:
            continue

        print(f"\n── {project.name} ──")
        for wav in orphans:
            dest = stems_dir / wav.name
            total_found += 1

            if dest.exists():
                # Duplicate: a newer copy is already correctly in stems/
                if args.delete_duplicates:
                    if args.apply:
                        try:
                            wav.unlink()
                            print(f"  DELETE {wav.name}  (dupe of stems/)")
                            total_deleted += 1
                        except OSError as e:
                            print(f"  FAIL   {wav.name}  ({e})")
                    else:
                        print(f"  PLAN-DELETE {wav.name}  (dupe of stems/)")
                else:
                    print(f"  SKIP   {wav.name}  (dupe of stems/ — pass --delete-duplicates to remove)")
                    total_skipped += 1
                continue

            # Orphan: stems/ has no such file; move it over.
            if args.apply:
                stems_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(wav), str(dest))
                    print(f"  MOVE   {wav.name}  masters/ → stems/")
                    total_moved += 1
                except OSError as e:
                    print(f"  FAIL   {wav.name}  ({e})")
            else:
                print(f"  PLAN-MOVE   {wav.name}  masters/ → stems/")

    print()
    if args.apply:
        bits = [f"moved {total_moved}"]
        if args.delete_duplicates:
            bits.append(f"deleted {total_deleted}")
        bits.append(f"skipped {total_skipped}")
        bits.append(f"total scanned {total_found}")
        print("Done: " + ", ".join(bits) + ".")
    else:
        print(
            "Dry-run. Re-run with --apply to execute. "
            + ("(--delete-duplicates also set — masters/ copies will be removed.)"
               if args.delete_duplicates
               else "(Pass --delete-duplicates to clean masters/ copies that already exist in stems/.)")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
