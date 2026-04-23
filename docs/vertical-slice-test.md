# Vertical-slice test: end-to-end EQ with verification

The single most important thing StudioMind does is **change a mix based on
measurements it made itself**. Every other feature supports this loop. Until it
works end-to-end in a real FL project, StudioMind is just a very good audio
analyzer.

This doc is the manual test that verifies the loop. Run it after any change
that touches auto-render, the write tools (`set_builtin_eq`, `set_proq3`,
`set_mixer_volume`), or the Plan/Verify prompt steps.

## Preconditions

- FL Studio 2025 running with a real project open (not an empty template).
- MIDI bridge connected — `ping` works.
- Auto-render has been run *once* manually so FL remembers Mode + format for
  the render-settings dialog. (Known gap: stage-2 Mode automation, see
  `docs/autorender-stage2.md`.)
- StudioMind web UI running and the project workspace is active.
- Pick one mixer track whose role you know: a kick, a bass, a piano. "Piano"
  is used below; substitute your track.

## The test prompt

Paste this into the chat, verbatim:

> Cut 2 dB at 300 Hz on the piano track with Q=1.5. Snapshot first. Before the
> write, tell me your predicted low_mid band delta. Then apply the change,
> re-render just that track plus the master, and show me the before/after
> numbers side by side. Write a one-line history entry when you're done.

## What a passing run looks like

1. **Orient:** agent calls `get_workspace_status` + `read_project_history` +
   `detect_external_changes` + `read_recent_decisions` + `read_user_preferences`
   in its first turn.
2. **Measure:** if stems are fresh, agent reuses them. If not, it runs
   `prepare_batch_render` → `collect_all_renders` for initial numbers on the
   piano track + master.
3. **Plan:** agent states a concrete predicted delta. Example: *"I'm cutting
   2dB at 300Hz Q=1.5 via the built-in EQ mid band. Expected: low_mid band
   drops ~1.5dB, low/mid bands mostly unchanged."*
4. **Snapshot:** agent calls `snapshot("pre-300Hz cut on piano")`.
5. **Execute:** agent calls `set_builtin_eq` (or `set_proq3` if loaded) with
   the mid band parameters.
6. **Verify:** agent calls `refresh_staleness` — piano + master should be
   flagged stale. Agent then calls `prepare_stem_render(piano_track_id)` +
   `prepare_master_render` + `collect_all_renders`.
7. **Report:** agent gives a concrete table:
   ```
              Before   After    Delta
   Piano low_mid  +3.2 dB  +1.7 dB  -1.5 dB   ✓ matches prediction
   Master LUFS    -9.6     -9.8              slight headroom
   ```
8. **Record:** agent calls `write_history_entry` with the numbers. You should
   see the entry appear under History in the sidebar.

## Things to check

- [ ] The predicted delta is within ~0.5 dB of the actual delta. If it's
      consistently off, the agent's mental model of EQ shapes is wrong.
- [ ] No duplicate `get_workspace_status` calls in the middle of the run —
      agent should cache its orient results.
- [ ] `decisions.json` in the project's `.studiomind/` folder contains a new
      record with `tool: "set_builtin_eq"` and `outcome: "pending"`.
- [ ] Re-run the same prompt 10 min later: agent reuses the fresh analyses
      for the Measure step instead of kicking off a new batch render.
- [ ] Say "revert that" — agent calls `revert`, and the decision in
      `decisions.json` flips to `outcome: "reverted"`.

## Known failure modes

- **Agent re-renders every turn:** prompt regression. Check that the Measure
  step in `prompt.py` still says "do NOT re-render" when stems are fresh.
- **No predicted delta in Plan:** prompt regression. Check that the Plan step
  requires expected spectral delta, not just the parameter change.
- **set_builtin_eq returns success but nothing changes:** FL parameter ID
  drift. Call `read_mixer_track` to verify the actual param value moved.
- **Stage-2 render-settings dialog doesn't confirm:** FL hasn't remembered
  Mode. Open FL's export dialog manually once, pick Mode, Start. Subsequent
  auto-renders inherit that setting until FL forgets it (rarely).

## History

This test was first drafted 2026-04-23 after completing the reliability batch
(retry on transient failures, stoppable auto-render, silent/ok status,
decisions log, user preferences). Running it is how we declare the Phase 1
MVP actually shipped.
