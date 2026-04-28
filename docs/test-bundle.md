# Test Bundle — Windows session playbook

Run this end-to-end in one focused sitting (~60-90 min). It covers (1)
installing / updating StudioMind on a Windows machine, (2) smoke-testing
the bridge, (3) the three vertical slices (EQ, compressor, sidechain),
(4) parameter-enumeration runs that unblock the next four typed
wrappers, and (5) a free-form exercise to surface emergent issues.

Capture the requested data at each step — that's what the next coding
session will be tuning against.

---

## 0. Install or update

### First-time install on a fresh Windows machine

Follow `CLAUDE.md` § "Windows Setup (driver-free)" verbatim:

1. Install **Microsoft MIDI Services Runtime + Tools** (rc-4) +
   **Basic MIDI 1.0 Loopback plugin** (rc-3) from
   `github.com/microsoft/MIDI/releases`. x64 installers run under Prism
   on ARM64.
2. Enable **Windows Developer Mode** (`Settings → For developers`)
   *before* running the loopback installer — preview MSIX requires it.
3. Open **Windows MIDI Settings** → "Finish MIDI Setup" → defaults
   create `Default App Loopback (A)` / `(B)`.
4. FL Studio: F10 → attach `Default App Loopback (B)` as both Input
   and Output, controller type `StudioMind Agent Bridge`, same port
   number on both rows.
5. Install Python 3.12 **x64** (`python-3.12.x-amd64.exe` from
   python.org — winget may pick ARM64 which has no `python-rtmidi`
   wheels).
6. `git clone <studiomind-remote> studiomind && cd studiomind`
7. `pip install -e .`
8. Copy `scripts/device_StudioMind.py` into FL's device-script folder:
   `Documents/Image-Line/FL Studio/Settings/Hardware/StudioMind/device_StudioMind.py`
   (note the `StudioMind` subfolder — direct placement under `Hardware/`
   does not load).
9. Restart FL → it auto-loads the script the next time you attach the
   `Default App Loopback (B)` controller.

### Update an existing install

```powershell
cd path\to\studiomind
git pull
pip install -e .
```

Then **copy the updated device script** into FL's hardware folder:

```powershell
copy scripts\device_StudioMind.py "$env:USERPROFILE\Documents\Image-Line\FL Studio\Settings\Hardware\StudioMind\device_StudioMind.py" /Y
```

This update adds a new SysEx command (`set_send`) — without copying the
fresh device script, `apply_sidechain` will return "Unknown method:
set_send" from FL's side.

Restart FL (or detach + re-attach the controller in F10) to reload the
device script.

---

## 1. Smoke

### 1a. Bridge ping

```powershell
python -m studiomind ping
```

**Pass:** `ok: True, fl_version: ...` round-trips in under a second.

**Fail symptom → fix:**

- *No MIDI ports found* → MIDI Services not installed or Developer Mode
  not on. Re-run step 0.1-0.2.
- *Timeout* → FL not running, or `Default App Loopback (B)` not
  attached. Open F10, confirm.
- *Unknown method: set_send* in any later step → device script wasn't
  refreshed (step 0 update path).

### 1b. Project state round-trip

```powershell
python -m studiomind state
```

**Pass:** prints BPM, channels, mixer tracks, plugin slots populated.

**Capture:** total mixer-track count + BPM. (We'll use this to know
which track ID to put in the slice prompts below.)

### 1c. Web UI

```powershell
python -m studiomind web
```

Open `http://localhost:8040` in a browser. The chat box should be
visible and the sidebar should list your active project.

**Pass:** chat accepts a "ping" message and the agent responds.

---

## 2. Slice A — EQ (sanity baseline)

Full recipe: `docs/vertical-slice-test.md` Slice A.

Pick one mixer track whose role you know (e.g., piano on track 5).

Paste into chat:

> Cut 2 dB at 300 Hz on track 5 with Q=1.5. Snapshot first. Before the
> write, tell me your predicted low_mid band delta. Then apply the
> change, re-render just that track plus the master, and show me the
> before/after numbers side by side. Write a one-line history entry
> when you're done.

**Capture:**

- Before/after table the agent prints.
- Predicted low_mid delta vs actual delta.
- Whether the agent re-rendered any unrelated tracks (it shouldn't).

**Pass criteria:**

- Predicted delta within ~0.5 dB of actual.
- `decisions.json` (in `~/StudioMind/projects/<ProjectName>/.studiomind/`)
  has a fresh `set_builtin_eq` entry with `outcome: "pending"`.

Then say "revert that" and confirm the entry flips to
`outcome: "reverted"`.

---

## 3. Slice B — Compressor (calibration acceptance test) ⭐

This is the most valuable test for development. The compressor wrapper
ships with **approximate** continuous-param curves. This test produces
the data we tune them against.

Full recipe: `docs/vertical-slice-test.md` Slice B.

### Preparation

Load **Fruity Compressor** on the **drum bus** track (or, if no drum
bus, any percussion-rich track with visible dynamics). Note the track
ID and the slot the comp is in (e.g., track 6 slot 0).

Open Fruity Compressor's UI and screenshot the four knob positions
**before** running the test (so we can compare to where the agent
moves them).

### Test prompt

> Add gentle bus compression to track 6 (drum bus). Use Fruity
> Compressor in slot 0 — threshold around -12 dB, ratio 2:1, attack
> ~10 ms, release ~80 ms, +1 dB makeup. Snapshot first. Before the
> write, tell me your predicted crest_factor and LRA deltas on that
> bus. Then apply the change, re-render just that track, and show me
> the before/after numbers side by side. Write a one-line history
> entry.

### Capture (this is the calibration data ⭐)

After the agent finishes, screenshot Fruity Compressor's UI again
**and** record the actual knob readouts FL displays (right-click each
knob → "edit value").

For each of the four continuous knobs, report:

| Knob       | Value the agent intended | What FL actually shows |
|------------|--------------------------|------------------------|
| Threshold  | -12 dB                   | ?                      |
| Ratio      | 2:1                      | ?                      |
| Attack     | 10 ms                    | ?                      |
| Release    | 80 ms                    | ?                      |
| Gain       | +1 dB                    | ?                      |

Also capture from the agent's report:

- Predicted vs actual `crest_factor` delta.
- Predicted vs actual `lra_lu` delta.
- The full before/after table.

**Pass criteria:**

- Each knob's actual FL value within **±20%** of the requested value
  (the wrapper's curves are approximate by construction; the goal here
  is "is it in the right ballpark, and how much do we need to tune?").
- Predicted crest_factor delta within ~1.5 dB of actual.

### Re-running with extreme values

Run a second comp test to probe the curve shape:

> Now squash track 6 hard — threshold -30 dB, ratio 8:1, attack 1 ms,
> release 200 ms, +3 dB makeup. Snapshot, predict the crest_factor
> drop, apply, re-render, report.

Capture the knob round-trip table the same way. Two data points per
axis is enough to fit a curve correction.

---

## 4. Slice C — Sidechain (dropdown verification)

Full recipe: `docs/vertical-slice-test.md` Slice C.

Pre-load **Fruity Compressor** on the **bass** (or synth bus) track.

Paste:

> Sidechain track 4 (bass) to track 5 (kick) — gentle pump, ~6 dB
> duck on the downbeats. Snapshot first. Use apply_sidechain, then
> read me the advisory verbatim so I can finish the wire in FL.
> After I confirm I've picked Kick from the bass-comp's
> sidechain-source dropdown, tune the comp's threshold and release
> for that ~6 dB target depth, re-render the bass + master, and
> report the side_balance / bass-LUFS shift before vs. after.

### Capture

- The exact advisory string the agent reads (paste back).
- Whether FL's plugin-wrapper sidechain-source dropdown actually
  populates the kick after `apply_sidechain` finishes (open Fruity
  Compressor on bass → click the wrench / sidechain icon → check the
  dropdown).
- Before/after `side_balance.low` and bass `lufs_integrated`.

**Pass criteria:**

- Advisory mentions both track names + the comp slot, no placeholders
  like `track ?`.
- The dropdown is populated (this confirms `mixer.setRouteTo` actually
  wired the send — without this, the FL plugin wrapper has nothing to
  list).
- After picking Kick from the dropdown and re-rendering, `bass.lufs`
  drops by the predicted amount on the kick hits (visible in the
  spectrogram or the side-band shift).

### Idempotency check

Re-run the exact same prompt a second time. The tool should return
`send_already_existed: true` and the advisory should still be sane.

### No-comp fallback

Bypass / remove Fruity Compressor on bass and re-run. The tool should
return `advisory_status: "needs_comp_loaded"` with an advisory telling
you to add a comp first.

---

## 5. Param enumeration runs (unblocks next 4 wrappers) ⭐

Each of these is a **60-second** task that produces a JSON file. Once
committed, the next coding session can ship typed wrappers for all
four. Without these, the next session is blocked.

For each plugin:

1. Load the plugin on any free mixer track in FL.
2. Note the track ID and slot.
3. Run the enumerator:

```powershell
python scripts/enumerate_plugin_params.py --track <n> --slot <s> --name <plugin_name>
```

The script writes `src/studiomind/plugins/<plugin_name>_params.json`.

### The four to run

| FL plugin                    | `--name` argument             | Why next             |
|------------------------------|-------------------------------|----------------------|
| Fruity Limiter               | `fruity_limiter`              | Master-bus + comp use; multi-section. |
| Fruity Reeverb 2             | `fruity_reeverb_2`            | First send-effect; introduces aux pattern. |
| Fruity Delay 3               | `fruity_delay_3`              | Same shape as reverb. |
| Fruity Stereo Enhancer       | `fruity_stereo_enhancer`      | Width control; pairs with stereo analysis. |

### Capture

For each: paste back the enumerator's stdout (lists each `id, name,
default_value`) — even though it's also written to disk, having it
inline in the chat lets the next session validate the structure
quickly.

After all four run, commit the JSONs:

```powershell
git add src/studiomind/plugins/*_params.json
git commit -m "Enumerate param IDs for Phase 2 plugin batch"
```

---

## 6. Drop-zone smoke (Arc 3 verification)

Open the web UI. Drag-drop one file each:

- A **stem** (an FL-named WAV like `MyProject_Kick.wav`) → should land
  in `stems/` with the `track_NNN_<slug>.wav` rename, watcher slug-
  matches it.
- A **reference track** (a full song, mp3 or wav, > 60 s, stereo) →
  should land in `references/`.
- A **mystery sample** (a one-shot, < 5 s) → should land in `drops/`.
- A **previous bounce** (filename contains "master" or "mix") → should
  land in `masters/`.

### Capture

For each: which folder it actually landed in vs the table above. Smart-
default routing is heuristic; if anything is misrouted, the chat
should offer an override pill — confirm that works.

---

## 7. Free-form exercise

Pick a real WIP project (or the test-project recipe from
`docs/test-project-recipe.md` if you don't have one ready). Open it in
FL. Active the workspace (`python -m studiomind project <name>`),
launch the web UI, and paste:

> Mix this professionally. Render the stems and the master, find the
> three biggest issues, propose fixes, snapshot, apply each, re-render,
> and verify. Stop and ask me to pick if you have a judgement call to
> make.

This is open-ended on purpose — the goal is to surface emergent issues
that scripted tests miss.

### Capture

- Anything weird the agent did (re-rendered when it shouldn't have,
  stalled, bad recommendation, etc.) — paste the relevant chat slice.
- Anything the agent *couldn't* do but seemed like it should be able
  to — those are the next-tool candidates.
- Total wall-clock time from "Mix this professionally" to "done" — gives
  us a baseline for the agent's current pacing.

---

## 8. Reporting back

When you're done, paste back into chat:

1. Pass/fail status for each numbered section above.
2. The compressor calibration table (Slice B section's "what FL
   actually shows" column) for both the gentle and the squashed run.
3. The four enumerator JSON contents (or just confirm the files exist
   in `src/studiomind/plugins/`).
4. The free-form exercise's emergent-issues list.

That set is everything the next coding session needs to:

- Refine the compressor wrapper's curves against real FL behaviour.
- Ship typed wrappers for Limiter / Reeverb 2 / Delay 3 / Stereo
  Enhancer.
- Triage the next round of agent-behaviour fixes.
