# All-in-one test prompt

Single self-contained prompt for the chat that exercises every Phase 2
agent feature shipped today (set_compressor, apply_sidechain,
read-back-for-calibration). UI features (Drops sidebar, universal Move
menu, FL drag-drop fix) are tested manually alongside — the inline
checklist below covers them.

Total wall-clock time once FL is open and a project is active: ~25-40
minutes, of which ~10-15 minutes is the user clicking around.

---

## Pre-flight

1. FL Studio open with a real project (not blank).
2. **On the drum bus (or any percussion-rich track), pre-load a Fruity
   Compressor in slot 0.** Right-click any drum track's mixer slot →
   add Fruity Compressor. Note the track ID + slot.
3. **On the bass (or any synth bus you want ducked), pre-load a Fruity
   Compressor in slot 0.** Note the track ID + slot.
4. **Note the kick track's ID** (it'll be the sidechain source).
5. `python -m studiomind web` running, browser open at
   `http://localhost:8040`, sidebar shows your project name.

You'll need three track IDs in hand:

- KICK_ID = ___
- DRUM_BUS_ID = ___ (with Fruity Comp in slot 0)
- BASS_ID = ___ (with Fruity Comp in slot 0)

Substitute those numbers into the prompt below.

---

## The chat prompt

Paste this verbatim, with the three IDs filled in:

> Run a Phase 2 stress test on this project. Take it step by step,
> snapshot before every destructive write, and stop after each step
> for me to confirm before moving on.
>
> 1. **Orient.** Show me the workspace status, then call
>    `read_mixer_track` on tracks <KICK_ID>, <DRUM_BUS_ID>, and
>    <BASS_ID>. Report the names, the plugins on each, and the current
>    threshold/ratio/attack/release/gain values on the two Fruity
>    Compressors.
>
> 2. **Gentle bus comp on the drum bus.** Snapshot first. Then
>    `set_compressor(track_id=<DRUM_BUS_ID>, slot=0, threshold_db=-12,
>    ratio=2.0, attack_ms=10, release_ms=80, gain_db=1.0, knee="hard")`.
>    Before you call it, predict the crest_factor and LRA deltas you
>    expect on that bus. After applying, IMMEDIATELY call
>    `read_mixer_track(<DRUM_BUS_ID>)` again and tell me what FL now
>    shows for each of the five values you just wrote — this is
>    calibration data, I need actual-vs-requested for both runs.
>    Then have me Ctrl+R that one track in FL, and report the
>    measured before/after.
>
> 3. **Stop and ask me to confirm step 2 before moving on.**
>
> 4. **Sidechain wire.** `apply_sidechain(source_track=<KICK_ID>,
>    target_track=<BASS_ID>, send_level=0.8)`. Read me the advisory
>    string verbatim. Don't continue until I reply "done" — I need to
>    pick the kick from the bass-comp's sidechain-source dropdown in
>    FL myself.
>
> 5. **Pump the bass.** Now that the sidechain is wired, set the bass
>    comp for a clear ~6 dB pump:
>    `set_compressor(track_id=<BASS_ID>, slot=0, threshold_db=-18,
>    ratio=4.0, attack_ms=2, release_ms=120, gain_db=2.0, knee="hard")`.
>    Snapshot first. Predict the bass-side LUFS shift and side_balance
>    change. Apply, then ask me to Ctrl+R the bass and master, then
>    report.
>
> 5b. **Reference compare (drop-zone exercise).** I want the master to
>    feel more like a commercial pop master. ASK ME to drop a
>    reference track — be specific about what you want (a finished
>    song with the target sound) and where to drop it. Then STOP and
>    wait for me to confirm. Once I say it's in, locate it via
>    `get_workspace_status`, run `compare_to_reference(track_path=<my
>    current master path>, reference_path=<the reference>)`, and tell
>    me the three biggest deltas with concrete suggestions on what to
>    fix on the master. Do NOT apply any of those fixes yet — this
>    step is just verifying the ask-for-drop loop works end-to-end.
>
> 6. **Squash the drum bus hard** (extreme-values calibration data).
>    Snapshot. `set_compressor(track_id=<DRUM_BUS_ID>, slot=0,
>    threshold_db=-30, ratio=8.0, attack_ms=1, release_ms=200,
>    gain_db=3.0, knee="smooth")`. Predict the crest_factor crash
>    (probably -6 to -8 dB). Apply, IMMEDIATELY read_mixer_track again
>    and tell me FL's actual values for each of the five params (this
>    is the second calibration data point — extreme-end of the range).
>    Have me Ctrl+R, report.
>
> 7. **Idempotency check.** Re-call `apply_sidechain` with the same
>    source/target as step 4. Confirm the response says
>    `send_already_existed: true` and the advisory is sane.
>
> 8. **Revert.** Revert step 6 (the squash). Confirm the decisions.json
>    record for that step flips to `outcome: "reverted"`.
>
> 9. **Summary.** Three things:
>    - The two calibration tables (gentle + squash): for each
>      parameter, what I asked for vs what FL actually showed.
>    - Predicted vs actual deltas across all four destructive steps.
>    - Anything weird you noticed — extra renders, slow tool calls,
>      misroutes, the agent re-orienting unnecessarily.

---

### How the workflow actually feels

The mechanic at every "ask the user for something" pause is:

1. Agent prints its question and **the chat input unlocks** (you see
   the spinner stop and your textbox is editable again). That's the
   visual signal that the agent ended its turn cleanly — exactly what
   you want.
2. Drop the reference file onto the chat page (or sidebar). A pill
   appears (`📎 ref.wav → references/`). The Drops/References
   sidebar count ticks up.
3. Type "done" and hit send. The agent resumes with full prior
   conversation memory and a single `get_workspace_status` call to
   discover where the file landed.

The drop itself goes through a separate HTTP endpoint
(`/api/workspace/upload`), so you can drop the file even WHILE the
agent is still talking — it'll be on disk and analysed by the time
the agent gets to step 3.

### What "passing" looks like for step 5b

- The agent's request is **specific**: it names what kind of file
  (e.g. "a finished commercial track in the same genre"), the
  preferred format (WAV / FLAC / MP3 OK), and where to drop it
  (the chat page anywhere, OR the sidebar's References zone).
- The agent **ends its turn cleanly** — no `get_workspace_status`,
  no `analyze_audio`, no "let me just check" tool calls hanging off
  the ask. Visual signal: the chat input unlocks and the spinner
  stops as soon as the agent prints its question.
- After you drop the file and reply "done", the agent recovers the
  new path from `get_workspace_status` (or the file_dropped pill in
  chat) and uses it directly in `compare_to_reference`.
- The agent doesn't ask you to "render" the dropped reference — the
  watcher analyzes drops automatically and the cache is warm.
- The deltas are concrete (per-band dB diff + a one-line summary
  per band), with suggestions tied to actual EQ moves.

### Failure modes to watch for in step 5b

- **Agent asks AND keeps tool-calling** → spinner stays on, chat
  input stays locked, you can't reply. This means Claude generated a
  trailing tool call after the ask (e.g. a redundant
  `get_workspace_status`) — the prompt's "end your turn cleanly,
  text only" rule got ignored. Capture the exact tool sequence from
  the session log; this is a real prompt-regression.
- Agent doesn't ask, makes up a path, tool errors out → the
  "ask for drops" guidance isn't being followed at all.
- Agent asks but is vague ("send me audio") → prompt could be
  stronger; capture the exact text it used.
- Agent re-renders all stems after the drop lands → confused; only
  the new file needs analysis, the existing stems are unchanged.

If you DO get stuck because the agent won't end its turn, hit the
Stop button (top-right of the chat) — that fires `POST /api/stop`
and breaks the agent loop on its next safe boundary. Then tell me
what you saw.

---

## Manual UI checklist (do alongside the chat run)

While the agent works, exercise the UI fixes in the browser:

- [ ] **Drag a stem WAV from FL onto the chat page.** Verify it lands
      in `drops/` (NOT references). The Drops sidebar section should
      appear with the file listed. Pre-fix this would have gone to
      `references/`.
- [ ] **Click the ⤴ button on a Drops row.** Menu opens
      *inside* the viewport (clamped — earlier bug had it shoot off
      the right edge of the narrow sidebar). Pick "Move to references/"
      → file disappears from Drops, appears in References within ~1s.
- [ ] **Hover any Stem row.** A faint ⤴ button appears next to (or
      where) the row's actions live. Click it → same menu, this time
      offering masters/references/drops. Don't actually move it
      unless you want to.
- [ ] **Hover a Master row.** Same ⤴ button shows on hover. Click
      → menu opens.
- [ ] **Hover a Reference row.** ⤴ button alongside the existing ×
      delete button. Both work.
- [ ] **Drop a file with `ref_` in the name** (e.g. rename one of the
      stems on disk to `ref_kick.wav`, drop it). Verify it correctly
      lands in `references/` (filename keyword path).
- [ ] **Drop a file with `master` in the name.** Verify it lands in
      `masters/`.
- [ ] **Drop a 2-second mono WAV.** Verify it lands in `drops/` with
      classification reason `short_mono_likely_sample`.

For the chat-side, watch for:

- [ ] Pills appearing inline in chat for every dropped file.
- [ ] Click a chat pill → its menu opens inside the chat pane (also
      viewport-clamped now).
- [ ] When the agent does long renders, the meta-line counts in the
      sidebar (`X stems · Y masters · Z refs · N drops`) update
      live without needing manual refresh.

---

## After everything: send me the log

```powershell
python -m studiomind debug-bundle
git add debug/
git commit -m "All-in-one test session log"
git push
```

That copies the most recent session log (every tool call, MIDI
exchange, classifier decision, ingest event) from
`~/StudioMind/logs/` into `<repo>/debug/`, then pushes it to GitHub.
The next coding session reads from `debug/` directly.

If you ran multiple `studiomind` invocations across the session
(e.g. closed and reopened web, ran some `state` calls, etc.), bundle
all of them: `python -m studiomind debug-bundle --last 10`.
