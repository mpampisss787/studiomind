"""Calibration probes for the training agent.

Higher-level than ``curves.py`` (which is the pure math) — these
functions drive the FL bridge and the user-readback provider through
the wizard's calibration steps. They are pure of LLM interaction:
the training agent loop wraps them as Anthropic tools.

The user-input dependency is abstracted as a ``ReadbackProvider``
callable so tests can inject canned responses.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from studiomind.learning.curves import (
    Fit,
    Sample,
    calibrate as curve_calibrate,
    predict,
)

log = logging.getLogger(__name__)


# ───────────────────────────── public types ───────────────────────────

@dataclass(frozen=True)
class EnumeratedParam:
    """One row from enumerate_plugin_params — the FL-reported params
    list with name, id, default value."""
    id: int
    name: str
    default_value: float


@dataclass(frozen=True)
class Readback:
    """One user-supplied readback. ``raw`` is the literal string the
    user typed (or that the mock provider returned); ``parsed`` is the
    extracted numeric, or None if it didn't parse."""
    raw: str
    parsed: float | None


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classify_param. ``kind`` is the heuristic's verdict;
    ``confident`` is False when the agent should ask the user to
    confirm before sweeping."""
    kind: str       # "continuous" | "enum" | "ambiguous"
    confident: bool
    readback_a: Readback
    readback_b: Readback
    note: str = ""


@dataclass(frozen=True)
class ReadbackContext:
    """Structured metadata for a readback prompt.

    Providers that surface readbacks to a UI (the websocket provider)
    use these fields to render a richer prompt — e.g. "Sweeping
    THRESHOLD step 4/6 (param=0.60)" — instead of the raw prompt
    string. CLI / canned providers ignore the context entirely; the
    field on the protocol has a default of None so existing impls
    don't break.
    """
    phase: str            # "classify" | "sweep" | "validate"
    param_id: int
    param_name: str = ""
    step: str = ""        # "4/6" or "1/2"
    param_value: float = 0.0
    expected_unit: str = ""


@dataclass(frozen=True)
class ValidationProbeResult:
    param_value: float
    predicted: float
    actual_raw: str
    actual: float | None
    delta: float | None
    ok: bool
    tolerance: float


@dataclass(frozen=True)
class ValidationOutcome:
    fit: Fit
    probes: tuple[ValidationProbeResult, ...]
    passed: bool


class ReadbackProvider(Protocol):
    """Pluggable user-input mechanism. Implementations:
      * stdin in a CLI subcommand
      * /ws/training in the web UI (P5)
      * canned-list in tests

    The ``context`` kwarg is optional structured metadata about the
    prompt — providers that surface readbacks to a UI can render a
    richer prompt from it. CLI / canned providers ignore it.
    """

    def request(
        self,
        prompt: str,
        *,
        expected_unit: str = "",
        context: "ReadbackContext | None" = None,
    ) -> str:
        ...


# ───────────────────────────── readback parsing ───────────────────────

_NUM_RE = re.compile(r"-?\d+(\.\d+)?(e-?\d+)?", re.IGNORECASE)


def parse_readback(raw: str) -> float | None:
    """Pull the first numeric token out of ``raw`` (e.g. ``"-12.0 dB"``
    → ``-12.0``, ``"smooth"`` → None). Returns None when no numeric
    match — the agent treats that as enum-strong evidence."""
    raw = raw.strip()
    if not raw:
        return None
    m = _NUM_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# ───────────────────────────── enumerate ──────────────────────────────

def enumerate_plugin_params(fl: Any, track_id: int, slot: int) -> list[EnumeratedParam]:
    """Wrap ``fl.get_plugin_params`` into a normalized list. The
    bridge returns a dict like ``{"params": [{"id", "name",
    "default_value", "current_value"}, ...]}`` — we adapt to a stable
    typed list."""
    raw = fl.get_plugin_params(track_id=track_id, slot=slot)
    if isinstance(raw, dict):
        rows = raw.get("params", [])
    else:
        rows = raw
    out: list[EnumeratedParam] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            out.append(EnumeratedParam(
                id=int(row["id"]),
                name=str(row.get("name") or f"Param {row['id']}"),
                default_value=float(row.get("default_value", row.get("value", 0.0))),
            ))
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping malformed param row from FL: %r", row)
    out.sort(key=lambda p: p.id)
    return out


# ───────────────────────────── single probe + sweep ───────────────────

def set_param_and_dwell(
    fl: Any,
    track_id: int,
    slot: int,
    param_id: int,
    value: float,
    *,
    dwell_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Drive ``param_id`` to ``value``, sleep ``dwell_s`` to let FL
    settle, return the bridge result dict (incl. the device script's
    readback if present)."""
    result = fl.set_plugin_param(
        track_id=track_id,
        slot=slot,
        param_id=param_id,
        value=value,
    )
    if dwell_s > 0:
        sleep(dwell_s)
    return result


def request_user_readback(
    provider: ReadbackProvider,
    *,
    prompt: str,
    expected_unit: str = "",
    context: ReadbackContext | None = None,
) -> Readback:
    """Ask the user via the provider, parse the response.

    ``context`` is forwarded as a kwarg; providers that don't accept
    it (older custom impls) fall back transparently.
    """
    try:
        raw = provider.request(prompt, expected_unit=expected_unit, context=context)
    except TypeError:
        # Provider's request() doesn't accept context= — call without.
        raw = provider.request(prompt, expected_unit=expected_unit)
    return Readback(raw=raw, parsed=parse_readback(raw))


# ───────────────────────────── classification ─────────────────────────

CLASSIFY_TEST_VALUES: tuple[float, float] = (0.25, 0.75)


def classify_param(
    fl: Any,
    track_id: int,
    slot: int,
    param_id: int,
    *,
    provider: ReadbackProvider,
    param_name: str = "",
    dwell_s: float = 0.5,
    test_values: tuple[float, float] = CLASSIFY_TEST_VALUES,
    sleep: Callable[[float], None] = time.sleep,
) -> ClassificationResult:
    """Drive the param to two distinct values, ask the user what FL
    shows for each, decide continuous vs enum.

    Heuristic:
      * Both readbacks parse as floats AND differ by >1% of mean →
        continuous.
      * Either readback is non-numeric AND the two raw strings differ
        → enum (likely a labeled value).
      * Both readbacks are identical (numeric or string) → ambiguous;
        the param probably doesn't move on this stride.
    """
    pa, pb = test_values
    label = param_name or f"param {param_id}"
    set_param_and_dwell(fl, track_id, slot, param_id, pa, dwell_s=dwell_s, sleep=sleep)
    a = request_user_readback(
        provider,
        prompt=f"Probing {label} at value {pa:.2f}. What does FL display?",
        context=ReadbackContext(
            phase="classify", param_id=param_id, param_name=param_name,
            step="1/2", param_value=pa,
        ),
    )
    set_param_and_dwell(fl, track_id, slot, param_id, pb, dwell_s=dwell_s, sleep=sleep)
    b = request_user_readback(
        provider,
        prompt=f"Probing {label} at value {pb:.2f}. What does FL display?",
        context=ReadbackContext(
            phase="classify", param_id=param_id, param_name=param_name,
            step="2/2", param_value=pb,
        ),
    )

    if a.parsed is not None and b.parsed is not None:
        denom = max(abs(a.parsed) + abs(b.parsed), 1e-9)
        rel = abs(a.parsed - b.parsed) / denom
        if rel > 0.005:
            return ClassificationResult(
                kind="continuous", confident=True, readback_a=a, readback_b=b,
                note=f"numeric pair differs by {rel*100:.1f}%",
            )
        return ClassificationResult(
            kind="ambiguous", confident=False, readback_a=a, readback_b=b,
            note="numeric readbacks too close — param may not respond to this stride",
        )

    # At least one non-numeric. If the strings differ, enum-like; if
    # they're identical, ambiguous (param may be locked or display
    # the same regardless).
    if a.raw.strip().lower() != b.raw.strip().lower():
        return ClassificationResult(
            kind="enum", confident=True, readback_a=a, readback_b=b,
            note="raw strings differ between probes",
        )
    return ClassificationResult(
        kind="ambiguous", confident=False, readback_a=a, readback_b=b,
        note="readbacks did not change between probes",
    )


# ───────────────────────────── 6-point sweep ──────────────────────────

DEFAULT_SWEEP_POINTS: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def run_sweep(
    fl: Any,
    track_id: int,
    slot: int,
    param_id: int,
    *,
    provider: ReadbackProvider,
    param_name: str = "",
    points: Sequence[float] = DEFAULT_SWEEP_POINTS,
    dwell_s: float = 0.5,
    expected_unit: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> list[tuple[float, Readback]]:
    """Drive each point in ``points``, ask for a readback, return the
    list as ``(param_value, Readback)`` pairs."""
    out: list[tuple[float, Readback]] = []
    label = param_name or f"param {param_id}"
    n = len(points)
    for i, p in enumerate(points, start=1):
        set_param_and_dwell(fl, track_id, slot, param_id, p, dwell_s=dwell_s, sleep=sleep)
        r = request_user_readback(
            provider,
            prompt=f"Sweep {label}, step {p:.2f}. What does FL display?",
            expected_unit=expected_unit,
            context=ReadbackContext(
                phase="sweep", param_id=param_id, param_name=param_name,
                step=f"{i}/{n}", param_value=p, expected_unit=expected_unit,
            ),
        )
        out.append((p, r))
    return out


def samples_from_sweep(
    sweep: Sequence[tuple[float, Readback]],
) -> list[Sample]:
    """Convert sweep results to ``curves.Sample`` instances. Skips
    rows where the readback didn't parse — the caller should surface
    that as an actionable error before fitting."""
    out: list[Sample] = []
    for p, r in sweep:
        if r.parsed is None:
            continue
        out.append(Sample(param=p, displayed=r.parsed))
    return out


def fit_param(samples: Sequence[Sample], *, min_r_squared: float = 0.99) -> Fit | None:
    """Run the curve calibrator over ``samples``. Returns the
    selected Fit or None if no shape clears the R² floor."""
    result = curve_calibrate(list(samples), min_r_squared=min_r_squared)
    if result is None:
        return None
    return result.selected


# ───────────────────────────── validation probes ──────────────────────

EXTREMITY_PROBE_OFFSETS: tuple[float, float] = (0.05, 0.95)


def select_validation_probes(
    fit: Fit,
    runner_up: Fit | None,
    *,
    n_extremity: int = 2,
    n_disagreement: int = 2,
    grid_resolution: int = 199,
) -> list[float]:
    """Deterministically pick ``n_extremity`` probes near the param
    range edges plus ``n_disagreement`` probes where ``fit`` and
    ``runner_up`` disagree most. With no runner-up, falls back to
    just the extremities + a midpoint pair."""
    extremities = list(EXTREMITY_PROBE_OFFSETS[:n_extremity])

    if runner_up is None or n_disagreement <= 0:
        # Pad with deterministic midpoints if needed.
        midpoints = [0.30, 0.55, 0.70, 0.85][:n_disagreement]
        return sorted(set(extremities + midpoints))

    # Sample a fine grid; pick the points where |fit - runner_up| is
    # largest, with mutual-spacing so we don't pile two probes onto
    # the same peak.
    grid = [i / (grid_resolution - 1) for i in range(grid_resolution)]
    deltas = [abs(predict(fit, p) - predict(runner_up, p)) for p in grid]
    order = sorted(range(grid_resolution), key=lambda i: -deltas[i])

    chosen: list[float] = []
    min_spacing = 1.0 / (n_disagreement + 1)
    for idx in order:
        cand = grid[idx]
        if all(abs(cand - c) >= min_spacing for c in chosen + extremities):
            chosen.append(cand)
            if len(chosen) >= n_disagreement:
                break
    return sorted(set(extremities + chosen))


def validate_fit(
    fl: Any,
    track_id: int,
    slot: int,
    param_id: int,
    *,
    fit: Fit,
    runner_up: Fit | None = None,
    provider: ReadbackProvider,
    param_name: str = "",
    tolerance_abs: float = 0.5,
    tolerance_rel: float = 0.01,
    dwell_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    probe_points: Sequence[float] | None = None,
) -> ValidationOutcome:
    """Drive ``fit`` at a deterministic set of probe points, ask the
    user for FL's readback at each, compare against the fit's
    prediction. Pass = every probe within tolerance.

    ``tolerance`` is ``max(tolerance_abs, tolerance_rel * |predicted|)``
    per design doc.
    """
    points = list(probe_points) if probe_points else select_validation_probes(fit, runner_up)
    probes: list[ValidationProbeResult] = []
    all_ok = True
    label = param_name or f"param {param_id}"
    n = len(points)
    for i, p in enumerate(points, start=1):
        set_param_and_dwell(fl, track_id, slot, param_id, p, dwell_s=dwell_s, sleep=sleep)
        readback = request_user_readback(
            provider,
            prompt=f"Validation probe {label} at {p:.3f}. What does FL display?",
            context=ReadbackContext(
                phase="validate", param_id=param_id, param_name=param_name,
                step=f"{i}/{n}", param_value=p,
            ),
        )
        predicted = predict(fit, p)
        actual = readback.parsed
        delta = (actual - predicted) if actual is not None else None
        tolerance = max(tolerance_abs, tolerance_rel * abs(predicted))
        ok = (
            actual is not None
            and abs(actual - predicted) <= tolerance
        )
        all_ok = all_ok and ok
        probes.append(ValidationProbeResult(
            param_value=p,
            predicted=predicted,
            actual_raw=readback.raw,
            actual=actual,
            delta=delta,
            ok=ok,
            tolerance=tolerance,
        ))
    return ValidationOutcome(fit=fit, probes=tuple(probes), passed=all_ok)
