# Vertical-slice tests: end-to-end mixing with verification

The single most important thing StudioMind does is **change a mix based on
measurements it made itself**. Every other feature supports this loop. Until
each new write tool works end-to-end in a real FL project, that tool is
just a parameter setter — not a mixing tool.

This doc is the manual test recipe that verifies the loop. Run the relevant
slice after any change that touches the write tools (`set_builtin_eq`,
`set_proq3`, `set_compressor`, `set_mixer_volume`) or the Plan/Verify prompt
steps.

There are currently **three slices**:

1. **EQ slice** — `set_builtin_eq` / `set_proq3`. Verifies the band-targeted
   write path against spectral_balance deltas.
2. **Compressor slice** — `set_compressor` (Fruity Compressor). Verifies the
   dynamics write path against crest_factor / LRA / RMS-vs-LUFS deltas.
3. **Sidechain slice** — `apply_sidechain`. Verifies the routing write path
   creates the send via the FL API and emits a clean advisory for the
   plugin-wrapper sidechain-source dropdown that the user has to confirm
   by hand.

## Preconditions (all slices)

- FL Studio 2025 running with a real project open (not an empty template).
- MIDI bridge connected — `ping` works.
- StudioMind web UI running and the project workspace is active.
- Render flow is **user-assisted**: when the agent calls `prepare_*_render`,
  read its instruction back to yourself, hit Ctrl+R in FL, choose
  `Tracks (separate audio files)` for the batch / `Master (single file)` for
  the master, and save into the printed folder. The watcher picks the files
  up, auto-analyses them, and the agent's `collect_*_renders` returns once
  every pending entry is READY. Auto-render was removed in Arc 1
  (2026-04-28) — there is no Ctrl+R automation any more.

---

## Slice A — EQ (band cut on a single track)

Pick one mixer track whose role you know: a kick, a bass, a piano. "Piano"
is used below; substitute your track.

### Test prompt

Paste verbatim:

> Cut 2 dB at 300 Hz on the piano track with Q=1.5. Snapshot first. Before
> the write, tell me your predicted low_mid band delta. Then apply the
> change, re-render just that track plus the master, and show me the
> before/after numbers side by side. Write a one-line history entry when
> you're done.

### What a passing run looks like

1. **Orient:** agent calls `get_workspace_status` + `read_project_history` +
   `detect_external_changes` + `read_recent_decisions` +
   `read_user_preferences` in its first turn.
2. **Measure:** if stems are fresh, agent reuses them. If not, it runs
   `prepare_batch_render` → reads the FL Ctrl+R instruction to you →
   `collect_all_renders` for initial numbers on the piano track + master.
3. **Plan:** agent states a concrete predicted delta. Example: *"I'm
   cutting 2dB at 300Hz Q=1.5 via the built-in EQ mid band. Expected:
   low_mid band drops ~1.5dB, low/mid bands mostly unchanged."*
4. **Snapshot:** agent calls `snapshot("pre-300Hz cut on piano")`.
5. **Execute:** agent calls `set_builtin_eq` (or `set_proq3` if loaded).
6. **Verify:** agent calls `refresh_staleness` — piano + master flagged
   stale. Agent calls `prepare_stem_render(piano_track_id)` +
   `prepare_master_render`, reads the FL instruction, you Ctrl+R, then
   `collect_all_renders`.
7. **Report:**
   ```
              Before   After    Delta
   Piano low_mid  +3.2 dB  +1.7 dB  -1.5 dB   ✓ matches prediction
   Master LUFS    -9.6     -9.8              slight headroom
   ```
8. **Record:** `write_history_entry` with the numbers.

### Things to check (EQ slice)

- [ ] Predicted delta within ~0.5 dB of actual.
- [ ] No duplicate `get_workspace_status` calls mid-run.
- [ ] `decisions.json` has a new record with `tool: "set_builtin_eq"` and
      `outcome: "pending"`.
- [ ] Re-run the same prompt 10 min later: agent reuses the fresh analyses
      for Measure (no batch render kicked off).
- [ ] Say "revert that" — `revert` is called, `decisions.json` flips to
      `outcome: "reverted"`.

---

## Slice B — Compressor (dynamics on a track)

Load **Fruity Compressor** on one mixer track before starting (vocal bus,
drum bus, or bass — pick something that has visible dynamic range). Note
the track ID and the slot the comp is in.

### Test prompt

Paste verbatim, substituting the track name and slot:

> Add gentle bus compression to the drum bus. Use Fruity Compressor in slot
> 0 — threshold around -12 dB, ratio 2:1, attack ~10 ms, release ~80 ms,
> +1 dB makeup. Snapshot first. Before the write, tell me your predicted
> crest_factor and LRA deltas on that bus. Then apply the change, re-render
> just that track, and show me the before/after numbers side by side. Write
> a one-line history entry.

### What a passing run looks like

1. **Orient:** same as Slice A. Agent also calls `read_mixer_track` on the
   target track to confirm Fruity Compressor is loaded in the slot you
   specified and reports the current parameter values.
2. **Measure:** if the drum-bus stem is fresh, reuse it. Else
   `prepare_stem_render(drum_bus_id)` → user Ctrl+R → `collect_render`.
3. **Plan:** agent states predicted deltas. Example: *"crest_factor drops
   ~3 dB (transients squashed), LRA drops ~1 LU, RMS rises ~0.5 dB."*
4. **Snapshot:** `snapshot("pre-comp on drum bus")`.
5. **Execute:** `set_compressor(track_id, slot=0, threshold_db=-12,
   ratio=2.0, attack_ms=10, release_ms=80, gain_db=1.0)`.
6. **Verify:** `refresh_staleness` flags the drum bus + master stale;
   `prepare_stem_render` + `prepare_master_render` → you Ctrl+R →
   `collect_all_renders`.
7. **Report:**
   ```
                       Before    After    Delta
   Drum bus crest_factor  14.8 dB  11.2 dB  -3.6 dB  ✓ within band
   Drum bus LRA            6.2 LU   5.0 LU  -1.2 LU  ✓
   Drum bus RMS          -14.3 dB -13.8 dB  +0.5 dB  ✓
   ```
8. **Record:** `write_history_entry` summarising the comp settings + delta.

### Things to check (compressor slice)

- [ ] Predicted crest_factor delta within ~1.5 dB of actual. Comp behaviour
      is more variable than EQ — wider band than the EQ slice is fine.
- [ ] `read_mixer_track` after the write shows threshold / ratio / attack /
      release / gain values that **round-trip back** to the requested
      human values via the typed wrapper's reverse converters.
- [ ] `decisions.json` record has `tool: "set_compressor"`,
      `outcome: "pending"`, and the human values in `params`.
- [ ] If the agent passes ratio < 1.0 or threshold > 0 dB, the wrapper
      clamps to the valid range — no silent failures.
- [ ] Say "revert that" — comp is bypassed / parameters return to baseline,
      `decisions.json` flips to `outcome: "reverted"`.

---

## Slice C — Sidechain (kick → bass duck)

Pre-load **Fruity Compressor** on the bass (or synth bus) track that you
want ducked. Note both track IDs.

### Test prompt

Paste verbatim, substituting the track names:

> Sidechain the bass to the kick — gentle pump, ~6 dB duck on the
> downbeats. Snapshot first. Use apply_sidechain, then read me the
> advisory verbatim so I can finish the wire in FL. After I confirm
> I've picked Kick from the bass-comp's sidechain-source dropdown,
> tune the comp's threshold and release for that ~6 dB target depth,
> re-render the bass + master, and report the side_balance / bass-LUFS
> shift before vs. after.

### What a passing run looks like

1. **Orient:** same as Slices A/B, plus `read_mixer_track` on both kick
   and bass to confirm names, slot of the bass comp, and that no
   pre-existing send already routes kick → bass.
2. **Snapshot:** `snapshot("pre-sidechain kick → bass")`.
3. **Wire (API half):** `apply_sidechain(source_track=<kick>,
   target_track=<bass>)`. Tool returns
   `advisory_status: "ready_for_dropdown"` and the advisory string —
   agent reads it to you verbatim. You right-click the bass comp's
   sidechain-source dropdown in FL and pick the kick. Reply "done" in
   chat.
4. **Tune (set_compressor):** agent calls `set_compressor` on the bass
   with conservative starting values (e.g., threshold -18 dB, ratio
   3:1, attack 5 ms, release 80 ms) and explains the prediction
   (~6 dB pump on each kick hit, faster release = punchier).
5. **Verify:** `refresh_staleness` flags bass + master. User Ctrl+R on
   each. `collect_all_renders`.
6. **Report:**
   ```
                      Before     After     Delta
   Bass low      +3.1 dB   +1.5 dB   -1.6 dB   ✓ duck visible
   Bass LUFS    -10.2     -11.0     -0.8 LU   ✓ headroom freed
   Master peak   -0.3 dB   -0.8 dB             ✓ kick punches through
   ```
7. **Record:** `write_history_entry`.

### Things to check (sidechain slice)

- [ ] `apply_sidechain` returns `advisory_status: "ready_for_dropdown"`
      because Fruity Compressor was pre-loaded.
- [ ] Advisory text actually names both tracks and the comp slot — agent
      reads it back unedited.
- [ ] If you re-run with the same source/target, tool reports
      `send_already_existed: true` and does not duplicate the route.
- [ ] If you intentionally remove Fruity Compressor before running, tool
      reports `advisory_status: "needs_comp_loaded"` and the advisory
      tells you to add a comp first.
- [ ] `decisions.json` has a record with `tool: "apply_sidechain"`,
      `outcome: "pending"`, including the source/target track IDs.
- [ ] FL undo after the run cleanly removes the send (agent's snapshot
      maps to FL's `general.saveUndo` call inside `_handle_set_send`).

---

## Known failure modes

- **Agent re-renders every turn:** prompt regression. Measure step in
  `prompt.py` still says "do NOT re-render" when stems are fresh.
- **No predicted delta in Plan:** prompt regression. Plan step requires
  expected spectral / dynamic delta, not just the parameter change.
- **`set_*` returns success but nothing changes:** FL parameter ID drift.
  Call `read_mixer_track` to verify the actual param value moved. For the
  compressor: if the param IDs in `fruity_compressor_params.json` were
  enumerated against an older FL version, re-run `enumerate_plugin_params`
  to refresh.
- **Comp slice: predicted delta is way off:** the typed wrapper's
  attack/release/ratio calibration is approximate (FL doesn't publish the
  exact normalized→ms / normalized→ratio curves). If actual values move
  reasonably but in the wrong magnitude, the calibration constants in
  `src/studiomind/plugins/fruity_compressor.py` need tuning against a
  measurement run — see the module docstring.
- **Sidechain slice: dropdown isn't there:** FL's plugin-wrapper
  sidechain-source dropdown only lists tracks that *send to* the target.
  If `apply_sidechain` succeeded and the dropdown still shows nothing,
  the route didn't actually fire — call `read_mixer_track` on the source
  and confirm `routing` lists the target. (Older FL versions silently
  no-op `setRouteToLevel` but `setRouteTo` always works.)
- **Sidechain slice: comp doesn't duck after picking the source:** the
  comp's threshold may be set so high that the kick never crosses it.
  Drop the threshold by 6-12 dB and re-listen. If nothing changes, the
  user may have selected the wrong track in the dropdown — the
  advisory's source-track name should match exactly.

## History

- 2026-04-23 — first drafted after the reliability batch (retry on
  transient failures, decisions log, user preferences). Phase 1 EQ MVP
  declared shipped.
- 2026-04-28 (Arc 1) — auto-render path removed; render flow rewritten
  here as user-assisted Ctrl+R + watcher ingest.
- 2026-04-28 (Phase 2 kickoff) — compressor slice added alongside EQ
  slice; `set_compressor` typed wrapper shipped.
