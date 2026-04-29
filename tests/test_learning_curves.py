"""Tests for the multi-shape curve fitter used by training mode.

Anchors the design properties from docs/training-mode.md:
- Each candidate shape is recovered from synthetic data with R² ≈ 1
- R² gating refuses calibration when no shape clears the floor
- Occam tiebreak: when linear fits as well as quadratic, linear wins
- Determinism: identical input → identical Fit, byte-for-byte
- Predict / invert round-trip
- Real-data sanity check against the Fruity Compressor 2026-04-29 sweep
- Validation probe points are deterministic and avoid duplicates
"""

from __future__ import annotations

import math

import pytest

from studiomind.learning import curves as cv
from studiomind.learning.curves import Sample


# ─────────────────────── helpers ───────────────────────

def _sweep_six(
    fn,
    points: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
) -> tuple[Sample, ...]:
    """Build a six-sample sweep by evaluating fn at the canonical points."""
    return tuple(Sample(param=p, displayed=fn(p)) for p in points)


# ─────────────────────── shape recovery ───────────────────────

def test_linear_recovered_exactly() -> None:
    samples = _sweep_six(lambda p: 0.386 + 29.629 * p)  # the Fruity Comp ratio fit
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "linear"
    assert result.selected.r_squared == pytest.approx(1.0, abs=1e-9)
    a, b = result.selected.params
    assert a == pytest.approx(29.629, abs=1e-6)
    assert b == pytest.approx(0.386, abs=1e-6)


def test_log_recovered_when_data_is_log() -> None:
    samples = _sweep_six(lambda p: 5.0 * math.log(p + cv._LOG_EPS) + 12.0)
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "log"
    assert result.selected.r_squared > 0.9999
    a, b = result.selected.params
    assert a == pytest.approx(5.0, abs=1e-6)
    assert b == pytest.approx(12.0, abs=1e-6)


def test_quadratic_recovered_when_only_quadratic_fits() -> None:
    # A genuinely quadratic curve that linear cannot model. Use a
    # sharp curvature so R²(linear) drops well below 0.99.
    samples = _sweep_six(lambda p: 100 * (p - 0.5) ** 2)  # vertex at 0.5
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "quadratic"
    a, b, c = result.selected.params
    assert a == pytest.approx(100.0, abs=1e-6)
    assert b == pytest.approx(-100.0, abs=1e-6)
    assert c == pytest.approx(25.0, abs=1e-6)


def test_exponential_recovered_when_data_is_exp() -> None:
    # 7 samples so exp's 3 params have headroom.
    samples = tuple(
        Sample(param=p, displayed=2.5 * math.exp(3.0 * p) + 1.0)
        for p in (0.0, 1/6, 2/6, 0.5, 4/6, 5/6, 1.0)
    )
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "exp"
    a, b, c = result.selected.params
    assert a == pytest.approx(2.5, rel=1e-4)
    assert b == pytest.approx(3.0, rel=1e-4)
    assert c == pytest.approx(1.0, abs=1e-3)


def test_power_recovered_when_data_is_power() -> None:
    samples = tuple(
        Sample(param=p, displayed=4.0 * (p + cv._POWER_EPS) ** 0.7 + 0.5)
        for p in (0.0, 1/6, 2/6, 0.5, 4/6, 5/6, 1.0)
    )
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "power"
    a, b, c = result.selected.params
    assert a == pytest.approx(4.0, rel=1e-3)
    assert b == pytest.approx(0.7, rel=1e-3)
    assert c == pytest.approx(0.5, abs=1e-2)


# ─────────────────────── Occam tiebreak ───────────────────────

def test_occam_prefers_linear_over_quadratic_on_linear_data() -> None:
    """If linear fits perfectly, quadratic also fits perfectly (a=0).
    Occam: prefer linear (2 params over 3)."""
    samples = _sweep_six(lambda p: 10 * p + 5)
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "linear"
    # Quadratic should also be in all_fits with very high R²
    assert any(f.shape == "quadratic" and f.r_squared > 0.999 for f in result.all_fits)


def test_occam_prefers_log_over_power_when_both_clear_threshold() -> None:
    """log has 2 params, power has 3. If both R² are near 1 and within
    occam_eps, log should win."""
    samples = _sweep_six(lambda p: 5.0 * math.log(p + cv._LOG_EPS) + 12.0)
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "log"
    assert result.selected.param_count() == 2


# ─────────────────────── R² gating ───────────────────────

def test_calibrate_returns_none_when_data_is_unfittable() -> None:
    """Pure noise — no shape clears 0.99."""
    # Pseudo-noise via a deterministic chaotic function. None of our
    # shapes can describe a logistic-map orbit on six samples.
    def chaos(p: float) -> float:
        x = (p + 0.123) % 1.0
        for _ in range(12):
            x = 3.97 * x * (1 - x)
        return x

    samples = _sweep_six(chaos)
    result = cv.calibrate(samples)
    assert result is None


def test_calibrate_returns_none_when_all_fits_below_threshold() -> None:
    """Below the 0.99 floor → reject."""
    # Six samples that no smooth curve can fit cleanly.
    samples = (
        Sample(0.0, 0.0),
        Sample(0.2, 5.0),
        Sample(0.4, -3.0),
        Sample(0.6, 8.0),
        Sample(0.8, -1.0),
        Sample(1.0, 4.0),
    )
    assert cv.calibrate(samples) is None


def test_calibrate_passes_with_lower_threshold_when_caller_allows() -> None:
    samples = (
        Sample(0.0, 0.0),
        Sample(0.2, 5.0),
        Sample(0.4, -3.0),
        Sample(0.6, 8.0),
        Sample(0.8, -1.0),
        Sample(1.0, 4.0),
    )
    # With a permissive threshold the fitter still picks something.
    relaxed = cv.calibrate(samples, min_r_squared=0.0)
    assert relaxed is not None


# ─────────────────────── insufficient samples ───────────────────────

def test_two_samples_only_fits_two_param_shapes() -> None:
    samples = (Sample(0.0, 0.0), Sample(1.0, 10.0))
    fits = cv.fit_all(samples)
    # 3-param shapes need at least 4 samples; 2-param shapes need 3
    # (n_params + 1). Both linear and log should refuse here.
    assert all(f.shape not in ("linear", "log", "power", "exp", "quadratic") for f in fits)
    assert fits == ()


def test_three_samples_fits_two_param_shapes_only() -> None:
    samples = (Sample(0.0, 0.0), Sample(0.5, 5.0), Sample(1.0, 10.0))
    fits = cv.fit_all(samples)
    shapes = {f.shape for f in fits}
    assert "linear" in shapes
    assert "log" in shapes
    # quadratic / power / exp need at least 4 samples
    assert "quadratic" not in shapes
    assert "power" not in shapes
    assert "exp" not in shapes


def test_four_samples_unlocks_quadratic() -> None:
    samples = (
        Sample(0.0, 0.0),
        Sample(0.33, 1.0),
        Sample(0.67, 4.0),
        Sample(1.0, 9.0),
    )
    fits = cv.fit_all(samples)
    shapes = {f.shape for f in fits}
    assert "quadratic" in shapes


# ─────────────────────── determinism ───────────────────────

def test_identical_input_yields_byte_identical_fit() -> None:
    samples = _sweep_six(lambda p: 0.386 + 29.629 * p)
    a = cv.calibrate(samples)
    b = cv.calibrate(samples)
    assert a is not None and b is not None
    assert a.selected == b.selected
    assert a.all_fits == b.all_fits


def test_determinism_across_nonlinear_shapes() -> None:
    """scipy.optimize.curve_fit can be sensitive to RNG state in some
    versions. Verify our fixed-p0 fits are stable across calls."""
    samples = tuple(
        Sample(param=p, displayed=2.5 * math.exp(3.0 * p) + 1.0)
        for p in (0.0, 1/6, 2/6, 0.5, 4/6, 5/6, 1.0)
    )
    a = cv.fit_shape("exp", samples)
    b = cv.fit_shape("exp", samples)
    assert a is not None and b is not None
    assert a.params == b.params
    assert a.r_squared == b.r_squared


# ─────────────────────── predict / invert round-trip ───────────────────────

@pytest.mark.parametrize("shape,fn,sample_points", [
    ("linear",    lambda p: 30.0 * p - 5.0,                            (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)),
    ("log",       lambda p: 5.0 * math.log(p + cv._LOG_EPS) + 12.0,    (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)),
    # Monotonically increasing on [0,1] (vertex at p=-0.15 is outside).
    ("quadratic", lambda p: 100 * p * p + 30 * p + 5,                  (0.0, 0.15, 0.35, 0.6, 0.8, 1.0)),
])
def test_predict_invert_round_trip_closed_form_shapes(shape, fn, sample_points) -> None:
    samples = _sweep_six(fn, sample_points)
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == shape
    for p in (0.07, 0.23, 0.5, 0.81):
        displayed = cv.predict(result.selected, p)
        recovered = cv.invert(result.selected, displayed)
        assert recovered is not None
        assert recovered == pytest.approx(p, abs=1e-6)


def test_invert_returns_none_for_unreachable_value() -> None:
    fit = cv.Fit(shape="linear", params=(10.0, 0.0), r_squared=1.0, rmse=0.0)
    assert cv.invert(fit, 5.0) == 0.5
    # Linear is fully invertible for any displayed; pathological case
    # is a=0:
    flat = cv.Fit(shape="linear", params=(0.0, 7.0), r_squared=1.0, rmse=0.0)
    assert cv.invert(flat, 7.0) is None  # a=0 means no slope

    # Power with rhs <= 0 → unreachable
    pow_fit = cv.Fit(shape="power", params=(2.0, 0.5, 1.0), r_squared=1.0, rmse=0.0)
    assert cv.invert(pow_fit, 0.0) is None


# ─────────────────────── real-data anchor ───────────────────────

def test_fruity_compressor_ratio_sweep_2026_04_29() -> None:
    """Anchor against the actual sweep we did today on Fruity Comp's
    ratio knob. Six readings, perfect linear fit."""
    samples = (
        Sample(0.0, 0.4),
        Sample(0.2, 6.3),
        Sample(0.4, 12.2),
        Sample(0.6, 18.2),
        Sample(0.8, 24.1),
        Sample(1.0, 30.0),
    )
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "linear"
    assert result.selected.r_squared > 0.9999
    a, b = result.selected.params
    # Matches the fit we committed in 69b9280
    assert a == pytest.approx(29.629, abs=0.01)
    assert b == pytest.approx(0.386, abs=0.01)


def test_fruity_compressor_threshold_sweep_synthetic() -> None:
    """Recompute the threshold curve from synthetic samples that match
    today's Run A + Run B (param 0.5 → -30 dB, param 0.8 → -12 dB).
    Should land linear [-60, 0]."""
    samples = _sweep_six(lambda p: -60.0 + 60.0 * p)
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "linear"
    a, b = result.selected.params
    assert a == pytest.approx(60.0, abs=1e-6)
    assert b == pytest.approx(-60.0, abs=1e-6)


# ─────────────────────── validation probes ───────────────────────

def test_validation_probes_are_deterministic() -> None:
    samples = _sweep_six(lambda p: 0.386 + 29.629 * p)
    r1 = cv.calibrate(samples)
    r2 = cv.calibrate(samples)
    assert r1 is not None and r2 is not None
    assert cv.validation_probe_points(r1) == cv.validation_probe_points(r2)


def test_validation_probes_include_extremities() -> None:
    samples = _sweep_six(lambda p: 30.0 * p)
    result = cv.calibrate(samples)
    assert result is not None
    points = cv.validation_probe_points(result)
    # First two should be the extremities
    assert points[0] == 0.05
    assert points[1] == 0.95


def test_validation_probes_pick_disagreement_points_when_runners_up_exist() -> None:
    """When linear and quadratic both fit, the disagreement points
    should target where their predictions diverge most."""
    # Construct data that linear fits well but quadratic fits a hair
    # better — both should be in all_fits, picking points where they
    # disagree should not collapse to the same value.
    samples = _sweep_six(lambda p: 30.0 * p + 0.5 * p * p)
    result = cv.calibrate(samples)
    assert result is not None
    points = cv.validation_probe_points(result, n_disagreement=2)
    # Got 2 extremities + up to 2 disagreement
    assert len(points) >= 2
    # All probe points are in [0, 1]
    for pt in points:
        assert 0.0 <= pt <= 1.0


def test_validation_probes_dedupe_against_extremities() -> None:
    samples = _sweep_six(lambda p: 30.0 * p)
    result = cv.calibrate(samples)
    assert result is not None
    points = cv.validation_probe_points(result)
    # No two probe points within 0.05 of each other
    for i, pi in enumerate(points):
        for pj in points[i + 1:]:
            assert abs(pi - pj) >= 0.04, f"too close: {pi} {pj}"


# ─────────────────────── error / nonsense input ───────────────────────

def test_empty_samples_returns_none() -> None:
    assert cv.calibrate(()) is None


def test_constant_displayed_value_returns_perfect_fit() -> None:
    """All samples display the same value — linear fits with slope=0."""
    samples = tuple(Sample(param=p, displayed=42.0) for p in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
    result = cv.calibrate(samples)
    assert result is not None
    assert result.selected.shape == "linear"
    a, b = result.selected.params
    assert a == pytest.approx(0.0, abs=1e-9)
    assert b == pytest.approx(42.0, abs=1e-9)
