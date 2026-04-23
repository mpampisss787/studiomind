"""Tests for global user preferences (studiomind.user_prefs)."""

from __future__ import annotations

from studiomind.user_prefs import (
    SOURCE_EXPLICIT,
    SOURCE_OBSERVATION,
    UserPreferences,
)


def test_load_missing_returns_empty(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    assert prefs.preferences == []


def test_record_persists(tmp_path):
    path = tmp_path / "user.json"
    prefs = UserPreferences.load(path)
    prefs.record("Never boost above 10kHz", source=SOURCE_EXPLICIT)
    assert path.exists()

    reloaded = UserPreferences.load(path)
    assert len(reloaded.preferences) == 1
    assert reloaded.preferences[0].statement == "Never boost above 10kHz"
    assert reloaded.preferences[0].source == SOURCE_EXPLICIT
    assert reloaded.preferences[0].strength == 1.0


def test_explicit_source_gets_full_strength(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    pref = prefs.record("Target -1dBTP master ceiling", source=SOURCE_EXPLICIT)
    assert pref.strength == 1.0


def test_observation_source_starts_lower(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    pref = prefs.record("Seems to prefer bell Q around 1.5", source=SOURCE_OBSERVATION)
    assert pref.strength == 0.4


def test_duplicate_statement_merges_and_bumps_strength(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    p1 = prefs.record("Never boost above 10kHz", source=SOURCE_OBSERVATION)
    assert p1.strength == 0.4

    p2 = prefs.record("Never boost above 10kHz", source=SOURCE_OBSERVATION)
    # Same id, strength bumped by 0.15
    assert p2.id == p1.id
    assert len(prefs.preferences) == 1
    assert abs(p2.strength - 0.55) < 1e-9


def test_similar_statements_merge(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    prefs.record("Never boost above 10kHz", source=SOURCE_OBSERVATION)
    # Substring match — "Never boost above 10kHz" contained in the longer version
    p = prefs.record("Never boost above 10kHz in any mix", source=SOURCE_EXPLICIT)
    assert len(prefs.preferences) == 1
    # Explicit re-statement upgrades source
    assert p.source == SOURCE_EXPLICIT


def test_explicit_restate_upgrades_source(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    prefs.record("Always leave master headroom", source=SOURCE_OBSERVATION)
    p = prefs.record("Always leave master headroom", source=SOURCE_EXPLICIT)
    assert p.source == SOURCE_EXPLICIT


def test_remove(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    pref = prefs.record("A specific thing we no longer want", source=SOURCE_EXPLICIT)
    assert prefs.remove(pref.id)
    assert prefs.preferences == []
    assert not prefs.remove(pref.id)  # idempotent


def test_sorted_for_agent_ranks_by_strength(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    prefs.record("Weak observation thing", source=SOURCE_OBSERVATION)
    prefs.record("Strong explicit rule xyz", source=SOURCE_EXPLICIT)
    prefs.record("Another medium thing", source=SOURCE_OBSERVATION)
    ordered = prefs.sorted_for_agent()
    assert ordered[0].source == SOURCE_EXPLICIT
    assert ordered[0].strength == 1.0


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "user.json"
    path.write_text("garbage not json", encoding="utf-8")
    prefs = UserPreferences.load(path)
    assert prefs.preferences == []
    # Still usable after recovery
    prefs.record("Recoverable", source=SOURCE_EXPLICIT)
    assert len(prefs.preferences) == 1


def test_empty_statement_rejected(tmp_path):
    prefs = UserPreferences.load(tmp_path / "user.json")
    import pytest
    with pytest.raises(ValueError):
        prefs.record("   ", source=SOURCE_EXPLICIT)
