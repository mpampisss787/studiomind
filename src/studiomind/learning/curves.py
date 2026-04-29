"""Curve fitting for plugin parameter calibration.

Given a set of (normalized_param, displayed_value) samples from a live
plugin, fit each candidate shape and pick the simplest one that
matches the data with R² >= a configurable threshold (default 0.99).

Used by training mode to auto-generate plugin wrappers from sweep
readbacks. See docs/training-mode.md for the surrounding design.

Design properties (asserted by tests):

  * **Five candidate shapes**: linear, log, power, exp, quadratic.
  * **Deterministic**: identical samples always yield an identical Fit.
    No RNG in the fitter; nonlinear fits use fixed initial guesses.
  * **Occam tiebreak**: when two shapes' R² are within ``occam_eps``
    of the best, the simpler shape wins. Linear beats quadratic when
    both fit equally well — that was the lesson from Fruity
    Compressor's ratio mistake.
  * **Hard R² floor**: ``calibrate()`` returns ``None`` when no shape
    clears ``min_r_squared``. The caller (training agent) is expected
    to ask for more samples and re-fit; nothing ships under-fit.
  * **Closed-form where possible** (linear, log, quadratic): one-shot
    least squares via ``numpy.linalg.lstsq``. ``power`` and ``exp``
    use ``scipy.optimize.curve_fit`` with deterministic initial
    guesses derived from the data range.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
from scipy.optimize import OptimizeWarning, curve_fit

ShapeName = Literal["linear", "log", "power", "exp", "quadratic"]
ALL_SHAPES: tuple[ShapeName, ...] = ("linear", "log", "power", "exp", "quadratic")

# Small offsets so log(0) and (0)^b don't blow up. Param values live in
# [0, 1] so a fixed 0.01 shift is essentially invisible to the curve
# shape but keeps the math finite at the lower bound.
_LOG_EPS: float = 0.01
_POWER_EPS: float = 0.01

_PARAM_COUNT: dict[ShapeName, int] = {
    "linear": 2,
    "log": 2,
    "quadratic": 3,
    "power": 3,
    "exp": 3,
}


# ───────────────────────── data classes ─────────────────────────

@dataclass(frozen=True)
class Sample:
    """One (param, displayed) pair from a sweep readback."""

    param: float       # the normalized value we sent (0.0–1.0)
    displayed: float   # what FL displayed in human units


@dataclass(frozen=True)
class Fit:
    """A fitted shape with its goodness-of-fit metrics."""

    shape: ShapeName
    params: tuple[float, ...]   # shape-specific (see _eval_*)
    r_squared: float
    rmse: float

    def param_count(self) -> int:
        return _PARAM_COUNT[self.shape]


@dataclass(frozen=True)
class CalibrationResult:
    """Output of a successful end-to-end fit."""

    selected: Fit
    all_fits: tuple[Fit, ...]   # every shape that fit, sorted (simplest first, then highest R²)


# ───────────────────────── shape evaluators ─────────────────────────
# Param order in tuples: see each function.

def _eval_linear(p: float, a: float, b: float) -> float:
    """v = a*p + b   (params: slope, intercept)"""
    return a * p + b


def _eval_log(p: float, a: float, b: float) -> float:
    """v = a*ln(p+ε) + b   (params: scale, offset)"""
    return a * math.log(p + _LOG_EPS) + b


def _eval_power(p: float, a: float, b: float, c: float) -> float:
    """v = a*(p+ε)^b + c   (params: scale, exponent, offset)"""
    return a * ((p + _POWER_EPS) ** b) + c


def _eval_exp(p: float, a: float, b: float, c: float) -> float:
    """v = a*exp(b*p) + c   (params: scale, rate, offset)"""
    return a * math.exp(b * p) + c


def _eval_quadratic(p: float, a: float, b: float, c: float) -> float:
    """v = a*p^2 + b*p + c   (params: a, b, c)"""
    return a * p * p + b * p + c


_EVALUATORS = {
    "linear": _eval_linear,
    "log": _eval_log,
    "power": _eval_power,
    "exp": _eval_exp,
    "quadratic": _eval_quadratic,
}


def predict(fit: Fit, p: float) -> float:
    """Apply ``fit`` to a param value and return the predicted display."""
    return _EVALUATORS[fit.shape](p, *fit.params)


def invert(fit: Fit, displayed: float) -> float | None:
    """Find the param value whose prediction matches ``displayed``.

    Closed-form for every shape. Returns ``None`` if ``displayed`` is
    unreachable under this fit (e.g. saturated, wrong sign, multiple
    valid roots in `quadratic` outside `[0, 1]`)."""
    if fit.shape == "linear":
        a, b = fit.params
        if a == 0:
            return None
        return (displayed - b) / a

    if fit.shape == "log":
        a, b = fit.params
        if a == 0:
            return None
        try:
            return math.exp((displayed - b) / a) - _LOG_EPS
        except OverflowError:
            return None

    if fit.shape == "quadratic":
        a, b, c = fit.params
        if a == 0:
            if b == 0:
                return None
            return (displayed - c) / b
        disc = b * b - 4 * a * (c - displayed)
        if disc < 0:
            return None
        sqrt_disc = math.sqrt(disc)
        roots = [(-b + sqrt_disc) / (2 * a), (-b - sqrt_disc) / (2 * a)]
        # Prefer the root inside [0, 1]; tiebreak by closeness to 0.5.
        in_range = [r for r in roots if -1e-4 <= r <= 1 + 1e-4]
        if not in_range:
            return None
        return min(in_range, key=lambda r: abs(r - 0.5))

    if fit.shape == "power":
        a, b, c = fit.params
        if a == 0 or b == 0:
            return None
        rhs = (displayed - c) / a
        if rhs <= 0:
            return None
        try:
            return rhs ** (1.0 / b) - _POWER_EPS
        except (ValueError, OverflowError, ZeroDivisionError):
            return None

    if fit.shape == "exp":
        a, b, c = fit.params
        if a == 0 or b == 0:
            return None
        rhs = (displayed - c) / a
        if rhs <= 0:
            return None
        try:
            return math.log(rhs) / b
        except (ValueError, OverflowError):
            return None

    return None


# ───────────────────────── fitting ─────────────────────────

def _r_squared_and_rmse(samples: Sequence[Sample], shape: ShapeName, params: tuple[float, ...]) -> tuple[float, float]:
    eval_fn = _EVALUATORS[shape]
    y_actual = np.array([s.displayed for s in samples], dtype=float)
    y_pred = np.array([eval_fn(s.param, *params) for s in samples], dtype=float)
    residuals = y_actual - y_pred
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((y_actual - np.mean(y_actual)) ** 2))
    rmse = math.sqrt(ss_res / len(samples)) if len(samples) else float("inf")
    if ss_tot == 0:
        # All displayed values identical: R²=1 if predictions match exactly.
        r2 = 1.0 if ss_res < 1e-12 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return r2, rmse


def fit_shape(shape: ShapeName, samples: Sequence[Sample]) -> Fit | None:
    """Fit a single candidate shape. Returns None on insufficient data
    or numerical failure."""
    n = len(samples)
    needed = _PARAM_COUNT[shape] + 1   # at least one DoF for residuals
    if n < needed:
        return None

    p_arr = np.array([s.param for s in samples], dtype=float)
    v_arr = np.array([s.displayed for s in samples], dtype=float)
    params: tuple[float, ...]

    try:
        if shape == "linear":
            A = np.column_stack([p_arr, np.ones_like(p_arr)])
            soln, *_ = np.linalg.lstsq(A, v_arr, rcond=None)
            params = (float(soln[0]), float(soln[1]))
        elif shape == "log":
            A = np.column_stack([np.log(p_arr + _LOG_EPS), np.ones_like(p_arr)])
            soln, *_ = np.linalg.lstsq(A, v_arr, rcond=None)
            params = (float(soln[0]), float(soln[1]))
        elif shape == "quadratic":
            A = np.column_stack([p_arr ** 2, p_arr, np.ones_like(p_arr)])
            soln, *_ = np.linalg.lstsq(A, v_arr, rcond=None)
            params = (float(soln[0]), float(soln[1]), float(soln[2]))
        elif shape == "power":
            v_range = max(float(v_arr.max() - v_arr.min()), 1e-9)
            p0 = (v_range, 1.0, float(v_arr.min()))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", OptimizeWarning)
                popt, _ = curve_fit(
                    lambda p, a, b, c: a * np.power(p + _POWER_EPS, b) + c,
                    p_arr, v_arr, p0=p0, maxfev=5000,
                )
            params = (float(popt[0]), float(popt[1]), float(popt[2]))
        elif shape == "exp":
            v_range = max(float(v_arr.max() - v_arr.min()), 1e-9)
            p0 = (v_range, 1.0, float(v_arr.min()))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", OptimizeWarning)
                popt, _ = curve_fit(
                    lambda p, a, b, c: a * np.exp(b * p) + c,
                    p_arr, v_arr, p0=p0, maxfev=5000,
                )
            params = (float(popt[0]), float(popt[1]), float(popt[2]))
        else:
            return None
    except (np.linalg.LinAlgError, RuntimeError, ValueError, OverflowError, FloatingPointError):
        return None

    r2, rmse = _r_squared_and_rmse(samples, shape, params)
    if not (math.isfinite(r2) and math.isfinite(rmse)):
        return None
    return Fit(shape=shape, params=params, r_squared=r2, rmse=rmse)


def fit_all(samples: Sequence[Sample]) -> tuple[Fit, ...]:
    """Try every candidate shape; return the ones that succeeded.

    Result is sorted by ``(param_count ASC, -r_squared ASC, shape ASC)``
    for deterministic ordering."""
    out: list[Fit] = []
    for shape in ALL_SHAPES:
        f = fit_shape(shape, samples)
        if f is not None:
            out.append(f)
    return tuple(sorted(out, key=lambda f: (f.param_count(), -f.r_squared, f.shape)))


def pick_best(
    fits: Sequence[Fit],
    *,
    min_r_squared: float = 0.99,
    occam_eps: float = 0.001,
) -> Fit | None:
    """Pick the simplest fit whose R² is within ``occam_eps`` of the
    best. Returns None if no fit clears ``min_r_squared``."""
    if not fits:
        return None
    qualifying = [f for f in fits if f.r_squared >= min_r_squared]
    if not qualifying:
        return None
    best_r2 = max(f.r_squared for f in qualifying)
    near_best = [f for f in qualifying if best_r2 - f.r_squared <= occam_eps]
    near_best.sort(key=lambda f: (f.param_count(), -f.r_squared, f.shape))
    return near_best[0]


def calibrate(
    samples: Sequence[Sample],
    *,
    min_r_squared: float = 0.99,
    occam_eps: float = 0.001,
) -> CalibrationResult | None:
    """End-to-end: fit every shape, pick the best, return the result.

    Returns None when the data is too noisy or sparse for any shape to
    clear ``min_r_squared`` — the training agent should ask for more
    samples in that case."""
    fits = fit_all(samples)
    selected = pick_best(fits, min_r_squared=min_r_squared, occam_eps=occam_eps)
    if selected is None:
        return None
    return CalibrationResult(selected=selected, all_fits=fits)


# ───────────────────────── validation probe selection ─────────────────────────

# Used by the training agent's validation gate. See docs/training-mode.md
# § "Validation gate — four smart probes".

def validation_probe_points(
    result: CalibrationResult,
    *,
    n_extremity: int = 2,
    n_disagreement: int = 2,
) -> tuple[float, ...]:
    """Return the param values that should be probed against live FL
    after a fit, before declaring the wrapper acceptable.

    - Up to ``n_extremity`` probes near the param-domain extremes
      (deterministic offsets 0.05, 0.95, then 0.10/0.90, ...).
    - Up to ``n_disagreement`` probes at the param values where the
      selected fit and the runner-up fit disagree most. Catches
      family-overfit (e.g. quadratic vs linear when both have R²≈1
      on six points).

    All probe points are deterministic given the same CalibrationResult."""
    extremity_offsets = [0.05, 0.95, 0.10, 0.90, 0.15, 0.85]
    points: list[float] = list(extremity_offsets[:n_extremity])

    if n_disagreement <= 0 or len(result.all_fits) < 2:
        return tuple(points)

    # The runner-up: highest R² that isn't the selected one. Among ties,
    # alphabetical to stay deterministic.
    others = [f for f in result.all_fits if f is not result.selected]
    if not others:
        return tuple(points)
    runner_up = max(others, key=lambda f: (f.r_squared, -f.param_count()))

    # Sample 1001 points across [0, 1]; pick the points with the
    # largest |selected.predict - runner_up.predict|.
    grid = np.linspace(0.0, 1.0, 1001)
    deltas = np.array([
        abs(predict(result.selected, float(p)) - predict(runner_up, float(p)))
        for p in grid
    ])

    # Skip already-chosen extremity points (within ±0.005).
    excluded = set()
    for chosen in points:
        idx = int(round(chosen * 1000))
        for k in range(max(0, idx - 5), min(1001, idx + 6)):
            excluded.add(k)

    # Pick top-n disagreement points with min spacing of 0.05 between
    # them so we don't sample the same neighborhood twice.
    sorted_idx = np.argsort(-deltas)
    picked: list[int] = []
    for i in sorted_idx:
        i = int(i)
        if i in excluded:
            continue
        if any(abs(i - j) < 50 for j in picked):
            continue
        picked.append(i)
        if len(picked) >= n_disagreement:
            break

    points.extend(round(float(grid[i]), 4) for i in picked)
    return tuple(points)
