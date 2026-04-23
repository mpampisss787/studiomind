"""Tests for the decisions log (studiomind.decisions)."""

from __future__ import annotations

import time

import pytest

from studiomind.decisions import (
    OUTCOME_KEPT,
    OUTCOME_PENDING,
    OUTCOME_REVERTED,
    PENDING_STALE_AFTER_S,
    Decision,
    DecisionsLog,
)


def test_load_missing_file_returns_empty(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    assert log.decisions == []


def test_append_persists_and_reloads(tmp_path):
    path = tmp_path / "decisions.json"
    log = DecisionsLog.load(path)
    log.append(
        tool="set_builtin_eq",
        params={"track_id": 3, "band": 1, "gain": 0.45},
        description="mid cut on track 3",
        track_id=3,
        track_name="Bass",
    )
    assert path.exists()

    reloaded = DecisionsLog.load(path)
    assert len(reloaded.decisions) == 1
    d = reloaded.decisions[0]
    assert d.tool == "set_builtin_eq"
    assert d.track_id == 3
    assert d.track_name == "Bass"
    assert d.outcome == OUTCOME_PENDING


def test_mark_last_reverted_marks_most_recent_pending(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    log.append(tool="set_mixer_volume", params={"track_id": 1, "value": 0.7}, description="a")
    log.append(tool="set_mixer_volume", params={"track_id": 2, "value": 0.6}, description="b")

    # Artificially mark the older one as kept, so only the newer is pending.
    log.decisions[0].outcome = OUTCOME_KEPT
    log.save()

    reverted = log.mark_last_reverted()
    assert reverted is not None
    assert reverted.params["track_id"] == 2
    assert log.decisions[1].outcome == OUTCOME_REVERTED
    assert log.decisions[0].outcome == OUTCOME_KEPT


def test_mark_last_reverted_no_pending_returns_none(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    log.append(tool="set_mixer_volume", params={"track_id": 1, "value": 0.7}, description="a")
    log.decisions[0].outcome = OUTCOME_KEPT
    log.save()

    assert log.mark_last_reverted() is None


def test_age_pending_flips_old_pendings_to_kept(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    log.append(tool="set_mixer_volume", params={"track_id": 1, "value": 0.7}, description="old")
    log.append(tool="set_mixer_volume", params={"track_id": 2, "value": 0.6}, description="new")

    # Backdate the first decision past the stale cutoff.
    log.decisions[0].timestamp = time.time() - PENDING_STALE_AFTER_S - 100
    log.save()

    aged = log.age_pending()
    assert aged == 1
    assert log.decisions[0].outcome == OUTCOME_KEPT
    assert log.decisions[1].outcome == OUTCOME_PENDING  # still recent


def test_recent_returns_tail(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    for i in range(5):
        log.append(tool="set_mixer_volume", params={"track_id": i, "value": 0.5}, description=str(i))

    tail = log.recent(limit=3)
    assert len(tail) == 3
    assert [d.params["track_id"] for d in tail] == [2, 3, 4]


def test_summary_counts(tmp_path):
    log = DecisionsLog.load(tmp_path / "decisions.json")
    log.append(tool="t", params={}, description="a")
    log.append(tool="t", params={}, description="b")
    log.append(tool="t", params={}, description="c")
    log.decisions[0].outcome = OUTCOME_KEPT
    log.decisions[1].outcome = OUTCOME_REVERTED
    counts = log.summary_counts()
    assert counts == {OUTCOME_PENDING: 1, OUTCOME_KEPT: 1, OUTCOME_REVERTED: 1}


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text("{not valid json", encoding="utf-8")
    log = DecisionsLog.load(path)
    assert log.decisions == []
    # Can still append after recovery.
    log.append(tool="t", params={}, description="x")
    assert len(log.decisions) == 1
