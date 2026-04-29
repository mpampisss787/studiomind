"""Tests for the FabFilter Pro-Q 3 skill.

Anchors the wrapper's conversion math (Hz/dB/Q ↔ normalized) and the
build_eq_commands shape so the agent's tool dispatch keeps producing
valid SysEx command lists. These tests were not present in the
hand-written era; they were added as part of the P3 skill migration
so the skill carries its own verification."""

from __future__ import annotations

import math

import pytest

from studiomind.skills.fabfilter_proq3 import wrapper as fabfilter_proq3
from studiomind.skills.fabfilter_proq3 import tool as proq3_tool


# ─────────────────── frequency mapping ───────────────────

def test_freq_round_trip_at_anchors() -> None:
    for f in (10.0, 100.0, 440.0, 1000.0, 5000.0, 20000.0, 30000.0):
        norm = fabfilter_proq3.freq_to_param(f)
        assert 0.0 <= norm <= 1.0
        assert fabfilter_proq3.param_to_freq(norm) == pytest.approx(f, rel=1e-4)


def test_freq_min_max_clamp() -> None:
    assert fabfilter_proq3.freq_to_param(1.0) == 0.0
    assert fabfilter_proq3.freq_to_param(1e9) == 1.0


# ─────────────────── gain mapping ───────────────────

@pytest.mark.parametrize("db", [-30.0, -12.0, -6.0, 0.0, 6.0, 12.0, 30.0])
def test_gain_round_trip(db: float) -> None:
    norm = fabfilter_proq3.gain_to_param(db)
    assert 0.0 <= norm <= 1.0
    assert fabfilter_proq3.param_to_gain(norm) == pytest.approx(db, abs=1e-6)


def test_gain_unity_at_param_half() -> None:
    assert fabfilter_proq3.gain_to_param(0.0) == pytest.approx(0.5, abs=1e-9)


# ─────────────────── Q mapping ───────────────────

@pytest.mark.parametrize("q", [0.025, 0.1, 0.5, 1.0, 4.0, 10.0, 40.0])
def test_q_round_trip(q: float) -> None:
    norm = fabfilter_proq3.q_to_param(q)
    assert 0.0 <= norm <= 1.0
    assert fabfilter_proq3.param_to_q(norm) == pytest.approx(q, rel=1e-4)


def test_q_unity_at_param_half() -> None:
    assert fabfilter_proq3.q_to_param(1.0) == pytest.approx(0.5, abs=1e-3)


# ─────────────────── command builder ───────────────────

def test_build_commands_uses_band_index_offsets() -> None:
    cmds = fabfilter_proq3.build_eq_commands(
        track_id=4, slot=0, band=3,
        frequency_hz=1000, gain_db=-3, q=2,
    )
    # Expect at least: used + enabled + freq + gain + q. Band 3 is
    # 1-based, so its base param ID is (3-1)*13 = 26.
    pids = {c["param_id"] for c in cmds}
    base = (3 - 1) * fabfilter_proq3.BAND_STRIDE
    assert base + fabfilter_proq3.OFF_USED in pids
    assert base + fabfilter_proq3.OFF_FREQ in pids
    assert base + fabfilter_proq3.OFF_GAIN in pids
    assert base + fabfilter_proq3.OFF_Q in pids
    for c in cmds:
        assert c["track_id"] == 4
        assert c["slot"] == 0
        assert 0.0 <= c["value"] <= 1.0


def test_build_commands_omits_unset_args() -> None:
    cmds = fabfilter_proq3.build_eq_commands(track_id=4, slot=0, band=1, gain_db=-3)
    # gain_db given → gain command present; no freq command since not given
    base = 0
    pids = {c["param_id"] for c in cmds}
    assert base + fabfilter_proq3.OFF_GAIN in pids
    assert base + fabfilter_proq3.OFF_FREQ not in pids


def test_band_param_id_helper() -> None:
    assert fabfilter_proq3.band_param_id(1, fabfilter_proq3.OFF_FREQ) == 0 + fabfilter_proq3.OFF_FREQ
    assert fabfilter_proq3.band_param_id(2, fabfilter_proq3.OFF_FREQ) == 13 + fabfilter_proq3.OFF_FREQ
    assert fabfilter_proq3.band_param_id(10, fabfilter_proq3.OFF_FREQ) == 9 * 13 + fabfilter_proq3.OFF_FREQ


# ─────────────────── tool spec ───────────────────

def test_tool_spec_well_formed() -> None:
    assert proq3_tool.TOOL["name"] == "set_proq3"
    schema = proq3_tool.TOOL["input_schema"]
    assert schema["type"] == "object"
    for required in ("track_id", "slot", "band"):
        assert required in schema["required"]


def test_build_commands_from_args_round_trip() -> None:
    cmds = proq3_tool.build_commands_from_args({
        "track_id": 4, "slot": 0, "band": 1, "gain_db": -2.5,
    })
    assert len(cmds) >= 2
    assert all("param_id" in c for c in cmds)
