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

### Robustness invariants (call these out so the design choices below make sense)

These are the constraints we're optimising for. Anything that violates one
of these is the wrong call regardless of how much code it saves.

1. **Hermetic skills.** A skill is a self-contained directory. Adding,
   deleting, or replacing a skill must touch *only* that directory plus a
   single registry-validation cache. No surgery on shared files.
2. **Single-source enforcement.** Every sandbox check goes through one
   function. Every git operation goes through one function. Anything
   not explicitly allowed is rejected (fail-closed).
3. **Mode exclusivity.** Mixing and Training cannot run concurrently in
   the same StudioMind instance. Switching mode requires the current
   mode to be idle. Skills installed during a Mixing session are
   ignored until the next session — no hot-reload.
4. **Resumable.** Training sessions persist their state after every
   step. Browser close, websocket drop, FL crash → session can be
   resumed without losing accumulated samples.
5. **Auditable.** Every commit produced by Training mode carries a
   distinguishing trailer that says so. Every skill carries the
   calibration log it was built from.
6. **Idempotent acquisition.** Acquiring the same plugin on the same
   FL version twice produces byte-identical wrapper code (modulo
   whitespace) and the same content hash. Repro is a property, not an
   accident.
7. **Schema-versioned.** Skill manifests carry a `schema_version`. The
   loader refuses to load a skill whose schema is too new or too old
   for the running StudioMind version.

### Layout

Each skill is a self-contained directory under `src/studiomind/skills/`.
**No append-edits to shared files.** No `tools.py` surgery, no
patches to `prompt.py`. The registry walks `skills/` at startup and
discovers everything by reading manifests — adding a skill is a `git
add` of one tree, deleting a skill is `rm -rf` of one tree.

```
src/studiomind/
  agent/
    loop.py                # existing — mixing agent
    learning_loop.py       # NEW — training agent loop
    learning_tools.py      # NEW — calibration + code-edit tools
    learning_prompt.py     # NEW — student-agent system prompt
    sandbox.py             # NEW — single-source path/git enforcement
  skills/                  # NEW — skills directory (replaces plugins/)
    __init__.py            # marker only; never edited by training agent
    _registry.py           # walks subdirs at startup, validates manifests
    fabfilter_proq3/       # retro-wrapped skill (P3)
      __init__.py
      manifest.json        # schema_version, fl_version, content_hash, ...
      wrapper.py           # the typed wrapper module
      tool.py              # tool spec + executor binding
      knowledge.md         # use cases + gotchas (loaded into mixing prompt)
      tests.py             # tests for this skill
      calibration-logs/    # per-acquisition logs
        2026-04-30T15-22-08.json
    fruity_compressor/     # retro-wrapped skill (P3)
    fruity_limiter/        # first acquired skill via training mode (P6)
    ...
  web/
    app.py                 # adds /api/training/* + /ws/training
    static/
      index.html           # existing chat
      training.html        # NEW — training-mode UI
src/studiomind/plugins/    # KEPT as a thin re-export shim during P3 migration
                           # so older code that imports from plugins/ continues
                           # to resolve while we move callers to skills/.
                           # Removed in P3-final once no callers reference it.
docs/
  training-mode.md         # this doc
```

`learning_loop.py` is a separate module from `loop.py` because the tool
surfaces and prompts are genuinely different. They share an
`agent/_core.py` for lower-level plumbing (Anthropic client, tool
dispatch, compaction). No duplication of agent core.

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

**Code edit (sandboxed to one and only one skill directory at a time):**
- `read_repo_file(path)` — read-only; allowed across the repo
- `propose_write(path, content)` — does NOT touch disk; queues a
  proposed write. Path must resolve under `src/studiomind/skills/<name>/`
  for the *current* skill being acquired. Anything else fails.
- `apply_proposed_writes(approval_token)` — flushes the queue to disk
  after user clicks Approve. Token is server-issued and one-shot.
- `run_pytest(skill_name)` — runs `pytest src/studiomind/skills/<name>/tests.py`
  in a subprocess with a 60s timeout. Returns structured result.
- `propose_commit(message, paths, approval_token)` — stages + previews;
  user approval gate. Trailer-tagged. Never pushes.

### Sandbox rules — one assertion, fail-closed

All write/git/test tools route through `agent/sandbox.py`:

- `assert_path_writable(path, current_skill)` — resolves the path to its
  real on-disk location (including symlinks), checks it sits under
  `src/studiomind/skills/<current_skill>/`, rejects anything else.
  No `..` escapes (`os.path.realpath` resolves to absolute first).
  No symlink escapes (rejects if the resolved real path leaves the
  skill directory). Specifically blocks: `.git/`, `.claude/`,
  `~/.ssh`, `~/.config`, `~/obsidian-vault`, anything outside the
  repo working tree.

- `assert_safe_git(args)` — accepts only an explicit allowlist of
  invocations: `git add <skill-paths>`, `git status`, `git diff`,
  `git commit -m <message>`. Rejects: `push`, `rebase`, `reset
  --hard`, `checkout`, `branch -D`, `clean`, anything with `--force`,
  any global config edit. The exact allowlist is unit-tested; tests
  are mandatory before P2 ships.

- `assert_pytest_safe(skill_name)` — pytest invocation must target a
  single skill's tests file. Wildcard runs and `--no-cov` style
  trickery rejected. Pytest runs in a subprocess with `PYTHONHASHSEED=0`
  for reproducibility.

The single-source rule means *every* future tool that touches disk or
git **must** call into `sandbox.py`. Code review checklist line item.

### Mode exclusivity

A `mode_lock.json` in `~/StudioMind/state/` records which mode a
StudioMind instance is in (`mixing` / `training` / `idle`) and the
PID of the running process. Switching modes requires:
1. Current mode is `idle` (no in-flight tool calls), OR
2. The recorded PID is no longer running (lock is stale; reclaim).

The web UI's mode toggle calls `/api/mode` which performs the lock
dance atomically. Clear UI state ("Cannot switch — mixing session
in progress; finish or abort first") if the lock is held.

Skills installed during a running mixing session are **ignored** by
that session. The mixing prompt is built from the registry at
session start, period. Hot-reload is intentionally not supported —
adds complexity and failure modes for a marginal UX win.

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

### Skill structure

Each skill is a directory under `src/studiomind/skills/<name>/` with
five files (plus a calibration-logs subdir):

```
src/studiomind/skills/fruity_limiter/
  __init__.py                    # re-exports wrapper + tool symbols
  manifest.json                  # versioned schema; the source of truth
  wrapper.py                     # typed wrapper (param ↔ human conversions)
  tool.py                        # @tool spec + executor; exports `TOOL`
  knowledge.md                   # use cases + gotchas (markdown body only)
  tests.py                       # pytest tests for the wrapper + tool
  calibration-logs/
    2026-04-30T15-22-08.json     # samples + fits + validation probes
```

`manifest.json` is the source of truth. `knowledge.md`'s frontmatter
is *not* parsed for skill metadata; it's only the human-readable
prompt content. This separation keeps the loader robust against
markdown edits.

```json
{
  "schema_version": 1,
  "name": "fruity_limiter",
  "type": "plugin_wrapper",
  "display_name": "Fruity Limiter",
  "tool_name": "set_limiter",
  "fl_version": "21.2.10",
  "acquired": "2026-04-30T15:22:08+03:00",
  "calibration_log": "calibration-logs/2026-04-30T15-22-08.json",
  "content_hash": "sha256:c39a...",
  "params": [
    {"id": 0, "name": "Ceiling",  "kind": "continuous", "fit": {"shape": "linear", "params": [60.0, -60.0], "r_squared": 0.99987}},
    {"id": 1, "name": "Release",  "kind": "continuous", "fit": {"shape": "linear", "params": [4000.0, 0.0], "r_squared": 0.99940}},
    {"id": 2, "name": "Style",    "kind": "enum",       "values": {"hard": 0.0, "smooth": 0.5, "transparent": 1.0}}
  ],
  "validation_probes": [
    {"param_id": 0, "param_value": 0.13, "predicted": -52.2, "actual": -52.2, "ok": true},
    {"param_id": 0, "param_value": 0.71, "predicted": -17.4, "actual": -17.4, "ok": true},
    {"param_id": 1, "param_value": 0.27, "predicted": 1080,  "actual": 1080,  "ok": true},
    {"param_id": 1, "param_value": 0.83, "predicted": 3320,  "actual": 3320,  "ok": true}
  ]
}
```

`knowledge.md` body (no frontmatter — just the prompt content):

```markdown
# Fruity Limiter

## Capabilities
Brick-wall limiting for masters and bus loudness control.

## Use cases
- **Master bus** (last in chain): ceiling -0.3 dBTP, level +3 to +6 dB
  for "modern loud" masters.
- **Mid-mix bus glue**: ceiling -3 dBTP, level +1 to +2 dB.
- **Drum bus crush**: short release (~50 ms), level +6 to +9 dB.

## Gotchas
- Sub-bass (<60 Hz) can hit limiter early — high-pass before for cleaner ceilings.
- "Release" knob doesn't affect peak limiter, only the level stage.
```

`content_hash` is `sha256(canonical_json(manifest_minus_hash) +
wrapper.py + tool.py + knowledge.md)`. Two acquisitions of the same
plugin on the same FL version should produce the same hash — that's
the idempotency check.

The skill registry (`skills/_registry.py`) on session start:

1. Walks `src/studiomind/skills/*/manifest.json`
2. Validates `schema_version` matches what this StudioMind build
   supports. Skills with a too-new schema are skipped with a clear
   warning ("upgrade studiomind to use skill X"); too-old schemas
   are skipped pending migration.
3. Re-computes `content_hash`; logs a warning if it doesn't match
   the recorded hash (someone edited a skill file by hand —
   tamper-detection, not enforcement).
4. Imports `wrapper.py` + `tool.py`; collects the exported `TOOL`
   from each.
5. Loads `knowledge.md`'s body and concatenates into a "Skills"
   section appended to the mixing system prompt.
6. **No relevance filtering. All valid skills load.** Hard cap of 50
   skills before we re-evaluate; below that, prompt bloat is a
   non-problem. If we cross it, deal with it then with real data.

The mixing agent gets every registered skill's tool added to its
schema and every skill's knowledge appended to its prompt. The
training agent does not get skill tools — it builds them, doesn't
use them.

### Schema versioning + migrations

`schema_version` increments only when a skill manifest field changes
in a backwards-incompatible way. Migration scripts live at
`src/studiomind/skills/_migrations/v<from>_to_v<to>.py` and are
applied by the registry on load: if a skill is at v1 and code is at
v2, run the migration in-memory (do not rewrite the on-disk
manifest until the user explicitly runs `studiomind migrate-skills`).

For v1, schema_version is `1`. We will not break it without a
migration script and a regression test that loads a captured v1
fixture and confirms it migrates cleanly.

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

### Validation gate — four smart probes

After generating the wrapper but **before** commit, probe the live
plugin at four param values that were *not* in the calibration sweep:

1. **Two extremity probes** — near the high-residual ends of the
   sample range. If samples are at p ∈ {0.0, 0.2, 0.4, 0.6, 0.8, 1.0},
   probe at 0.05 and 0.95.
2. **Two cross-shape disagreement probes** — pick the two param
   values where the leading fit shape and the runner-up fit shape
   predict the most different displayed values. Catches overfitting
   to a wrong family even when both fits had high R².

For each probe:
- Drive the param via the wrapper's `*_to_param` function
- Ask user for FL readback
- Compute predicted-vs-actual delta

**Pass** if all four probes are within tolerance:
- Continuous: `abs(predicted - actual) < max(0.5, 0.01 * abs(predicted))`
- Enum: exact string match

**Fail** → ask for 3 more samples at the failing region's midpoints;
re-fit; re-validate. Up to 3 retry rounds before the agent surfaces
"this plugin's curve is too irregular for v1's fitter, escalate to
human" and aborts the acquisition.

Probe selection is deterministic given the sample set (no RNG):
extremity points at fixed offsets, disagreement points by analytic
maximum of `|fit_a(p) - fit_b(p)|`. Same input samples → same probe
points. Reproducibility across sessions is a feature, not a bug.

All probes (samples, predictions, actuals, deltas) land in the
calibration log.

### Calibration logs

`src/studiomind/skills/<name>/calibration-logs/<iso-timestamp>.json`.
Logs travel **with** the skill — committed to the repo alongside the
wrapper, so re-fitting from samples after a wrapper update doesn't
require finding a separate logs directory. One log per acquisition
(re-acquisitions append a new file; previous logs are preserved).

Format:

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

### Resumability

Training mode persists session state to
`~/StudioMind/state/training-session.json` after every step. The file
captures: target plugin, current step, accumulated samples per param,
fits computed so far, validation probes done. If the websocket
disconnects, the browser is closed, FL crashes, or the user kills the
StudioMind process, the next launch of training mode offers:

> Resume Fruity Limiter acquisition?  
> You were on CEILING calibration, point 4 of 6.

Three buttons: **Resume**, **Restart this skill**, **Discard session**.
Resume re-attaches; restart re-runs from the beginning of the current
skill (samples discarded); discard nukes the state and returns to the
"What should I learn?" prompt.

Mid-acquisition FL connection drops surface as a clear error in chat,
not silent extrapolation. The agent does not invent samples to fill
gaps. If FL is gone, the user must reconnect and click Resume.

### Approval tokens

The "user clicks Approve, agent commits" flow is mediated by
server-issued one-shot tokens to prevent prompt-injection scenarios
where a malicious tool result could trick the agent into commiting
without UI approval.

Flow:
1. Agent calls `propose_writes(payload)` or `propose_commit(payload)`.
   Backend stores the payload, generates a `secrets.token_urlsafe(32)`
   nonce, returns it to the agent (which forwards it to the UI via WS).
2. UI renders preview (diff or commit summary). User clicks Approve.
3. UI POSTs `{token, action: 'approve'}` to `/api/training/approve`.
   Backend validates the token: exists, not consumed, not expired
   (10-minute TTL), payload hash matches.
4. Backend marks token consumed, performs the action atomically, posts
   `approved` event back over WS so the agent learns the outcome.
5. Tokens are consumed exactly once. Replay is rejected.

The agent **cannot** apply writes or commits without a valid consumed
token. Tool implementations enforce this — the only way to flip the
state is for the UI to make the approval call.

### Audit trail — commit trailer

Every commit produced by training mode carries a structured trailer:

```
Skill-Acquired-Via: studiomind-training-mode
Skill-Name: fruity_limiter
Skill-Schema-Version: 1
Skill-Content-Hash: sha256:c39a...
Calibration-Log: src/studiomind/skills/fruity_limiter/calibration-logs/2026-04-30T15-22-08.json
FL-Version: 21.2.10
```

`git log --grep='Skill-Acquired-Via'` lists all training-mode commits.
Trailers are validated by the commit tool — agent cannot omit them.

### Reproducibility

Same plugin, same FL version, same readback values → byte-identical
wrapper code (modulo whitespace) and same `content_hash`. The code
generator emits canonical formatting (one fixed style; no random
ordering of dict keys; no relative timestamps inside the wrapper —
those go in the manifest). Curve fits use deterministic numpy with
fixed random seed (irrelevant for least-squares; matters if we ever
add stochastic methods).

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
| **P1** | Curve fitter library + tests | 6-point fit picks correct shape on synthetic data for all 5 candidate shapes; R² gating refuses < 0.99; simplest-shape tiebreak verified; deterministic across runs |
| **P2** | `agent/sandbox.py` + pytest-runner + tests | Single-source path enforcement (allowed paths pass; `..` escapes, symlinks out, `.git`, vault all rejected); git allowlist enforced; pytest-runner invokes subprocess with timeout; tamper attempts in tests prove fail-closed |
| **P3** | Skill registry + retro-wrap existing wrappers | Pro-Q 3 + Fruity Compressor migrated to `src/studiomind/skills/<name>/` with full manifests + content hashes; mixing agent boots and uses both via the registry; `plugins/` becomes a thin re-export shim; full suite still green |
| **P4** | Training agent loop + system prompt + resumability | Mock-driven end-to-end run synthesises sample readbacks, fits, validates, generates wrapper for a fake plugin schema, commits in a test repo. Resume from disk works after kill-and-restart. |
| **P5** | Web UI: `/training` page + `/ws/training` + approval token flow | Tokens issued, validated, consumed-once. UI rejects without token. Mode-lock toggle works. |
| **P6** | First real acquisition: Fruity Limiter | Wrapper + tests + manifest + knowledge committed under `src/studiomind/skills/fruity_limiter/`. `set_limiter` tool surfaces in mixing mode after restart. Calibration log archived in skill dir. |

Phases are sequential. P1, P2, P3, P4 are pure-Linux work (P4 with
mocked MIDI); P5 has Linux + Windows pieces; P6 is the live
acceptance test on Windows.

Each phase ships green tests as part of its acceptance criteria. No
phase is "done" until its tests pass and the existing suite still
passes. We will not skip ahead.

## Decisions resolved (no longer open)

These were initially open questions; the robustness lens picked them.

1. **Single agent loop with mode flag, or two loops?** → **Two loops.**
   `learning_loop.py` and `loop.py` share `agent/_core.py` for the
   plumbing (Anthropic client, compaction, tool dispatch) but the
   agent surfaces are otherwise independent. Tool-set leakage between
   modes is the failure we cannot afford.
2. **Skill registry — relevance filter, or always-load?** →
   **Always-load**, with a hard cap of 50 skills before we revisit.
   Relevance filtering is an optimisation that adds debugging
   complexity ("why doesn't the agent know about my limiter") for a
   marginal token win.
3. **Where do generated wrapper files live?** → **In a hermetic skill
   directory** at `src/studiomind/skills/<name>/`. Adding/removing a
   skill is one tree, no surgery on shared files.
4. **`tools.py` append-only edit vs per-skill `tool.py`?** →
   **Per-skill `tool.py`**, auto-discovered by the registry. No
   shared-file edits during acquisition.
5. **Validation gate — 2 random probes or 4 smart probes?** →
   **4 deterministic smart probes**: 2 extremity + 2 cross-shape
   disagreement. Reproducible across sessions.
6. **Where do calibration logs live?** → **In-skill at
   `src/studiomind/skills/<name>/calibration-logs/`**. Logs travel
   with the skill they describe.
7. **Approval flow — boolean flag or token-gated?** →
   **Server-issued one-shot tokens.** Prompt-injection-safe.
8. **Hot-reload of newly-installed skills mid mixing session?** →
   **No.** Skills load only at session start. Mode exclusivity
   prevents simultaneous training + mixing anyway.
9. **Should the agent run on Sonnet or Opus?** → **Opus for code
   generation, Sonnet for conversational sweep flow.** Route by
   tool category. Cheaper sweeps, careful code.
10. **What about non-deterministic curve fits?** → **Deterministic
    only.** Numpy with fixed seed; canonical code formatting.
    Reproducibility is a guarantee, not a hope.

## Still open (revisit at P5)

1. How granular should the chat UI's "now sweeping CEILING param 4/6"
   progress display be? Single progress bar, per-param breakdown, or
   timeline view?
2. Should knowledge-only "skills" be allowed (no wrapper, just a
   prompt addition)? Useful for capturing genre conventions or
   reference material; but blurs the v1 scope. Lean *no* unless a
   compelling use case appears during P4.
3. Should the registry warn on `content_hash` mismatch (someone hand-
   edited a skill file) loudly or silently? Probably loudly during
   P3-P5, then re-evaluate based on real false-positive rate.
