# Training Mode

> Self-extending StudioMind. The mixing agent acts on audio; the training
> agent acquires new skills by being taught. Each acquired skill becomes
> a typed wrapper, a knowledge note, and a tool the mixing agent can call.

## Status

Design — not yet implemented (2026-04-29). v1 scope: plugin-wrapper
acquisition only. Mixing-technique training is v2.

## Why this exists

Today every new plugin requires:
1. Load plugin in FL on a spare track
2. Run `scripts/enumerate_plugin_params.py` from the CLI on Windows
3. Commit JSON, switch back to Linux dev box
4. Hand-write a typed wrapper (~200 lines)
5. Hand-write tests (~100 lines)
6. Live-calibrate each continuous param via ad-hoc CLI scripts (we just
   did this for Fruity Compressor: two CLI helpers, two sweeps, manual
   curve-fitting in my head, hand-edit the wrapper)
7. Commit, push, hope it works on the next mixing session

That's ~2 hours per plugin and **four plugins are queued** (Limiter,
Reeverb 2, Delay 3, Stereo Enhancer). After v1: any plugin becomes a
skill in ~10 minutes of guided dialogue, with the agent driving the
flow and writing its own code under user supervision.

The deeper point: this is what it actually means for StudioMind to be
*"Claude Code, but for music production."* Claude Code's value isn't
that it generates code — it's that it captures developer skill into
artifacts other agents can use. Training mode gives the same property
to plugin / production knowledge.

## Two agents, one product

| Mode | Agent's role | User's role | Output |
|------|--------------|-------------|--------|
| **Mixing** (existing) | Acts on the mix using its current skills | Sets goals, listens, gives feedback | Audio decisions + revert/commit |
| **Training** (this doc) | Acquires a new skill by being taught | Teaches: confirms readings, approves diffs | Wrapper module + tests + knowledge note + new tool |

Modes do not run concurrently in a session. Selector lives at the top
of the web UI: **Mix** / **Train**.

## v1 example walkthrough — Fruity Limiter

What the user actually sees and does to acquire the Limiter wrapper:

1. Click **Train** → "What should I learn?" → "A new plugin"
2. Type plugin name: `Fruity Limiter`
3. Agent: *"Please load Fruity Limiter on a spare mixer track and tell
   me the track and slot. Reply when ready."*
4. User: `track 5, slot 0`. Agent calls `enumerate_plugin_params(5, 0)`,
   panel renders the param table (22 rows).
5. Agent classifies each param: continuous vs enum (heuristic: probe
   default value + a small step; if displayed string changes by a
   non-trivial real, continuous; if it jumps between discrete strings,
   enum).
6. For each continuous param, agent runs a 6-point sweep:
   - Sets param to 0.0, 0.2, 0.4, 0.6, 0.8, 1.0 with 2s dwell
   - At each step, UI shows an input box: *"What does FL display?"*
   - User reads the knob (right-click → Edit value) and types the number
7. After all sweeps, agent fits multiple curves (linear / log /
   exponential / power / quadratic), picks the shape with highest R²
   that's also parsimonious. Reports each fit + R² in the chat.
8. Agent picks two random param values not in the sweep, drives them in
   FL, asks the user to read each. Compares against the wrapper's
   prediction. **Validation gate**: if either probe is off by more than
   tolerance, sweep again with extra points.
9. Agent generates four files (shown as a unified diff in the UI):
   - `src/studiomind/plugins/fruity_limiter.py`
   - `tests/test_fruity_limiter.py`
   - `skills/fruity_limiter.md`
   - patch to `src/studiomind/agent/tools.py` (append `set_limiter`)
10. Agent runs `pytest tests/test_fruity_limiter.py` (in-process). All
    green is required. If red, agent shows the failure and offers to
    regenerate or abort.
11. Agent: *"24/24 tests pass. Approve to commit?"* User clicks
    **Approve**. Agent commits with a structured message; **never
    pushes**. User pushes manually if/when ready.
12. Skill registry picks up the new entry on next mixing-mode startup;
    knowledge note auto-appended to mixing system prompt.

## Architecture

### Layout

```
src/studiomind/
  agent/
    loop.py                # existing — mixing agent
    learning_loop.py       # NEW — training agent loop
    learning_tools.py      # NEW — calibration + code-edit tools
    learning_prompt.py     # NEW — student-agent system prompt
  skill_registry.py        # NEW — scans skills/, builds prompt addition
  plugins/                 # existing — typed wrappers (skill artifact #1)
  web/
    app.py                 # adds /api/training/* + /ws/training
    static/
      index.html           # existing chat
      training.html        # NEW — training-mode UI
skills/                    # NEW — knowledge notes (skill artifact #3)
  fabfilter_proq3.md
  fruity_compressor.md
  (one per acquired skill)
tests/                     # existing — wrapper tests (skill artifact #2)
docs/
  training-mode.md         # this doc
```

`learning_loop.py` is a separate module from `loop.py` because the tool
surfaces and prompts are genuinely different. They can share lower-level
plumbing (Anthropic client, tool dispatch, compaction) via small
helpers; no duplication of agent core.

### Training-agent tool surface (~12 tools)

**FL probe / calibration:**
- `enumerate_plugin_params(track, slot)` — wraps the existing script
- `set_param_and_dwell(track, slot, param_id, value, dwell_s)` — drive a
  single value, sleep, return
- `request_user_readback(prompt, expected_unit)` — pauses the agent
  loop until UI posts a value via `/ws/training`
- `classify_param(track, slot, param_id)` — sets two values, asks for
  two readbacks, decides continuous vs enum
- `fit_curve(samples, candidate_shapes)` — least-squares against each
  shape; returns ranked list with `(shape, params, r_squared)`
- `validate_fit(track, slot, param_id, fit, n_probes=2, tolerance)` —
  picks unsampled values, drives them, asks for readback, checks
  prediction. Returns pass/fail + per-probe deltas.

**Code edit (sandboxed to whitelisted paths only):**
- `read_repo_file(path)`
- `write_repo_file(path, content)` — does NOT touch disk; queues a
  proposed write reviewed by user via UI
- `apply_proposed_writes(approval_token)` — flushes the queue to disk
  after user clicks Approve
- `run_pytest(path_filter)` — runs in-process, returns structured result
- `propose_commit(message, files)` — stages + previews; user clicks
  Approve to commit. Never pushes.

### Sandbox rules

Writable paths whitelist (rejected outside this set, no glob escapes):
- `src/studiomind/plugins/*.py`
- `src/studiomind/plugins/*.json`
- `tests/test_*.py`
- `skills/*.md`
- `src/studiomind/agent/tools.py` (append-only edit mode — see below)

Append-only mode for `tools.py`: the writer accepts only a "register
new tool" delta — a tool name, schema, and executor binding — and
rejects any edit that touches existing tool definitions. This keeps
the existing tool surface intact when the training agent adds one.

Hard `NEVER`s — even with user approval, training mode refuses to
write or stage:
- Anything outside the repo working tree
- `.git/`, `.claude/`, dotfile configs
- `~/.ssh`, `~/.config`, `~/.bashrc`, `~/.zshrc` etc.
- Anything in `~/obsidian-vault` (vault writes go through a separate
  flow we already have, not the training tool surface)

Operational `NEVER`s:
- Never commits without user approval
- Never pushes — push remains explicit human action per global rule
- Never rewrites git history (no amends, no rebases)

### Skill manifest

`skills/<name>.md`:

```markdown
---
type: plugin_wrapper
plugin_name: Fruity Limiter
fl_version: "21.2.10"
acquired: 2026-04-30
wrapper_module: studiomind.plugins.fruity_limiter
test_module: tests.test_fruity_limiter
tool_name: set_limiter
calibration_log: training-logs/fruity_limiter-2026-04-30T15-22-08.json
---

# Fruity Limiter

## Capabilities
Brick-wall limiting for masters and bus loudness control.

## Parameter shapes (calibrated 2026-04-30)
- CEILING: linear [-60, 0] dB, R²=0.9998
- LEVEL: linear [-12, +24] dB, R²=0.9999
- RELEASE: linear [0, 4000] ms, R²=0.9994

## Use cases
- **Master bus** (last in chain): ceiling -0.3 dBTP, level +3 to +6 dB
  for "modern loud" masters.
- **Mid-mix bus glue**: ceiling -3 dBTP, level +1 to +2 dB.
- **Drum bus crush**: short release (~50 ms), level +6 to +9 dB.

## Gotchas
- Sub-bass (<60 Hz) can hit limiter early — high-pass before for
  cleaner ceilings.
- "Release" knob doesn't affect peak limiter, only the level stage.
```

The skill registry (`skill_registry.py`) on session start:
1. Scans `skills/*.md`
2. Validates the wrapper module + test module + tool name exist
3. Builds a "Skills" section appended to the mixing system prompt with
   each skill's `## Use cases` and `## Gotchas` blocks (capped — only
   skills the agent might use this session, ranked by recency or
   relevance to the active project)
4. Registers the typed tool with the agent loop

This is the **mechanism by which teaching translates to "the agent now
knows X."** Skills are pluggable and shareable.

### Curve fitter

Six samples → multiple candidate fits → pick best.

Candidate shapes:
1. **Linear** `v = a*p + b` (2 params)
2. **Log** `v = a*log(p+ε) + b` (2 params; ε small)
3. **Power** `v = a*(p+ε)^b + c` (3 params)
4. **Exponential** `v = a*exp(b*p) + c` (3 params)
5. **Quadratic** `v = a*p² + b*p + c` (3 params)

For each shape: least-squares fit, compute residual sum of squares,
compute R² = 1 - SS_res / SS_tot.

Pick by:
1. Highest R²
2. **BUT** if shape with N+1 params beats N-param shape by less than
   0.001 R², prefer the simpler shape (Occam).
3. If best R² < 0.99: REFUSE TO PROCEED. Ask user for 3 more samples at
   the param values where residuals are largest. Re-fit.

This is the lesson from Fruity Compressor's ratio: the 2-point
"quadratic" fit had perfect R² (impossible not to with 2 points and 3
DoF) but was the wrong shape. Six points + R² gating + simplest-wins
catches that class of mistake.

### Validation gate

After generating the wrapper but **before** commit:

1. Pick two unsampled param values per axis (e.g., 0.13 and 0.71)
2. Drive them via the wrapper's `human_to_param` function
3. Ask user for FL readbacks
4. Compute relative error vs wrapper's `param_to_human(value_we_set)`
5. **Pass** if all probes within tolerance:
   - Continuous: `abs(predicted - actual) < max(0.5, 0.01 * predicted)`
   - Enum: exact string match
6. **Fail** → ask for 3 more samples at the failing region; re-fit;
   re-validate

Probes are random per session; not the same two points each time.
Logged so a future audit can re-run them.

### Calibration logs

`~/StudioMind/training-logs/<plugin>-<iso-timestamp>.json`:

```json
{
  "plugin": "Fruity Limiter",
  "fl_version": "21.2.10",
  "session_started": "2026-04-30T15:18:00+03:00",
  "session_finished": "2026-04-30T15:31:42+03:00",
  "params": [
    {
      "id": 0,
      "name": "Ceiling",
      "kind": "continuous",
      "samples": [
        {"param_value": 0.0, "displayed": "-60.0 dB", "displayed_value": -60.0},
        {"param_value": 0.2, "displayed": "-48.0 dB", "displayed_value": -48.0},
        {"param_value": 0.4, "displayed": "-36.0 dB", "displayed_value": -36.0},
        {"param_value": 0.6, "displayed": "-24.0 dB", "displayed_value": -24.0},
        {"param_value": 0.8, "displayed": "-12.0 dB", "displayed_value": -12.0},
        {"param_value": 1.0, "displayed":  "0.0 dB",  "displayed_value":  0.0}
      ],
      "fits_attempted": [
        {"shape": "linear", "params": [60.0, -60.0], "r_squared": 1.0},
        {"shape": "log",    "params": [...],          "r_squared": 0.97}
      ],
      "selected_fit": {"shape": "linear", "params": [60.0, -60.0]},
      "validation_probes": [
        {"param_value": 0.13, "predicted": -52.2, "actual": -52.2, "ok": true},
        {"param_value": 0.71, "predicted": -17.4, "actual": -17.4, "ok": true}
      ]
    }
  ],
  "files_written": [
    "src/studiomind/plugins/fruity_limiter.py",
    "tests/test_fruity_limiter.py",
    "skills/fruity_limiter.md"
  ],
  "commit_sha": "a1b2c3d"
}
```

Logs serve three purposes:
1. **Debugging.** If the mixing agent later mispredicts using the
   wrapper, we can re-fit from the original samples without
   re-rendering the FL session.
2. **Re-verification.** When FL version changes, replay the same param
   probes and check whether the readbacks still match.
3. **Skill auditing.** "When did this skill get added, what was the FL
   version, what was the R²?"

## Web UI shape

`/training` is a single page with three regions:

```
┌──────────────────────────────────────────────────────────────────┐
│  Mode:  ( • Train  /  Mix )                              [ Stop ] │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Left panel: structured progress                                  │
│  - Current step (1/4: enumerate / 2/4: calibrate / ...)           │
│  - Per-param status (waiting / sweeping / fitted / validated)     │
│  - Live curve plots as fits land                                  │
│                                                                   │
│  Right panel: agent chat + user input                             │
│  - Agent narrates each step                                       │
│  - User inputs: free text + numeric readback boxes during sweeps  │
│  - Approve / Reject buttons for diffs and commits                 │
│                                                                   │
│  Bottom: diff viewer (collapsed by default; expands when proposed)│
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

WebSocket `/ws/training` messages mirror `/ws` but with a couple new
payload types:
- `request_readback` — agent → UI: "render this input box and wait"
- `readback_value` — UI → agent: user's typed value
- `proposed_diff` — agent → UI: render diff viewer + approve buttons
- `proposed_commit` — agent → UI: render commit preview + approve

## Risks / things we'll watch for

1. **Param-classification false positives** — heuristic for
   continuous vs enum is fragile. Mitigation: agent always asks
   *"this looks continuous, confirm?"* before a 6-point sweep.
2. **Multi-section plugins** — Fruity Limiter has internal stages (EQ
   pre, comp, peak). v1 treats all params as one flat list; the
   wrapper just exposes them all. Grouping happens manually in the
   knowledge note, not in code structure. v2 might generate
   sub-sections.
3. **Sweep dwell race conditions** — if user is slow to read a knob,
   the next sweep step may have moved on. Mitigation: agent waits for
   readback **before** advancing the next step, dwell is a minimum not
   a maximum.
4. **Code-edit failure modes.** Generated wrapper compiles but breaks
   subtly (e.g. `decode_state` reads wrong key). Mitigation: tests are
   generated alongside the wrapper, run before commit; validation
   probe gives a behavioral check that pure unit tests can't.
5. **Skill bloat in mixing prompt.** Once we have 20+ skills, the
   appended knowledge sections will balloon the system prompt.
   Mitigation: skill loader filters to skills "relevant" to the
   project (heuristic: plugins actually loaded on tracks). Hard cap
   at ~10 skills' worth of knowledge in any one session.
6. **FL version drift** — a wrapper calibrated on FL 21.2.10 may be
   wrong on 22.x. Mitigation: skill manifest records FL version; on
   mismatch, mixing agent flags "skill calibration is from a different
   FL version, results may drift" and a re-verify command can be run.

## Out of scope for v1

- Mixing-technique training (no plugin involved — just verbal
  knowledge capture). v2.
- Sharing / importing skills from other StudioMind users.
- Training mode in CLI; web only.
- Auto-detection of plugin from FL state (user names it).
- Skill versioning / migrations between FL versions (just records the
  version, doesn't migrate).
- Cross-skill dependencies (e.g., "set_master_chain" calls
  "set_limiter"). Each skill stays atomic.
- LLM-side caching across sweep dialog. (Inherits the agent loop's
  existing prompt-cache behavior, no special handling.)

## Phased delivery plan

| Phase | Scope | Acceptance |
|-------|-------|------------|
| **P0** | This design doc + ADR | Reviewed, approved |
| **P1** | Curve fitter library + tests | 6-point fit picks correct shape on synthetic data for all 5 candidate shapes; R² gating works |
| **P2** | Sandboxed code-edit tools + pytest runner + tests | Can read/write/commit a no-op file in a clean test repo; refuses paths outside whitelist |
| **P3** | Skill registry + manifest loader | Existing Pro-Q 3 + Fruity Comp wrappers wrapped as skills with `skills/<name>.md`; mixing-mode prompt picks them up |
| **P4** | Training agent loop + system prompt | Driving sweeps over MIDI bridge end-to-end; logs to `training-logs/` |
| **P5** | Web UI: `/training` page + WebSocket | Limiter acquisition runs cleanly start-to-finish |
| **P6** | First real acquisition: Fruity Limiter | Wrapper + tests + skill note committed; mixing agent demonstrably uses `set_limiter` next session |

Phases are sequential. P1 and P2 are pure-Linux work; P3-P6 require
live FL on Windows (P6 mandatory; P4/P5 testable with mocks plus a
short live shake-out).

## Open design questions

1. **Should the training agent run on Sonnet or Opus?** Probably Opus
   for code generation (wrappers + tests must be correct) but Sonnet
   for the conversational sweep flow (cheaper, faster, fewer tokens).
   Could route by tool category.
2. **How does the user fix a botched skill mid-session?** "Restart this
   axis" / "Abort this skill" buttons in the UI — but the proposed
   diff is the natural rollback point.
3. **Should we surface the calibration log in the chat UI, or just
   write it silently?** Probably write silently + link in the
   commit-approval screen.

These are not blockers for P1; revisit at the P5 boundary.
