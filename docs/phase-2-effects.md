# Phase 2 — Effects tooling plan

Phase 1 shipped with the EQ vertical slice (2026-04-24). Phase 2 adds the
rest of the mix-engineer's core toolkit: **compression, sidechain, reverb,
delay, stereo width, automation**.

This doc is the roadmap so Phase 2 work doesn't fragment. It also records
the design decisions (typed wrapper vs param-map; native vs third-party;
per-track vs master-bus) so future sessions don't re-derive them.

## Current state

Already available to the agent:

- `set_builtin_eq(track_id, band, gain, frequency, bandwidth)` — stock 3-band EQ on every mixer track
- `set_proq3(track_id, slot, band, ...)` — typed wrapper over Pro-Q 3 with Hz/dB/Q inputs
- `set_plugin_param(track_id, slot, param_id, value)` — generic VST parameter access
- `read_mixer_track(track_id)` — returns every plugin slot with its advertised params

The agent *can* already change any plugin parameter today via
`set_plugin_param` — but it has to (a) know the plugin is loaded from
`read_mixer_track`, (b) figure out the correct param ID by name, and
(c) know what normalized value to pass. Typed wrappers collapse those
three steps into one, but they're not strictly required for the agent to
be useful on any given effect.

## Priority order (in planned ship order)

1. **Fruity Compressor** — stock, simplest param model (threshold / ratio /
   attack / release / gain). Comp is the #1 effect after EQ. Every FL user
   has it. Target: typed `set_compressor` wrapper + system-prompt knowledge.
2. **Fruity Limiter** — stock, widely used on the master bus + on tracks as
   a simpler one-knob comp. More complex internal structure (has EQ section
   + comp section + limiter section). Typed wrapper for the comp/limiter
   sections.
3. **Fruity Reeverb 2** — stock reverb. Send-effect pattern; the agent
   also needs to learn about aux sends, not just insert effects. Typed
   wrapper: `set_reverb(track_id, slot, size, decay_s, damp, wet, dry)`.
4. **Fruity Delay 3** — stock delay. Similar shape to reverb.
5. **Sidechain routing** — mixer send from kick track to comp key-input on
   a bass/synth track. Not a single plugin, a *routing pattern*. Needs its
   own tool: `apply_sidechain(source_track, target_track, amount_db)`.
6. **Fruity Stereo Enhancer** — width control.
7. **Automation** — parameter automation writing via FL's event system. Complex,
   defer.

## The typed-wrapper pattern (established)

Mirror `src/studiomind/plugins/fabfilter_proq3.py`:

- Module per plugin: `src/studiomind/plugins/<name>.py`
- Constants for param offsets, ranges, enums
- `*_to_param(value) -> float` and `param_to_*(value) -> original` for each
  continuous parameter so the agent can read back actual values from a
  `read_mixer_track` response
- `build_<plugin>_commands(...)` returns a list of `{track_id, slot, param_id, value}` dicts
- The corresponding `_exec_set_<plugin>` in `agent/tools.py` calls
  `build_*_commands` and dispatches each one via `fl.set_plugin_param`
- Tool schema in `TOOL_SCHEMAS` with human-friendly parameter names and ranges
- Decision logging via `_log_decision` already wraps destructive execs

## What blocks building any of them from Linux

**Param IDs must be verified on a live FL instance.** VST param IDs are
not in any public document for FL's stock plugins; they're whatever the
plugin advertises via `getParamName`. For Pro-Q 3 we knew them because
FabFilter publishes a reference. For Fruity Compressor we don't.

Three strategies:

1. **Discover at runtime.** When the agent first encounters a Fruity
   Compressor on a track, call `read_mixer_track` → scan the plugin's
   params → build a name→id map on the fly. No pre-baked profile needed.
   Cheap but requires the plugin to be loaded somewhere before we know
   the mapping. Also vulnerable to localized builds (French FL names
   params in French).
2. **User-supplied enumeration.** A one-off script the user runs: loads
   Fruity Compressor in FL, walks params, dumps `{id: name}` JSON.
   StudioMind ships that JSON alongside the profile module. Verified,
   deterministic, localization-proof.
3. **Wait for the write path to break.** Ship typed wrappers with
   best-guess IDs from community reverse-engineering. First bad write
   surfaces the mismatch.

Plan: **Option 2 first**, falling back to Option 1 when a user hasn't
supplied a map.

## Unblocked work this session

Even without verified param IDs, we can:

- Ship **system-prompt knowledge** for common compression / reverb /
  sidechain moves so the agent uses `set_plugin_param` sensibly when it
  finds the relevant plugin on a track
- Ship a **param-inspection tool** that summarizes a plugin's param table
  from a `read_mixer_track` response — so the agent doesn't have to read
  the whole blob just to find the threshold param
- Write this doc

Typed `set_compressor` and friends wait until we have a verified param
map OR the inspection-tool-driven discovery approach is built.

## Non-goals

- **Third-party VSTs (FabFilter Pro-C 2, Slate, Waves, etc.)** — out of
  scope for Phase 2. Pattern is the same as Pro-Q 3, one profile per
  plugin; the decision of which to support is marketing, not architecture.
- **Automation writing** — FL's automation model is pattern-based and
  event-timed, a lot of surface area. Defer to Phase 4.
- **MIDI-generated notes / drum patterns** — that's Phase 3 per the main
  roadmap.
