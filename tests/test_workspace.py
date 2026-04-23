"""Tests for the workspace module — project folders, manifest, staleness."""

from __future__ import annotations

from pathlib import Path

import pytest

from studiomind.workspace import (
    KIND_MASTER,
    KIND_STEM,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_STALE,
    Manifest,
    Project,
    RenderRecord,
    hash_state,
    open_project,
    project_name_from_fl_path,
    slugify,
)


def test_slugify_common_cases():
    assert slugify("My Track") == "my_track"
    assert slugify("  Weird-Name_Here  ") == "weird_name_here"
    assert slugify("Kick (808)") == "kick_808"
    assert slugify("") == "unnamed"
    assert slugify("!!!") == "unnamed"


def test_project_name_from_fl_path():
    # None / empty → None so callers can chain with `or` fallbacks
    assert project_name_from_fl_path(None) is None
    assert project_name_from_fl_path("") is None
    assert project_name_from_fl_path(r"C:\Users\x\My Track v3.flp") == "My Track v3"
    assert project_name_from_fl_path("/home/x/demo.flp") == "demo"


def test_hash_state_is_stable():
    # Same input -> same hash, key order doesn't matter
    a = {"volume": 0.8, "pan": 0.5, "eq": [1, 2, 3]}
    b = {"pan": 0.5, "eq": [1, 2, 3], "volume": 0.8}
    assert hash_state(a) == hash_state(b)

    # Changed input -> different hash
    c = {"volume": 0.9, "pan": 0.5, "eq": [1, 2, 3]}
    assert hash_state(a) != hash_state(c)

    # Reasonable length
    assert len(hash_state(a)) == 16


def test_render_record_round_trip():
    rec = RenderRecord(
        kind=KIND_STEM,
        filename="track_003_bass.wav",
        status=STATUS_READY,
        track_id=3,
        track_name="Bass",
        fl_state_hash="abc123",
        rendered_at=1234567890.0,
        analysis={"lufs": -9.2},
    )
    d = rec.to_dict()
    restored = RenderRecord.from_dict(d)
    assert restored == rec


def test_manifest_round_trip():
    m = Manifest(project_name="Demo", fl_project_path=r"C:\x\Demo.flp")
    m.stems[3] = RenderRecord(
        kind=KIND_STEM,
        filename="track_003_bass.wav",
        status=STATUS_READY,
        track_id=3,
        track_name="Bass",
        fl_state_hash="h1",
        rendered_at=100.0,
    )
    m.masters.append(
        RenderRecord(
            kind=KIND_MASTER,
            filename="master_100.wav",
            status=STATUS_READY,
            fl_state_hash="h2",
            rendered_at=100.0,
        )
    )

    restored = Manifest.from_dict(m.to_dict())
    assert restored.project_name == "Demo"
    assert restored.fl_project_path == r"C:\x\Demo.flp"
    assert 3 in restored.stems
    assert restored.stems[3].track_name == "Bass"
    assert restored.stems[3].status == STATUS_READY
    assert len(restored.masters) == 1
    assert restored.masters[0].kind == KIND_MASTER


def test_open_project_creates_directories(tmp_path: Path):
    project = open_project("My Track", root=tmp_path)
    assert project.root == tmp_path / "my_track"
    assert project.stems_dir.is_dir()
    assert project.masters_dir.is_dir()
    assert project.references_dir.is_dir()
    assert project.meta_dir.is_dir()
    assert project.manifest_path.is_file()  # Fresh manifest written


def test_open_project_reuses_existing(tmp_path: Path):
    p1 = open_project("Demo", root=tmp_path)
    m = p1.load_manifest()
    m.stems[5] = RenderRecord(
        kind=KIND_STEM,
        filename="track_005_snare.wav",
        track_id=5,
        track_name="Snare",
    )
    p1.save_manifest(m)

    # Re-open — same folder, stem is still there
    p2 = open_project("Demo", root=tmp_path)
    assert p2.root == p1.root
    m2 = p2.load_manifest()
    assert 5 in m2.stems
    assert m2.stems[5].track_name == "Snare"


def test_open_project_updates_fl_path(tmp_path: Path):
    p = open_project("Demo", root=tmp_path, fl_project_path="/new/path.flp")
    m = p.load_manifest()
    assert m.fl_project_path == "/new/path.flp"

    # Re-open with a different path updates it
    open_project("Demo", root=tmp_path, fl_project_path="/another/path.flp")
    m2 = p.load_manifest()
    assert m2.fl_project_path == "/another/path.flp"


def test_stem_filename_deterministic(tmp_path: Path):
    p = open_project("X", root=tmp_path)
    assert p.stem_filename(3, "Bass Guitar") == "track_003_bass_guitar.wav"
    assert p.stem_filename(0, "Master") == "track_000_master.wav"
    assert p.stem_filename(127, "") == "track_127_unnamed.wav"


def test_master_filename_timestamped(tmp_path: Path):
    p = open_project("X", root=tmp_path)
    assert p.master_filename(1234567890) == "master_1234567890.wav"


def test_mark_stale_flags_changed_tracks(tmp_path: Path):
    p = open_project("X", root=tmp_path)
    m = p.load_manifest()
    m.stems[1] = RenderRecord(
        kind=KIND_STEM,
        filename="track_001_kick.wav",
        status=STATUS_READY,
        track_id=1,
        track_name="Kick",
        fl_state_hash="h1",
    )
    m.stems[2] = RenderRecord(
        kind=KIND_STEM,
        filename="track_002_snare.wav",
        status=STATUS_READY,
        track_id=2,
        track_name="Snare",
        fl_state_hash="h2",
    )
    # Track 1 still matches, track 2 has changed
    newly_stale = p.mark_stale(m, current_track_hashes={1: "h1", 2: "h2_CHANGED"})

    assert newly_stale == [2]
    assert m.stems[1].status == STATUS_READY
    assert m.stems[2].status == STATUS_STALE


def test_mark_stale_handles_removed_track(tmp_path: Path):
    p = open_project("X", root=tmp_path)
    m = p.load_manifest()
    m.stems[5] = RenderRecord(
        kind=KIND_STEM,
        filename="track_005_bass.wav",
        status=STATUS_READY,
        track_id=5,
        track_name="Bass",
        fl_state_hash="h5",
    )
    # Track 5 no longer exists in FL
    newly_stale = p.mark_stale(m, current_track_hashes={})
    assert newly_stale == [5]
    assert m.stems[5].status == STATUS_STALE


def test_mark_stale_ignores_pending(tmp_path: Path):
    p = open_project("X", root=tmp_path)
    m = p.load_manifest()
    m.stems[1] = RenderRecord(
        kind=KIND_STEM,
        filename="track_001_kick.wav",
        status=STATUS_PENDING,
        track_id=1,
        track_name="Kick",
        fl_state_hash=None,  # pending hasn't rendered yet
    )
    newly_stale = p.mark_stale(m, current_track_hashes={1: "whatever"})
    assert newly_stale == []
    assert m.stems[1].status == STATUS_PENDING


def test_manifest_file_is_valid_json(tmp_path: Path):
    """Manifest should be human-readable JSON with sorted keys."""
    p = open_project("X", root=tmp_path)
    text = p.manifest_path.read_text(encoding="utf-8")
    # Must parse
    import json as _json

    data = _json.loads(text)
    assert data["project_name"] == "X"
    # Indented (pretty-printed)
    assert "\n  " in text


def test_save_is_atomic(tmp_path: Path):
    """save_manifest writes through a temp file then replaces, so partial writes don't corrupt."""
    p = open_project("X", root=tmp_path)
    m = p.load_manifest()
    p.save_manifest(m)
    # No leftover .tmp file after a clean save
    tmp_file = p.manifest_path.with_suffix(".json.tmp")
    assert not tmp_file.exists()
