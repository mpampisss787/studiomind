"""Tests for the Fruity Compressor typed wrapper.

These tests cover the conversion math + ``build_compressor_commands``
behaviour. Round-trip tests assert that ``human_to_param → param_to_human``
returns the original within a small tolerance, which is what the agent
relies on when it reads back state via ``read_mixer_track`` and reasons
about whether to write or hold."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from studiomind.skills.fruity_compressor import wrapper as fc


# ───────────────────────────── Round-trip ─────────────────────────────

@pytest.mark.parametrize("db", [-60.0, -40.0, -20.0, -12.0, -6.0, 0.0])
def test_threshold_round_trip(db: float) -> None:
    p = fc.threshold_to_param(db)
    assert 0.0 <= p <= 1.0
    assert fc.param_to_threshold(p) == pytest.approx(db, abs=1e-6)


@pytest.mark.parametrize("ratio", [1.0, 1.5, 2.0, 4.0, 8.0, 11.3, 20.0, 30.0])
def test_ratio_round_trip(ratio: float) -> None:
    p = fc.ratio_to_param(ratio)
    assert 0.0 <= p <= 1.0
    assert fc.param_to_ratio(p) == pytest.approx(ratio, abs=1e-6)


@pytest.mark.parametrize("db", [-30.0, -20.0, -10.0, -3.0, 0.0, 3.0, 10.0, 20.0, 30.0])
def test_gain_round_trip(db: float) -> None:
    p = fc.gain_to_param(db)
    assert 0.0 <= p <= 1.0
    assert fc.param_to_gain(p) == pytest.approx(db, abs=1e-6)


@pytest.mark.parametrize("ms", [0.0, 1.0, 10.0, 50.0, 200.0, 400.0])
def test_attack_round_trip(ms: float) -> None:
    p = fc.attack_to_param(ms)
    assert 0.0 <= p <= 1.0
    assert fc.param_to_attack(p) == pytest.approx(ms, abs=1e-6)


@pytest.mark.parametrize("ms", [0.0, 50.0, 100.0, 500.0, 2000.0, 4000.0])
def test_release_round_trip(ms: float) -> None:
    p = fc.release_to_param(ms)
    assert 0.0 <= p <= 1.0
    assert fc.param_to_release(p) == pytest.approx(ms, abs=1e-6)


# ───────────────────────────── Anchor points ──────────────────────────

def test_gain_unity_at_param_half() -> None:
    """0 dB makeup gain MUST map to exactly param=0.5 (FL's documented default)."""
    assert fc.gain_to_param(0.0) == pytest.approx(0.5, abs=1e-9)
    assert fc.param_to_gain(0.5) == pytest.approx(0.0, abs=1e-9)


def test_threshold_max_at_param_one() -> None:
    """0 dB threshold (no compression engaged) maps to param=1.0."""
    assert fc.threshold_to_param(0.0) == pytest.approx(1.0, abs=1e-9)


def test_ratio_unity_near_param_zero() -> None:
    """1:1 ratio (no compression) maps just above 0 — FL's intercept is
    at ratio≈0.386 (sub-unity expansion territory we never expose), so
    1.0 lands at a small positive param value."""
    p = fc.ratio_to_param(1.0)
    assert 0.0 < p < 0.05
    assert fc.param_to_ratio(p) == pytest.approx(1.0, abs=1e-6)


def test_attack_linear_against_live_readback() -> None:
    """Live-FL readback (2026-04-29 calibration): param 0.5 -> 200 ms,
    param 0.25 -> 100 ms. This is the linear [0, 400] ms curve."""
    assert fc.param_to_attack(0.5) == pytest.approx(200.0, abs=1e-6)
    assert fc.param_to_attack(0.25) == pytest.approx(100.0, abs=1e-6)


def test_release_linear_against_live_readback() -> None:
    """Live-FL readback (2026-04-29 calibration): param 0.5145 -> 2058 ms,
    param 0.6221 -> 2489 ms. This is the linear [0, 4000] ms curve."""
    assert fc.param_to_release(0.5145) == pytest.approx(2058.0, abs=1.0)
    assert fc.param_to_release(0.6221) == pytest.approx(2488.0, abs=2.0)


def test_gain_range_is_thirty_db_each_way() -> None:
    """Live-FL readback (2026-04-29 calibration): param 0.5 -> 0 dB,
    param 0.525 -> 1.5 dB. Slope = 60 dB per unit param ⇒ [-30, +30]."""
    assert fc.param_to_gain(0.5) == pytest.approx(0.0, abs=1e-9)
    assert fc.param_to_gain(0.525) == pytest.approx(1.5, abs=1e-6)
    assert fc.param_to_gain(0.0) == pytest.approx(-30.0, abs=1e-9)
    assert fc.param_to_gain(1.0) == pytest.approx(30.0, abs=1e-9)


def test_ratio_linear_against_live_readback() -> None:
    """Live-FL six-point sweep (2026-04-29):
        param 0.0 -> 0.4,   0.2 -> 6.3,   0.4 -> 12.2,
        param 0.6 -> 18.2,  0.8 -> 24.1,  1.0 -> 30.0
    Least-squares fit: ratio = 0.386 + 29.629 * param. Tolerance 0.1
    absorbs FL's one-decimal display rounding."""
    assert fc.param_to_ratio(0.0) == pytest.approx(0.4, abs=0.1)
    assert fc.param_to_ratio(0.2) == pytest.approx(6.3, abs=0.1)
    assert fc.param_to_ratio(0.4) == pytest.approx(12.2, abs=0.1)
    assert fc.param_to_ratio(0.6) == pytest.approx(18.2, abs=0.1)
    assert fc.param_to_ratio(0.8) == pytest.approx(24.1, abs=0.1)
    assert fc.param_to_ratio(1.0) == pytest.approx(30.0, abs=0.1)


# ───────────────────────────── Clamping ───────────────────────────────

def test_threshold_clamps_above_zero() -> None:
    assert fc.threshold_to_param(50.0) == 1.0


def test_threshold_clamps_below_min() -> None:
    assert fc.threshold_to_param(-200.0) == 0.0


def test_ratio_clamps_below_one() -> None:
    """Sub-unity ratio (expansion) is clamped up to 1.0:1, which lands
    just above param=0 under FL's linear fit (intercept ≈ 0.4)."""
    assert fc.ratio_to_param(0.5) == fc.ratio_to_param(1.0)
    assert fc.ratio_to_param(0.5) < 0.05


def test_ratio_clamps_above_max() -> None:
    """1000:1 clamps to RATIO_MAX (30:1) and lands at the top of the param range."""
    assert fc.ratio_to_param(1000.0) == fc.ratio_to_param(fc.RATIO_MAX)
    assert fc.ratio_to_param(1000.0) > 0.99


def test_gain_clamps() -> None:
    assert fc.gain_to_param(50.0) == 1.0
    assert fc.gain_to_param(-50.0) == 0.0


def test_attack_clamps() -> None:
    assert fc.attack_to_param(-10.0) == 0.0
    assert fc.attack_to_param(1e6) == 1.0


def test_release_clamps() -> None:
    assert fc.release_to_param(-10.0) == 0.0
    assert fc.release_to_param(1e9) == 1.0


# ───────────────────────────── Knee enum ──────────────────────────────

def test_knee_hard() -> None:
    assert fc.knee_to_param("hard") == 0.0
    assert fc.knee_to_param("HARD") == 0.0
    assert fc.knee_to_param("  hard ") == 0.0


def test_knee_smooth() -> None:
    assert fc.knee_to_param("smooth") == 1.0


def test_knee_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Unknown knee"):
        fc.knee_to_param("medium")


def test_knee_round_trip() -> None:
    assert fc.param_to_knee(fc.knee_to_param("hard")) == "hard"
    assert fc.param_to_knee(fc.knee_to_param("smooth")) == "smooth"


def test_knee_decode_threshold() -> None:
    """Boundary of hard/smooth in param_to_knee is exactly 0.5."""
    assert fc.param_to_knee(0.499) == "hard"
    assert fc.param_to_knee(0.5) == "smooth"
    assert fc.param_to_knee(0.501) == "smooth"


# ───────────────────────────── build_compressor_commands ──────────────

def test_build_only_writes_passed_params() -> None:
    cmds = fc.build_compressor_commands(track_id=4, slot=0, threshold_db=-12.0)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd["track_id"] == 4
    assert cmd["slot"] == 0
    assert cmd["param_id"] == fc.PARAM_THRESHOLD
    assert cmd["value"] == pytest.approx(fc.threshold_to_param(-12.0))


def test_build_full_set() -> None:
    cmds = fc.build_compressor_commands(
        track_id=5,
        slot=2,
        threshold_db=-18.0,
        ratio=3.0,
        gain_db=2.0,
        attack_ms=10.0,
        release_ms=100.0,
        knee="smooth",
    )
    assert len(cmds) == 6
    by_id = {c["param_id"]: c for c in cmds}
    assert set(by_id.keys()) == {
        fc.PARAM_THRESHOLD,
        fc.PARAM_RATIO,
        fc.PARAM_GAIN,
        fc.PARAM_ATTACK,
        fc.PARAM_RELEASE,
        fc.PARAM_TYPE,
    }
    for cmd in cmds:
        assert cmd["track_id"] == 5
        assert cmd["slot"] == 2
        assert 0.0 <= cmd["value"] <= 1.0


def test_build_no_params() -> None:
    cmds = fc.build_compressor_commands(track_id=1, slot=0)
    assert cmds == []


def test_build_invalid_knee_raises() -> None:
    with pytest.raises(ValueError, match="Unknown knee"):
        fc.build_compressor_commands(track_id=1, slot=0, knee="medium")


# ───────────────────────────── decode_state ───────────────────────────

def test_decode_state_round_trip() -> None:
    """Encode a full set, then decode the resulting param dict — recovered
    human values should match the inputs."""
    cmds = fc.build_compressor_commands(
        track_id=0,
        slot=0,
        threshold_db=-15.0,
        ratio=4.0,
        gain_db=2.0,
        attack_ms=5.0,
        release_ms=120.0,
        knee="smooth",
    )
    param_values = {c["param_id"]: c["value"] for c in cmds}
    state = fc.decode_state(param_values)
    assert state.threshold_db == pytest.approx(-15.0, abs=1e-6)
    assert state.ratio == pytest.approx(4.0, abs=1e-6)
    assert state.gain_db == pytest.approx(2.0, abs=1e-6)
    assert state.attack_ms == pytest.approx(5.0, rel=1e-6)
    assert state.release_ms == pytest.approx(120.0, rel=1e-6)
    assert state.knee == "smooth"


def test_decode_state_summary() -> None:
    state = fc.CompressorState(
        threshold_db=-12.0,
        ratio=2.0,
        gain_db=1.0,
        attack_ms=10.0,
        release_ms=80.0,
        knee="hard",
    )
    s = state.summary()
    assert "thresh -12.0 dB" in s
    assert "ratio 2.0:1" in s
    assert "attack 10.0 ms" in s
    assert "release 80 ms" in s
    assert "gain +1.0 dB" in s
    assert "knee hard" in s


# ───────────────────────────── Param JSON consistency ─────────────────

def test_param_ids_match_enumerated_json() -> None:
    """The PARAM_* constants must agree with the IDs that the live-FL
    enumeration script wrote to fruity_compressor_params.json. If FL ever
    renumbers these, this test breaks loud and we re-enumerate."""
    json_path = Path(__file__).resolve().parent / "fruity_compressor_params.json"
    data = json.loads(json_path.read_text())
    by_name = {p["name"]: p["id"] for p in data["params"]}
    assert by_name["Threshold"] == fc.PARAM_THRESHOLD
    assert by_name["Ratio"] == fc.PARAM_RATIO
    assert by_name["Gain"] == fc.PARAM_GAIN
    assert by_name["Attack"] == fc.PARAM_ATTACK
    assert by_name["Release"] == fc.PARAM_RELEASE
    assert by_name["Type"] == fc.PARAM_TYPE
    assert data["num_params"] == fc.NUM_PARAMS


def test_default_values_round_trip_to_plausible_humans() -> None:
    """The factory defaults from the JSON should decode to sensible human
    values. If we ever change the calibration, this test catches whether
    the new mapping moved the defaults into nonsense territory."""
    json_path = Path(__file__).resolve().parent / "fruity_compressor_params.json"
    data = json.loads(json_path.read_text())
    defaults = {p["id"]: p["default_value"] for p in data["params"]}
    state = fc.decode_state(defaults)
    # Threshold default is exactly 0 dB (no compression)
    assert state.threshold_db == pytest.approx(0.0, abs=1e-3)
    # Gain default is exactly unity
    assert state.gain_db == pytest.approx(0.0, abs=1e-3)
    # Knee default is hard
    assert state.knee == "hard"
    # Ratio default decodes to ≈1:1 — FL's factory default is "no
    # compression engaged," confirmed by the live linear fit.
    assert state.ratio == pytest.approx(1.0, abs=0.05), f"ratio default decoded to {state.ratio}"
    # Attack default in "fast" range (< 50 ms)
    assert 0.1 <= state.attack_ms < 50.0
    # Release default in "moderate" range (1–500 ms)
    assert 1.0 <= state.release_ms < 500.0
