"""Tests for the ``apply_sidechain`` agent tool.

The tool has three observable behaviours we want to lock in:

1. Idempotent on the routing — if a send already exists, don't re-create.
2. Plugin-aware advisory — different message when Fruity Compressor /
   Fruity Limiter / Maximus is loaded vs. not.
3. Decision logging — every call leaves a record in decisions.json so the
   memory layer can reason about reverts.

Live FL is not available in the test env, so we drive a FakeFL that
records every bridge call and returns canned ``read_mixer_track`` /
``set_send`` payloads.
"""

from __future__ import annotations

from typing import Any

import pytest

from studiomind.agent.tools import DESTRUCTIVE_TOOLS, TOOL_SCHEMAS, ToolExecutor


# ───────────────────────────── Fake FL ────────────────────────────────

class FakeFL:
    """Minimal FL stub: read_mixer_track returns the canned ``tracks`` dict
    for the given id; set_send appends to ``send_calls`` and reflects the
    new route in subsequent read_mixer_track responses."""

    def __init__(self, tracks: dict[int, dict]) -> None:
        self.tracks = tracks
        self.send_calls: list[dict[str, Any]] = []

    def read_mixer_track(self, track_id: int) -> dict:
        return {**self.tracks[track_id], "index": track_id}

    def set_send(
        self,
        source_track: int,
        dest_track: int,
        level: float = 0.8,
        enabled: bool = True,
    ) -> dict:
        self.send_calls.append({
            "source_track": source_track,
            "dest_track": dest_track,
            "level": level,
            "enabled": enabled,
        })
        # Reflect the new route in the source track's "routing" list so a
        # second read_mixer_track shows the send. Mirrors the device script.
        src = self.tracks.setdefault(source_track, {"name": f"track_{source_track}", "plugins": [], "routing": []})
        routes = src.setdefault("routing", [])
        if enabled and not any(r["dest"] == dest_track for r in routes):
            routes.append({"dest": dest_track, "dest_name": self.tracks[dest_track].get("name", ""), "level": level})
        return {
            "ok": True,
            "source": source_track,
            "dest": dest_track,
            "level": level,
            "now_active": enabled,
        }


def _executor(fl: FakeFL) -> ToolExecutor:
    return ToolExecutor(fl=fl, workspace=None)


# ───────────────────────────── Schema + registration ──────────────────

def test_apply_sidechain_in_schemas() -> None:
    names = {s["name"] for s in TOOL_SCHEMAS}
    assert "apply_sidechain" in names


def test_apply_sidechain_is_destructive() -> None:
    assert "apply_sidechain" in DESTRUCTIVE_TOOLS


def test_apply_sidechain_schema_required_fields() -> None:
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "apply_sidechain")
    required = schema["input_schema"]["required"]
    assert "source_track" in required
    assert "target_track" in required


# ───────────────────────────── Source == target guard ─────────────────

def test_self_sidechain_rejected() -> None:
    fl = FakeFL({5: {"name": "Bass", "plugins": [], "routing": []}})
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 5}
    )
    assert out["ok"] is False
    assert "differ" in out["error"]
    assert fl.send_calls == []


# ───────────────────────────── Happy path: comp loaded ────────────────

def test_creates_send_when_no_route_exists() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["ok"] is True
    assert out["send_already_existed"] is False
    assert len(fl.send_calls) == 1
    call = fl.send_calls[0]
    assert call["source_track"] == 5
    assert call["dest_track"] == 7
    assert call["enabled"] is True
    assert call["level"] == pytest.approx(0.8)


def test_advisory_when_comp_loaded_mentions_plugin_and_slot() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 2, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["advisory_status"] == "ready_for_dropdown"
    assert "Fruity Compressor" in out["advisory"]
    assert "slot 2" in out["advisory"]
    assert "Kick" in out["advisory"]
    assert "Bass" in out["advisory"]
    assert len(out["target_capable_plugins"]) == 1
    assert out["target_capable_plugins"][0]["name"] == "Fruity Compressor"
    assert out["target_capable_plugins"][0]["slot"] == 2


def test_advisory_lists_first_capable_plugin() -> None:
    """If multiple capable plugins are loaded, the advisory cites the first
    one; the agent can re-call with explicit guidance if needed."""
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Limiter", "params": []},
            {"slot": 1, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert len(out["target_capable_plugins"]) == 2
    # First one wins for the advisory text
    first = out["target_capable_plugins"][0]
    assert first["name"] == "Fruity Limiter"
    assert "Fruity Limiter" in out["advisory"]


def test_recognises_all_capable_plugin_names() -> None:
    """Maximus and the multiband comp should also count."""
    for plugin_name in ("Fruity Limiter", "Maximus", "Fruity Multiband Compressor"):
        fl = FakeFL({
            5: {"name": "Kick", "plugins": [], "routing": []},
            7: {"name": "Target", "plugins": [
                {"slot": 0, "name": plugin_name, "params": []},
            ], "routing": []},
        })
        out = _executor(fl).execute(
            "apply_sidechain", {"source_track": 5, "target_track": 7}
        )
        assert out["advisory_status"] == "ready_for_dropdown", plugin_name


# ───────────────────────────── No-comp path ───────────────────────────

def test_advisory_when_no_comp_loaded() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Parametric EQ 2", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["ok"] is True
    assert out["advisory_status"] == "needs_comp_loaded"
    assert out["target_capable_plugins"] == []
    assert "Fruity Compressor" in out["advisory"] or "Fruity Limiter" in out["advisory"]
    # Send is still created — the agent might be doing this in a "wire-up
    # before adding the comp" order
    assert len(fl.send_calls) == 1


def test_send_still_created_when_target_has_no_plugins_at_all() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["advisory_status"] == "needs_comp_loaded"
    assert len(fl.send_calls) == 1


# ───────────────────────────── Existing-route detection ───────────────

def test_detects_existing_route() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": [
            {"dest": 7, "dest_name": "Bass", "level": 0.8},
        ]},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["send_already_existed"] is True
    # Bridge call still happens (level may be updated), but the existing
    # routing was reported back to the caller honestly
    assert len(fl.send_calls) == 1


# ───────────────────────────── Custom send level ──────────────────────

def test_custom_send_level_passes_through() -> None:
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    out = _executor(fl).execute(
        "apply_sidechain",
        {"source_track": 5, "target_track": 7, "send_level": 0.5},
    )
    assert out["send_level"] == pytest.approx(0.5)
    assert fl.send_calls[0]["level"] == pytest.approx(0.5)


# ───────────────────────────── No workspace = no decision crash ───────

def test_no_workspace_does_not_crash_decision_log() -> None:
    """ToolExecutor without a workspace (bare-agent / tests) must still
    execute without trying to write decisions.json."""
    fl = FakeFL({
        5: {"name": "Kick", "plugins": [], "routing": []},
        7: {"name": "Bass", "plugins": [
            {"slot": 0, "name": "Fruity Compressor", "params": []},
        ], "routing": []},
    })
    # workspace=None is the bare-agent shape
    executor = ToolExecutor(fl=fl, workspace=None)
    out = executor.execute(
        "apply_sidechain", {"source_track": 5, "target_track": 7}
    )
    assert out["ok"] is True
