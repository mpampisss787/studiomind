"""Tests for WorkspaceSession — pending lifecycle, watcher, collect, staleness."""

from __future__ import annotations

import time
from pathlib import Path

from studiomind.workspace import (
    KIND_MASTER,
    KIND_STEM,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_STALE,
    WorkspaceSession,
    open_project,
)


class FakeFL:
    """Minimal stand-in for FLStudio for tests — records calls, returns canned data."""

    def __init__(self) -> None:
        self.solo_calls: list[tuple[int, bool]] = []
        self.tracks: dict[int, dict] = {
            0: {"index": 0, "name": "Master", "volume": 0.8},
            3: {"index": 3, "name": "Bass", "volume": 0.78, "plugins": []},
            5: {"index": 5, "name": "Kick", "volume": 0.80, "plugins": []},
        }

    def read_mixer_track(self, track_id: int) -> dict:
        return dict(self.tracks[track_id])

    def solo_track(self, track_id: int, solo: bool = True) -> dict:
        self.solo_calls.append((track_id, solo))
        self.tracks[track_id]["solo"] = solo
        return {"ok": True}

    def read_project_state(self) -> dict:
        return {"mixer_tracks": list(self.tracks.values())}


def _write_fake_wav(path: Path, size: int = 2000) -> None:
    """Write a fake "audio file" of a given size. Content doesn't matter for watcher tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


def _fake_analyze(path: Path) -> dict:
    """Analysis stub — real analyzer needs real WAV headers."""
    return {"lufs": -10.5, "peak_db": -1.0, "path": str(path)}


def _session(tmp_path: Path) -> WorkspaceSession:
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    return WorkspaceSession(fl, project, analyze_fn=_fake_analyze)


def test_prepare_stem_writes_pending_and_solos(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    result = sess.prepare_stem(track_id=3)

    assert result["ok"] is True
    assert result["pending"] is True
    assert result["track_id"] == 3
    assert result["track_name"] == "Bass"
    assert result["filename"] == "track_003_bass.wav"
    assert "Ctrl+R" in result["instruction"] or "Export" in result["instruction"]

    # Track 3 should be soloed
    assert (3, True) in fl.solo_calls

    # Manifest has a pending entry
    manifest = project.load_manifest()
    assert 3 in manifest.stems
    assert manifest.stems[3].status == STATUS_PENDING
    assert manifest.stems[3].fl_state_hash is not None


def test_prepare_stem_removes_stale_file(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    # A leftover file from a previous session
    stale = project.stems_dir / "track_003_bass.wav"
    _write_fake_wav(stale, size=500)
    assert stale.exists()

    sess.prepare_stem(track_id=3)
    assert not stale.exists(), "prepare_stem should remove any pre-existing file"


def test_prepare_master_unsolos_everything(tmp_path: Path):
    fl = FakeFL()
    fl.tracks[3]["solo"] = True
    fl.tracks[5]["solo"] = True
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    result = sess.prepare_master()

    assert result["ok"] is True
    assert result["mode"] == "master"
    assert result["filename"].startswith("master_") and result["filename"].endswith(".wav")

    # Both soloed tracks should have been un-soloed
    assert (3, False) in fl.solo_calls
    assert (5, False) in fl.solo_calls

    # Manifest has a pending master entry
    manifest = project.load_manifest()
    assert len(manifest.masters) == 1
    assert manifest.masters[0].status == STATUS_PENDING


def test_watcher_flips_pending_to_ready(tmp_path: Path):
    sess = _session(tmp_path)
    sess.prepare_stem(track_id=3)
    sess.start()
    try:
        stem_path = sess.project.stems_dir / "track_003_bass.wav"
        _write_fake_wav(stem_path, size=1500)

        # Wait for watcher to detect stable size (need STABLE_POLLS_NEEDED identical polls)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            rec = sess.manifest.stems.get(3)
            if rec and rec.status == STATUS_READY:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("Watcher did not flip to READY within 5s")

        assert sess.manifest.stems[3].status == STATUS_READY
        assert sess.manifest.stems[3].rendered_at is not None
    finally:
        sess.stop()


def test_collect_returns_analysis_and_unsolos(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    sess.prepare_stem(track_id=3)
    sess.start()
    try:
        _write_fake_wav(project.stems_dir / "track_003_bass.wav", size=1500)
        result = sess.collect(track_id=3, timeout_s=5.0)
    finally:
        sess.stop()

    assert result["ok"] is True
    assert result["filename"] == "track_003_bass.wav"
    assert result["analysis"]["lufs"] == -10.5

    # Un-solo happened
    assert (3, False) in fl.solo_calls


def test_collect_times_out_when_no_file(tmp_path: Path):
    sess = _session(tmp_path)
    sess.prepare_stem(track_id=3)
    sess.start()
    try:
        import pytest

        with pytest.raises(TimeoutError):
            sess.collect(track_id=3, timeout_s=1.0)
    finally:
        sess.stop()


def test_collect_errors_on_unknown_target(tmp_path: Path):
    sess = _session(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        sess.collect(track_id=999, timeout_s=0.5)


def test_status_reports_full_state(tmp_path: Path):
    sess = _session(tmp_path)
    sess.prepare_stem(track_id=3)
    # Drop a reference file
    (sess.project.references_dir / "doja_cat.wav").write_bytes(b"ref")

    status = sess.status()
    assert status["project_name"] == "Demo"
    assert any(s["track_id"] == 3 for s in status["stems"])
    assert status["stems"][0]["status"] == STATUS_PENDING
    assert "doja_cat.wav" in status["references"]


def test_prepare_batch_render_creates_pending_for_each_track(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    result = sess.prepare_batch_render(include_master=True)

    assert result["ok"] is True
    assert result["mode"] == "batch"
    # FakeFL has track 3 (Bass), 5 (Kick); track 0 (Master) is excluded
    assert result["track_count"] == 2
    track_ids = {t["track_id"] for t in result["tracks_prepared"]}
    assert track_ids == {3, 5}

    manifest = project.load_manifest()
    assert set(manifest.stems.keys()) == {3, 5}
    for rec in manifest.stems.values():
        assert rec.status == STATUS_PENDING
    # Master entry was also queued
    assert len(manifest.masters) == 1


def test_watcher_fuzzy_matches_fl_batch_export_names(tmp_path: Path):
    """FL's batch export writes files named after tracks, not our canonical scheme."""
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    sess.prepare_batch_render(include_master=False)
    sess.start()
    try:
        # FL writes files with its own naming (e.g., project_Bass.wav, project_Kick.wav)
        _write_fake_wav(project.stems_dir / "project_Bass.wav")
        _write_fake_wav(project.stems_dir / "project_Kick.wav")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(
                rec.status == STATUS_READY
                for rec in sess.manifest.stems.values()
            ):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("Watcher did not match batch files within 5s")

        # Both records should be READY and bound to the actual filenames FL wrote
        assert sess.manifest.stems[3].filename == "project_Bass.wav"
        assert sess.manifest.stems[5].filename == "project_Kick.wav"
    finally:
        sess.stop()


def test_watcher_respects_slug_word_boundary(tmp_path: Path):
    """
    Regression for the koto bug 2026-04-24: a track named 'koto thing' (slug
    'koto_thing') was matching an FL batch-export file for a different track
    'koto thing #2' (slug 'koto_thing_2') because the match was a raw substring
    — 'koto_thing' appears inside 'koto_koto_thing_2'.

    The fix: match only at word boundaries (whole equal or ends with '_<slug>'),
    mirroring FL's "<project>_<track>.wav" naming convention.
    """
    fl = FakeFL()
    fl.tracks[14] = {"index": 14, "name": "koto thing", "volume": 0.78, "plugins": []}
    fl.tracks[15] = {"index": 15, "name": "koto thing #2", "volume": 0.78, "plugins": []}
    project = open_project("koto", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    sess.prepare_batch_render(include_master=False)
    sess.start()
    try:
        # FL batch export writes files named "<project>_<track>.wav". The two
        # candidates here are the exact pattern that broke in the koto log:
        # "koto_thing_2" is a strict superset substring of "koto_thing".
        _write_fake_wav(project.stems_dir / "koto_koto thing.wav")
        _write_fake_wav(project.stems_dir / "koto_koto thing #2.wav")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (
                sess.manifest.stems[14].status == STATUS_READY
                and sess.manifest.stems[15].status == STATUS_READY
            ):
                break
            time.sleep(0.2)

        assert sess.manifest.stems[14].filename == "koto_koto thing.wav", (
            f"track 14 'koto thing' must bind to its own file, not track 15's #2"
        )
        assert sess.manifest.stems[15].filename == "koto_koto thing #2.wav"
    finally:
        sess.stop()


def test_watcher_prefers_specific_over_generic_slug(tmp_path: Path):
    """With both 'Bass' and 'Sub Bass' tracks, a file named 'Sub Bass.wav' binds to Sub Bass."""
    fl = FakeFL()
    fl.tracks[7] = {"index": 7, "name": "Sub Bass", "volume": 0.7, "plugins": []}
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    sess.prepare_batch_render(include_master=False)
    sess.start()
    try:
        _write_fake_wav(project.stems_dir / "Sub Bass.wav")
        _write_fake_wav(project.stems_dir / "Bass.wav")
        _write_fake_wav(project.stems_dir / "Kick.wav")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(
                rec.status == STATUS_READY
                for rec in sess.manifest.stems.values()
            ):
                break
            time.sleep(0.2)

        assert sess.manifest.stems[7].filename == "Sub Bass.wav"  # Sub Bass, not Bass.wav
        assert sess.manifest.stems[3].filename == "Bass.wav"
        assert sess.manifest.stems[5].filename == "Kick.wav"
    finally:
        sess.stop()


def test_detect_external_changes_reports_diff(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    # Render two tracks; track 3 was rendered, track 5 never rendered
    sess.prepare_stem(track_id=3)
    sess.manifest.stems[3].status = STATUS_READY

    # Track 5 exists in FL but no manifest entry yet — should show as "never rendered"
    result = sess.detect_external_changes()
    assert len(result["tracks_unchanged"]) == 1
    assert result["tracks_unchanged"][0] == 3
    assert any(t["track_id"] == 5 for t in result["tracks_never_rendered"])

    # Now mutate track 3 externally (e.g., user changed volume in FL)
    fl.tracks[3]["volume"] = 0.42
    result = sess.detect_external_changes()
    assert any(t["track_id"] == 3 for t in result["tracks_changed"])
    assert 3 not in result["tracks_unchanged"]


def test_refresh_staleness_flags_changed_track(tmp_path: Path):
    fl = FakeFL()
    project = open_project("Demo", root=tmp_path)
    sess = WorkspaceSession(fl, project, analyze_fn=_fake_analyze)

    # Render + mark ready manually
    sess.prepare_stem(track_id=3)
    sess.manifest.stems[3].status = STATUS_READY
    # No change yet -> not stale
    assert sess.refresh_staleness() == []

    # Mutate the track in FL -> state hash changes
    fl.tracks[3]["volume"] = 0.5
    newly_stale = sess.refresh_staleness()
    assert 3 in newly_stale
    assert sess.manifest.stems[3].status == STATUS_STALE
