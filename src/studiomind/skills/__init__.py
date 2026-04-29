"""Hermetic skill directories.

Each skill is a self-contained directory under this package, with five
files (plus a calibration-logs subdir):

    src/studiomind/skills/<name>/
      __init__.py
      manifest.json
      wrapper.py
      tool.py
      knowledge.md
      tests.py
      calibration-logs/
        <iso-timestamp>.json

The registry (``_registry.py``) walks subdirectories at session start,
validates each manifest, imports wrapper + tool modules, and yields
``Skill`` objects to the agent loops.

Anything not following this layout is not loaded — the registry is
strict about structure to keep the loading semantics predictable.
"""
