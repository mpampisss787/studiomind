"""Generate the five files that make up a hermetic skill directory
from a calibrated ``SkillSpec``.

Reproducibility property: same ``SkillSpec`` → byte-identical files.
The training agent calls this after curve fitting and validation
succeed; the resulting files go through ``propose_write`` (sandboxed)
before they're flushed to disk.

Outputs:

  * ``__init__.py`` — empty package marker
  * ``manifest.json`` — schema-versioned manifest incl. ``content_hash``
  * ``wrapper.py`` — typed param-conversion module
  * ``tool.py`` — TOOL schema + execute/build helpers
  * ``knowledge.md`` — capability + gotcha prompt content
  * ``tests.py`` — round-trip and anchor tests
  * ``calibration-logs/<session>.json`` — full samples + fits + probes

The wrapper is **runtime-cheap**: pure-Python math (``math.log``,
``math.exp``, ``math.sqrt``) only, no scipy/numpy. Continuous
conversion functions are emitted from the ``Fit`` shape via per-shape
templates; enum params get a static lookup table.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

# ───────────────────────────── data classes ───────────────────────────

ShapeName = Literal["linear", "log", "power", "exp", "quadratic"]


@dataclass(frozen=True)
class FitSpec:
    """Subset of curves.Fit needed to emit code. Plain types only."""
    shape: ShapeName
    params: tuple[float, ...]
    r_squared: float


@dataclass(frozen=True)
class ValidationProbeSpec:
    param_value: float
    predicted: float
    actual: float
    ok: bool


@dataclass(frozen=True)
class ParamSpec:
    """One plugin parameter, fully calibrated."""
    id: int
    name: str                              # FL display name, e.g. "Ceiling"
    kind: Literal["continuous", "enum"]
    # For continuous params: the fit + min/max human-units bounds.
    fit: FitSpec | None = None
    human_min: float | None = None
    human_max: float | None = None
    unit: str = ""                          # "dB", "ms", ":1", ""
    # For enum params:
    enum_values: dict[str, float] = field(default_factory=dict)
    # Probes that landed (predicted vs actual readbacks):
    validation_probes: tuple[ValidationProbeSpec, ...] = ()

    def slug(self) -> str:
        """snake_case identifier — used as the wrapper's function and
        constant name root."""
        return _slugify(self.name)


@dataclass(frozen=True)
class SkillSpec:
    """Everything the generator needs. Ordering is important — params
    must be in stable id order so the output is reproducible."""
    plugin_name: str                        # "Fruity Limiter"
    skill_name: str                         # "fruity_limiter"
    tool_name: str                          # "set_limiter"
    fl_version: str                         # "21.2.10"
    acquired_iso: str                       # "2026-04-30T15:22:08+03:00"
    calibration_log_relpath: str            # "calibration-logs/2026-04-30T15-22-08.json"
    params: tuple[ParamSpec, ...] = ()


# ───────────────────────────── slug helpers ───────────────────────────

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slugify(name: str) -> str:
    """FL display name → snake_case identifier. Lowercases, strips
    non-alphanumerics, collapses runs."""
    s = _SLUG_RE.sub("_", name).strip("_").lower()
    return s or "param"


def default_skill_name(plugin_name: str) -> str:
    return _slugify(plugin_name)


def default_tool_name(plugin_name: str) -> str:
    """Drop a leading ``fruity_`` from the slug if present so we get
    ``set_limiter`` instead of ``set_fruity_limiter``."""
    slug = _slugify(plugin_name)
    if slug.startswith("fruity_"):
        slug = slug[len("fruity_"):]
    return f"set_{slug}"


# ───────────────────────────── content_hash ───────────────────────────

def _content_hash(manifest_minus_hash: dict, wrapper: str, tool: str, knowledge: str) -> str:
    """Same canonicalisation as ``skills/_registry.compute_content_hash``."""
    canonical = json.dumps(manifest_minus_hash, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    h.update(canonical.encode("utf-8"))
    for body in (wrapper, tool, knowledge):
        h.update(b"\x1e")
        h.update(body.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


# ───────────────────────────── manifest ───────────────────────────────

def _manifest_dict(spec: SkillSpec, *, content_hash: str | None) -> dict:
    """Build the manifest dict. ``content_hash`` is None on the first
    pass (we hash without it), then re-injected on the second pass."""
    params_section: list[dict[str, Any]] = []
    probes_section: list[dict[str, Any]] = []
    for p in sorted(spec.params, key=lambda x: x.id):
        if p.kind == "continuous":
            assert p.fit is not None, f"continuous param {p.name} missing fit"
            params_section.append({
                "id": p.id,
                "name": p.name,
                "kind": "continuous",
                "unit": p.unit,
                "human_min": p.human_min,
                "human_max": p.human_max,
                "fit": {
                    "shape": p.fit.shape,
                    "params": list(p.fit.params),
                    "r_squared": p.fit.r_squared,
                },
            })
        else:
            params_section.append({
                "id": p.id,
                "name": p.name,
                "kind": "enum",
                "values": dict(p.enum_values),
            })
        for v in p.validation_probes:
            probes_section.append({
                "param_id": p.id,
                "param_value": v.param_value,
                "predicted": v.predicted,
                "actual": v.actual,
                "ok": v.ok,
            })

    out: dict[str, Any] = {
        "schema_version": 1,
        "name": spec.skill_name,
        "type": "plugin_wrapper",
        "display_name": spec.plugin_name,
        "tool_name": spec.tool_name,
        "destructive": True,
        "fl_version": spec.fl_version,
        "acquired": spec.acquired_iso,
        "calibration_log": spec.calibration_log_relpath,
        "params": params_section,
        "validation_probes": probes_section,
        "origin": "studiomind-training-mode",
    }
    if content_hash is not None:
        out["content_hash"] = content_hash
    return out


def _render_manifest_json(manifest: dict) -> str:
    """Stable indented JSON with trailing newline."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


# ───────────────────────────── wrapper.py ─────────────────────────────

_WRAPPER_HEADER = '''\
"""Auto-generated typed wrapper for {plugin_name}.

Generated by studiomind-training-mode on {acquired}. Do not hand-edit
unless you also re-run the calibration sweep — content_hash will drift
otherwise.
"""

from __future__ import annotations

import math
from typing import Any

PLUGIN_NAME = {plugin_name!r}
'''

_WRAPPER_BUILD_FOOTER = '''
def build_commands(track_id: int, slot: int, **kwargs: Any) -> list[dict[str, Any]]:
    """Translate human-units kwargs into a list of set_plugin_param
    SysEx commands. Pass only the params you want to write — anything
    omitted leaves the FL value untouched."""
    cmds: list[dict[str, Any]] = []
{build_body}    return cmds
'''


def _wrapper_continuous_block(p: ParamSpec) -> str:
    """Per-continuous-param: PARAM_<NAME>, _<NAME>_MIN, _<NAME>_MAX,
    <name>_to_param, param_to_<name>."""
    assert p.fit is not None
    slug = p.slug()
    upper = slug.upper()
    forward = _emit_forward_fn(p)
    inverse = _emit_inverse_fn(p)
    return f"""
PARAM_{upper} = {p.id}
{upper}_MIN = {p.human_min!r}
{upper}_MAX = {p.human_max!r}
{upper}_FIT_SHAPE = {p.fit.shape!r}
{upper}_FIT_PARAMS = {tuple(p.fit.params)!r}

{forward}

{inverse}
"""


def _emit_forward_fn(p: ParamSpec) -> str:
    """Emit ``param_to_<slug>(p) -> human_value`` from the fit shape."""
    slug = p.slug()
    fit = p.fit
    assert fit is not None
    if fit.shape == "linear":
        a, b = fit.params
        body = f"    return {a!r} * p + {b!r}"
    elif fit.shape == "log":
        a, b = fit.params
        body = f"    return {a!r} * math.log(max(p, 1e-9) + 1e-9) + {b!r}"
    elif fit.shape == "power":
        a, b, c = fit.params
        body = f"    return {a!r} * ((max(p, 0.0) + 1e-9) ** {b!r}) + {c!r}"
    elif fit.shape == "exp":
        a, b, c = fit.params
        body = f"    return {a!r} * math.exp({b!r} * p) + {c!r}"
    elif fit.shape == "quadratic":
        a, b, c = fit.params
        body = f"    return {a!r} * p * p + {b!r} * p + {c!r}"
    else:
        raise ValueError(f"Unknown fit shape: {fit.shape}")
    return f"def param_to_{slug}(p: float) -> float:\n{body}\n"


def _emit_inverse_fn(p: ParamSpec) -> str:
    """Emit ``<slug>_to_param(value) -> param`` from the fit shape.
    Clamps to [human_min, human_max] before inverting; final result
    clamped to [0, 1]."""
    slug = p.slug()
    fit = p.fit
    assert fit is not None
    upper = slug.upper()
    pre = (
        f"    if {upper}_MIN is not None and value < {upper}_MIN:\n"
        f"        value = {upper}_MIN\n"
        f"    if {upper}_MAX is not None and value > {upper}_MAX:\n"
        f"        value = {upper}_MAX\n"
    )
    if fit.shape == "linear":
        a, b = fit.params
        body = f"    p = (value - {b!r}) / {a!r}"
    elif fit.shape == "log":
        a, b = fit.params
        body = (
            f"    p = math.exp((value - {b!r}) / {a!r}) - 1e-9\n"
        )
    elif fit.shape == "power":
        a, b, c = fit.params
        body = (
            f"    base = (value - {c!r}) / {a!r}\n"
            f"    if base <= 0.0:\n"
            f"        return 0.0\n"
            f"    p = base ** (1.0 / {b!r}) - 1e-9"
        )
    elif fit.shape == "exp":
        a, b, c = fit.params
        body = (
            f"    arg = (value - {c!r}) / {a!r}\n"
            f"    if arg <= 0.0:\n"
            f"        return 0.0\n"
            f"    p = math.log(arg) / {b!r}"
        )
    elif fit.shape == "quadratic":
        a, b, c = fit.params
        # Pick the root that lands in [0,1] (typically the "+" branch
        # for monotonically-increasing fits over the param range).
        body = (
            f"    A, B, C = {a!r}, {b!r}, {c!r} - value\n"
            f"    disc = B * B - 4.0 * A * C\n"
            f"    if disc < 0.0 or A == 0.0:\n"
            f"        return 0.0 if value <= {c!r} else 1.0\n"
            f"    root_pos = (-B + math.sqrt(disc)) / (2.0 * A)\n"
            f"    root_neg = (-B - math.sqrt(disc)) / (2.0 * A)\n"
            f"    p = root_pos if 0.0 <= root_pos <= 1.0 else root_neg"
        )
    else:
        raise ValueError(f"Unknown fit shape: {fit.shape}")
    tail = (
        "    if p < 0.0:\n"
        "        return 0.0\n"
        "    if p > 1.0:\n"
        "        return 1.0\n"
        "    return p\n"
    )
    return (
        f"def {slug}_to_param(value: float) -> float:\n"
        f"{pre}{body}\n"
        f"{tail}"
    )


def _wrapper_enum_block(p: ParamSpec) -> str:
    """Per-enum-param: PARAM_<NAME>, <NAME>_VALUES, <name>_to_param,
    param_to_<name>."""
    slug = p.slug()
    upper = slug.upper()
    items_sorted = sorted(p.enum_values.items())
    values_dict = "{" + ", ".join(f"{k!r}: {v!r}" for k, v in items_sorted) + "}"
    reverse_dict = "{" + ", ".join(f"{v!r}: {k!r}" for k, v in items_sorted) + "}"
    return f"""
PARAM_{upper} = {p.id}
{upper}_VALUES: dict[str, float] = {values_dict}
_{upper}_REVERSE: dict[float, str] = {reverse_dict}


def {slug}_to_param(label: str) -> float:
    key = label.strip().lower()
    for k, v in {upper}_VALUES.items():
        if k.lower() == key:
            return v
    raise ValueError(f"Unknown {slug}: {{label!r}} (valid: {{list({upper}_VALUES)!r}})")


def param_to_{slug}(p: float) -> str:
    # Pick the enum entry whose stored value is closest to p.
    return min({upper}_VALUES.items(), key=lambda kv: abs(kv[1] - p))[0]
"""


def _wrapper_build_body(spec: SkillSpec) -> str:
    """Body of build_commands() — one append-block per param."""
    lines: list[str] = []
    for p in sorted(spec.params, key=lambda x: x.id):
        slug = p.slug()
        if p.kind == "continuous":
            lines.append(
                f'    if {slug!r} in kwargs and kwargs[{slug!r}] is not None:\n'
                f'        cmds.append({{\n'
                f'            "track_id": track_id,\n'
                f'            "slot": slot,\n'
                f'            "param_id": PARAM_{slug.upper()},\n'
                f'            "value": {slug}_to_param(float(kwargs[{slug!r}])),\n'
                f'        }})\n'
            )
        else:  # enum
            lines.append(
                f'    if {slug!r} in kwargs and kwargs[{slug!r}] is not None:\n'
                f'        cmds.append({{\n'
                f'            "track_id": track_id,\n'
                f'            "slot": slot,\n'
                f'            "param_id": PARAM_{slug.upper()},\n'
                f'            "value": {slug}_to_param(str(kwargs[{slug!r}])),\n'
                f'        }})\n'
            )
    return "".join(lines)


def render_wrapper_py(spec: SkillSpec) -> str:
    parts: list[str] = [
        _WRAPPER_HEADER.format(plugin_name=spec.plugin_name, acquired=spec.acquired_iso)
    ]
    for p in sorted(spec.params, key=lambda x: x.id):
        if p.kind == "continuous":
            parts.append(_wrapper_continuous_block(p))
        else:
            parts.append(_wrapper_enum_block(p))
    parts.append(_WRAPPER_BUILD_FOOTER.format(build_body=_wrapper_build_body(spec)))
    return "\n".join(parts)


# ───────────────────────────── tool.py ────────────────────────────────

_TOOL_HEADER = '''\
"""Auto-generated tool spec + executor for {plugin_name}.

Exposes the four entry points the mixing-agent ToolExecutor expects:
TOOL, build_commands_from_args, execute, description_from_args.
Generated by studiomind-training-mode on {acquired}.
"""

from __future__ import annotations

from typing import Any

from studiomind.skills.{skill_name} import wrapper as _wrapper


WRITE_TOLERANCE = 1e-3
'''

_TOOL_BUILD_AND_EXECUTE = '''
def build_commands_from_args(args: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: arg dict → list of set_plugin_param command dicts."""
    return _wrapper.build_commands(
        track_id=args["track_id"],
        slot=args["slot"],
{forward_kwargs}    )


def execute(fl: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive every command through the FL bridge and verify each
    write against the device script's new_value readback."""
    commands = build_commands_from_args(args)

    per_param: list[dict[str, Any]] = []
    for cmd in commands:
        requested = cmd["value"]
        result = fl.set_plugin_param(
            track_id=cmd["track_id"],
            slot=cmd["slot"],
            param_id=cmd["param_id"],
            value=requested,
        )
        new_value = result.get("new_value") if isinstance(result, dict) else None
        took = (
            isinstance(new_value, (int, float))
            and abs(float(new_value) - float(requested)) <= WRITE_TOLERANCE
        )
        per_param.append({{
            "param_id": cmd["param_id"],
            "requested_value": requested,
            "new_value": new_value,
            "display": result.get("display") if isinstance(result, dict) else None,
            "took": took,
        }})

    succeeded = [p for p in per_param if p["took"]]
    failed = [p for p in per_param if not p["took"]]
    return {{
        "ok": len(failed) == 0,
        "params_attempted": len(commands),
        "params_accepted": len(succeeded),
        "params_rejected": len(failed),
        "per_param": per_param,
{echo_args}    }}


def description_from_args(args: dict[str, Any]) -> str:
    """One-line description for the centralized decision logger."""
    bits: list[str] = []
{describe_body}    bits_str = ", ".join(bits) if bits else "no changes"
    return f"{plugin_name} on track {{args.get('track_id')}} slot {{args.get('slot')}}: {{bits_str}}"
'''


def _tool_input_schema(spec: SkillSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "track_id": {"type": "integer", "description": "Mixer track index"},
        "slot": {"type": "integer", "description": f"FX slot where {spec.plugin_name} is loaded (0-9)"},
    }
    for p in sorted(spec.params, key=lambda x: x.id):
        slug = p.slug()
        if p.kind == "continuous":
            entry: dict[str, Any] = {
                "type": "number",
                "description": f"{p.name} ({p.unit or 'continuous'}).",
            }
            if p.human_min is not None:
                entry["minimum"] = p.human_min
            if p.human_max is not None:
                entry["maximum"] = p.human_max
            properties[slug] = entry
        else:
            properties[slug] = {
                "type": "string",
                "enum": sorted(p.enum_values.keys()),
                "description": f"{p.name} (one of {sorted(p.enum_values.keys())!r}).",
            }
    return {
        "type": "object",
        "properties": properties,
        "required": ["track_id", "slot"],
    }


def render_tool_py(spec: SkillSpec) -> str:
    schema = _tool_input_schema(spec)
    schema_json = json.dumps(schema, indent=4, sort_keys=True)
    schema_indented = "\n".join(("    " + line) if i else line for i, line in enumerate(schema_json.splitlines()))

    description = (
        f"Set {spec.plugin_name} parameters using human-readable units. "
        f"PREFERRED for any {spec.plugin_name} adjustment over the "
        f"generic set_plugin_param. Pass only the parameters you want to "
        f"change. ALWAYS call snapshot() before using this tool."
    )

    forward_kwargs_lines: list[str] = []
    describe_lines: list[str] = []
    echo_lines: list[str] = []
    for p in sorted(spec.params, key=lambda x: x.id):
        slug = p.slug()
        forward_kwargs_lines.append(f"        {slug}=args.get({slug!r}),\n")
        echo_lines.append(f'        {slug!r}: args.get({slug!r}),\n')
        if p.unit:
            describe_lines.append(
                f"    if args.get({slug!r}) is not None:\n"
                f"        bits.append(f\"{slug} {{args[{slug!r}]}} {p.unit}\")\n"
            )
        else:
            describe_lines.append(
                f"    if args.get({slug!r}) is not None:\n"
                f"        bits.append(f\"{slug} {{args[{slug!r}]}}\")\n"
            )

    parts = [
        _TOOL_HEADER.format(
            plugin_name=spec.plugin_name,
            skill_name=spec.skill_name,
            acquired=spec.acquired_iso,
        ),
    ]
    parts.append(
        f"\nTOOL: dict[str, Any] = {{\n"
        f"    \"name\": {spec.tool_name!r},\n"
        f"    \"description\": (\n"
        f"        {description!r}\n"
        f"    ),\n"
        f"    \"input_schema\": {schema_indented},\n"
        f"}}\n"
    )
    parts.append(
        _TOOL_BUILD_AND_EXECUTE.format(
            forward_kwargs="".join(forward_kwargs_lines),
            echo_args="".join(echo_lines),
            describe_body="".join(describe_lines),
            plugin_name=spec.plugin_name,
        )
    )
    return "".join(parts)


# ───────────────────────────── tests.py ───────────────────────────────

def render_tests_py(spec: SkillSpec) -> str:
    """Generate round-trip + anchor tests for each continuous param
    and an enum-membership test for each enum param."""
    lines: list[str] = [
        '"""Auto-generated tests for ' + spec.plugin_name + '."""',
        '',
        'from __future__ import annotations',
        '',
        'import pytest',
        '',
        f'from studiomind.skills.{spec.skill_name} import wrapper as w',
        f'from studiomind.skills.{spec.skill_name} import tool as t',
        '',
        '',
    ]
    for p in sorted(spec.params, key=lambda x: x.id):
        slug = p.slug()
        if p.kind == "continuous":
            assert p.fit is not None
            # Round-trip across the validation probes — those are the
            # readbacks the user actually confirmed during acquisition.
            lines.append(f"def test_{slug}_validation_probes_roundtrip() -> None:")
            if p.validation_probes:
                for v in p.validation_probes:
                    lines.append(
                        f"    assert w.param_to_{slug}({v.param_value!r}) == "
                        f"pytest.approx({v.actual!r}, abs=0.5, rel=0.01)"
                    )
            else:
                lines.append("    pass  # no validation probes captured")
            lines.append("")
            lines.append("")
            # Range bounds:
            lines.append(f"def test_{slug}_clamps_to_min() -> None:")
            if p.human_min is not None:
                lines.append(f"    assert w.{slug}_to_param({p.human_min!r} - 100) == 0.0")
            else:
                lines.append("    pass")
            lines.append("")
            lines.append("")
            lines.append(f"def test_{slug}_clamps_to_max() -> None:")
            if p.human_max is not None:
                lines.append(f"    assert w.{slug}_to_param({p.human_max!r} + 100) == 1.0")
            else:
                lines.append("    pass")
            lines.append("")
            lines.append("")
        else:
            # Enum: round-trip every label.
            lines.append(f"def test_{slug}_enum_round_trip() -> None:")
            if p.enum_values:
                for k in sorted(p.enum_values):
                    lines.append(f"    assert w.param_to_{slug}(w.{slug}_to_param({k!r})) == {k!r}")
            else:
                lines.append("    pass")
            lines.append("")
            lines.append("")
    # Tool spec sanity:
    lines.append("def test_tool_spec_well_formed() -> None:")
    lines.append(f"    assert t.TOOL['name'] == {spec.tool_name!r}")
    lines.append("    assert 'track_id' in t.TOOL['input_schema']['required']")
    lines.append("    assert 'slot' in t.TOOL['input_schema']['required']")
    lines.append("")
    return "\n".join(lines) + "\n"


# ───────────────────────────── knowledge.md ───────────────────────────

def render_knowledge_md(spec: SkillSpec) -> str:
    """Minimal capability section. The training agent can extend this
    via knowledge_extra at acquisition time, but v1 emits a clean
    skeleton even without."""
    lines = [
        f"# {spec.plugin_name}",
        "",
        "## Capabilities",
        "",
        f"Auto-generated wrapper for {spec.plugin_name}. Use `{spec.tool_name}` "
        f"with human units; the wrapper handles param-value normalization.",
        "",
        "## Calibrated parameters",
        "",
    ]
    for p in sorted(spec.params, key=lambda x: x.id):
        if p.kind == "continuous":
            assert p.fit is not None
            unit = f" {p.unit}" if p.unit else ""
            range_str = ""
            if p.human_min is not None and p.human_max is not None:
                range_str = f" — `[{p.human_min}, {p.human_max}]{unit}`"
            lines.append(
                f"- **{p.name}** (continuous, fit `{p.fit.shape}` "
                f"R²={p.fit.r_squared:.4f}){range_str}"
            )
        else:
            labels = ", ".join(sorted(p.enum_values))
            lines.append(f"- **{p.name}** (enum): {labels}")
    lines.append("")
    lines.append("## Gotchas")
    lines.append("")
    lines.append(
        "- This wrapper was generated by `studiomind-training-mode`. "
        "If FL renumbers or rescales these params in a future version, "
        "the calibration log in `calibration-logs/` lets you re-fit "
        "without a fresh sweep."
    )
    lines.append("")
    return "\n".join(lines)


# ───────────────────────────── public entry point ─────────────────────

def render_skill(spec: SkillSpec) -> dict[str, str]:
    """Generate every text file for the skill. Returns a mapping
    from path-relative-to-skill-dir → file content. The caller is
    responsible for writing them through the sandbox."""
    wrapper = render_wrapper_py(spec)
    tool = render_tool_py(spec)
    knowledge = render_knowledge_md(spec)
    tests = render_tests_py(spec)

    # First pass: hash without content_hash field; second pass: inject.
    manifest_no_hash = _manifest_dict(spec, content_hash=None)
    h = _content_hash(manifest_no_hash, wrapper, tool, knowledge)
    manifest = _manifest_dict(spec, content_hash=h)

    return {
        "__init__.py": "",
        "manifest.json": _render_manifest_json(manifest),
        "wrapper.py": wrapper,
        "tool.py": tool,
        "knowledge.md": knowledge,
        "tests.py": tests,
    }


def render_calibration_log(
    spec: SkillSpec,
    *,
    started_iso: str,
    finished_iso: str,
    raw_samples_per_param: dict[int, list[dict[str, Any]]] | None = None,
    fits_attempted_per_param: dict[int, list[dict[str, Any]]] | None = None,
) -> str:
    """Emit the JSON sidecar that travels in calibration-logs/. Plain
    text — the caller writes it under the skill dir alongside the five
    canonical files."""
    samples_default: dict[int, list[dict[str, Any]]] = raw_samples_per_param or {}
    fits_default: dict[int, list[dict[str, Any]]] = fits_attempted_per_param or {}
    out = {
        "plugin": spec.plugin_name,
        "fl_version": spec.fl_version,
        "session_started": started_iso,
        "session_finished": finished_iso,
        "params": [
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "samples": samples_default.get(p.id, []),
                "fits_attempted": fits_default.get(p.id, []),
                "selected_fit": (
                    {
                        "shape": p.fit.shape,
                        "params": list(p.fit.params),
                        "r_squared": p.fit.r_squared,
                    } if p.fit else None
                ),
                "validation_probes": [
                    {
                        "param_value": v.param_value,
                        "predicted": v.predicted,
                        "actual": v.actual,
                        "ok": v.ok,
                    }
                    for v in p.validation_probes
                ],
                "enum_values": dict(p.enum_values) if p.kind == "enum" else None,
            }
            for p in sorted(spec.params, key=lambda x: x.id)
        ],
    }
    return json.dumps(out, indent=2, sort_keys=True) + "\n"
