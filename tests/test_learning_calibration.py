"""Tests for the calibration probe layer.

The pure-math fitter is already covered by tests/test_learning_curves.py.
This file exercises the layer that drives FL + the readback provider:
classify, sweep, validate."""

from __future__ import annotations

from typing import Any

import math
import pytest

from studiomind.learning import calibration as cal
from studiomind.learning.calibration import (
    EnumeratedParam,
    Readback,
    ValidationProbeResult,
)
from studiomind.learning.curves import Fit


# ───────────────────────────── Fakes ──────────────────────────────────

class FakeFL:
    """Minimal FL stub: tracks param values per (track, slot, param_id)
    and exposes get_plugin_params + set_plugin_param."""

    def __init__(self, params_table: list[dict] | None = None) -> None:
        self.params_table = params_table or []
        self.set_calls: list[dict[str, Any]] = []
        self.values: dict[tuple[int, int, int], float] = {}

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        return {"params": self.params_table}

    def set_plugin_param(self, track_id: int, slot: int, param_id: int, value: float) -> dict:
        self.set_calls.append({
            "track_id": track_id, "slot": slot,
            "param_id": param_id, "value": value,
        })
        self.values[(track_id, slot, param_id)] = value
        return {"ok": True, "param_id": param_id, "new_value": value, "display": f"{value}"}


class CannedProvider:
    """Returns the next response from a list. Raises if exhausted."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def request(self, prompt: str, *, expected_unit: str = "") -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise RuntimeError("CannedProvider exhausted")
        return self._responses.pop(0)


def _no_sleep(_seconds: float) -> None:
    return None


# ───────────────────────────── parsing ────────────────────────────────

def test_parse_readback_pulls_numeric() -> None:
    assert cal.parse_readback("-12.0 dB") == -12.0
    assert cal.parse_readback("200 ms") == 200.0
    assert cal.parse_readback("0.4 : 1") == 0.4
    assert cal.parse_readback("1e-3") == 1e-3
    assert cal.parse_readback("  -60  ") == -60.0


def test_parse_readback_returns_none_for_strings() -> None:
    assert cal.parse_readback("smooth") is None
    assert cal.parse_readback("hard") is None
    assert cal.parse_readback("") is None
    assert cal.parse_readback("   ") is None


# ───────────────────────────── enumerate ──────────────────────────────

def test_enumerate_normalizes_param_rows() -> None:
    fl = FakeFL([
        {"id": 2, "name": "Threshold", "default_value": 0.8},
        {"id": 0, "name": "Used", "default_value": 1.0},
        {"id": 1, "name": "Enabled", "default_value": 1.0},
    ])
    out = cal.enumerate_plugin_params(fl, track_id=4, slot=0)
    assert [p.id for p in out] == [0, 1, 2]   # sorted by id
    assert isinstance(out[0], EnumeratedParam)
    assert out[2].name == "Threshold"
    assert out[2].default_value == 0.8


def test_enumerate_skips_malformed_rows() -> None:
    fl = FakeFL([
        {"id": 0, "name": "OK", "default_value": 0.0},
        {"id": "bad", "name": "X"},                   # bad type
        "not a dict",                                  # not a dict
        {"id": 1, "name": "Also OK", "default_value": "0.5"},
    ])
    out = cal.enumerate_plugin_params(fl, 0, 0)
    ids = [p.id for p in out]
    assert ids == [0, 1]


# ───────────────────────────── set_param_and_dwell ────────────────────

def test_set_param_and_dwell_drives_and_sleeps() -> None:
    fl = FakeFL()
    sleeps: list[float] = []
    cal.set_param_and_dwell(
        fl, track_id=4, slot=0, param_id=0, value=0.7,
        dwell_s=0.25, sleep=lambda s: sleeps.append(s),
    )
    assert fl.set_calls == [{
        "track_id": 4, "slot": 0, "param_id": 0, "value": 0.7,
    }]
    assert sleeps == [0.25]


def test_set_param_and_dwell_zero_dwell_skips_sleep() -> None:
    fl = FakeFL()
    sleeps: list[float] = []
    cal.set_param_and_dwell(
        fl, 0, 0, 0, 0.5, dwell_s=0.0,
        sleep=lambda s: sleeps.append(s),
    )
    assert sleeps == []


# ───────────────────────────── classify_param ─────────────────────────

def test_classify_continuous_with_distinct_floats() -> None:
    fl = FakeFL()
    provider = CannedProvider(["-30 dB", "-15 dB"])
    result = cal.classify_param(
        fl, 0, 0, 0,
        provider=provider,
        sleep=_no_sleep,
    )
    assert result.kind == "continuous"
    assert result.confident is True
    assert result.readback_a.parsed == -30.0
    assert result.readback_b.parsed == -15.0


def test_classify_enum_with_distinct_strings() -> None:
    fl = FakeFL()
    provider = CannedProvider(["hard", "smooth"])
    result = cal.classify_param(fl, 0, 0, 0, provider=provider, sleep=_no_sleep)
    assert result.kind == "enum"
    assert result.confident is True


def test_classify_ambiguous_when_floats_too_close() -> None:
    fl = FakeFL()
    provider = CannedProvider(["1.000 dB", "1.001 dB"])
    result = cal.classify_param(fl, 0, 0, 0, provider=provider, sleep=_no_sleep)
    assert result.kind == "ambiguous"
    assert result.confident is False


def test_classify_ambiguous_when_strings_identical() -> None:
    fl = FakeFL()
    provider = CannedProvider(["off", "off"])
    result = cal.classify_param(fl, 0, 0, 0, provider=provider, sleep=_no_sleep)
    assert result.kind == "ambiguous"
    assert result.confident is False


def test_classify_drives_fl_at_test_values() -> None:
    fl = FakeFL()
    provider = CannedProvider(["-30 dB", "-15 dB"])
    cal.classify_param(fl, 4, 0, 7, provider=provider, sleep=_no_sleep)
    assert [c["value"] for c in fl.set_calls] == list(cal.CLASSIFY_TEST_VALUES)
    assert all(c["param_id"] == 7 for c in fl.set_calls)


# ───────────────────────────── run_sweep ──────────────────────────────

def test_run_sweep_six_points_default() -> None:
    fl = FakeFL()
    provider = CannedProvider(["-60 dB", "-48 dB", "-36 dB", "-24 dB", "-12 dB", "0 dB"])
    out = cal.run_sweep(fl, 0, 0, 0, provider=provider, sleep=_no_sleep)
    assert [p for p, _ in out] == list(cal.DEFAULT_SWEEP_POINTS)
    assert [r.parsed for _, r in out] == [-60.0, -48.0, -36.0, -24.0, -12.0, 0.0]


def test_samples_from_sweep_skips_unparsed() -> None:
    sweep = [
        (0.0, Readback(raw="-60 dB", parsed=-60.0)),
        (0.5, Readback(raw="bogus", parsed=None)),    # skipped
        (1.0, Readback(raw="0 dB", parsed=0.0)),
    ]
    samples = cal.samples_from_sweep(sweep)
    assert len(samples) == 2


def test_fit_param_recovers_linear_curve() -> None:
    """Samples = a*p + b → linear fit recovered."""
    sweep = [
        (p, Readback(raw=f"{p*60-60}", parsed=p * 60 - 60))
        for p in cal.DEFAULT_SWEEP_POINTS
    ]
    samples = cal.samples_from_sweep(sweep)
    fit = cal.fit_param(samples)
    assert fit is not None
    assert fit.shape == "linear"
    assert fit.params[0] == pytest.approx(60.0, abs=1e-6)
    assert fit.params[1] == pytest.approx(-60.0, abs=1e-6)


# ───────────────────────────── validation probes ──────────────────────

def test_select_validation_probes_includes_extremities() -> None:
    fit = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    probes = cal.select_validation_probes(fit, runner_up=None)
    assert 0.05 in probes
    assert 0.95 in probes
    assert len(probes) == 4    # 2 extremity + 2 fallback midpoints


def test_select_validation_probes_picks_max_disagreement() -> None:
    """When two fits disagree most at the middle, the chosen probes
    cluster there."""
    a = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    b = Fit(shape="quadratic", params=(60.0, 0.0, -60.0), r_squared=0.99, rmse=0.0)
    # |a - b| = |60p - 60p^2| — peaks at p=0.5
    probes = cal.select_validation_probes(a, b, n_extremity=2, n_disagreement=2)
    # Confirm at least one disagreement probe is in the middle band.
    middle_band = [p for p in probes if 0.3 <= p <= 0.7]
    assert middle_band, f"expected a disagreement probe near 0.5; got {probes}"


def test_select_validation_probes_is_deterministic() -> None:
    a = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    b = Fit(shape="quadratic", params=(60.0, 0.0, -60.0), r_squared=0.99, rmse=0.0)
    probes_1 = cal.select_validation_probes(a, b)
    probes_2 = cal.select_validation_probes(a, b)
    assert probes_1 == probes_2


def test_validate_fit_passes_when_readbacks_match_prediction() -> None:
    fl = FakeFL()
    fit = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    # Use explicit probe points so we know the readback order.
    probe_points = [0.05, 0.95, 0.30, 0.70]
    expected = [60.0 * p - 60.0 for p in probe_points]
    provider = CannedProvider([f"{v:.4f} dB" for v in expected])

    outcome = cal.validate_fit(
        fl, 0, 0, 0,
        fit=fit, provider=provider,
        sleep=_no_sleep,
        probe_points=probe_points,
    )
    assert outcome.passed is True
    assert all(p.ok for p in outcome.probes)


def test_validate_fit_fails_when_readback_drifts() -> None:
    fl = FakeFL()
    fit = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    probe_points = [0.05, 0.95, 0.30, 0.70]
    # Inject a 5dB error on one probe — way beyond tolerance.
    expected = [60.0 * p - 60.0 for p in probe_points]
    expected[0] += 5.0
    provider = CannedProvider([f"{v:.4f} dB" for v in expected])
    outcome = cal.validate_fit(
        fl, 0, 0, 0,
        fit=fit, provider=provider,
        sleep=_no_sleep, probe_points=probe_points,
    )
    assert outcome.passed is False
    assert outcome.probes[0].ok is False
    assert all(p.ok for p in outcome.probes[1:])


def test_validate_fit_marks_unparsed_readback_as_failure() -> None:
    fl = FakeFL()
    fit = Fit(shape="linear", params=(60.0, -60.0), r_squared=1.0, rmse=0.0)
    probe_points = [0.05, 0.95]
    provider = CannedProvider(["bogus", "-3 dB"])
    outcome = cal.validate_fit(
        fl, 0, 0, 0,
        fit=fit, provider=provider,
        sleep=_no_sleep, probe_points=probe_points,
    )
    assert outcome.probes[0].actual is None
    assert outcome.probes[0].ok is False
    assert outcome.passed is False
