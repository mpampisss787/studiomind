# FL Agent — Vision & MVP Spec

> **One-liner:** Claude Code, but for music production. An agentic AI producer that lives beside FL Studio, reads your project, makes changes through real tools, verifies its work by listening, and iterates.

---

## 1. Vision

### 1.1 The core insight

The current generation of "AI music tools" (Suno, Stable Audio, LANDR, iZotope Neutron) share a fatal limitation: **they don't live inside the producer's workflow, and they can't see the project as a whole.** They're either black-box generators that spit out finished audio, or static plugins bolted onto a single track.

Claude Code changed software development not because the underlying LLM got smarter, but because it was given **tools, a loop, and access to the real environment**. It reads files, runs commands, sees outputs, fixes errors, iterates. That same pattern — `plan → act → verify → iterate` with real tools against a real environment — has never been applied to music production.

That's the gap. That's the product.

### 1.2 The analogy, made precise

| Claude Code (dev) | FL Agent (music) |
|---|---|
| Reads the codebase | Reads the FL project (channels, patterns, mixer, plugins, routing) |
| Runs tests, reads errors | Renders stems, analyzes audio (FFT, LUFS, transients, key) |
| Edits files | Writes MIDI, sets plugin params, adds EQs/compressors |
| Terminal + filesystem | FL MIDI scripting API + stem rendering + UI automation |
| Git for rollback | Project snapshots, per-action revert |
| "Fix this bug" | "Fix this mix" |
| "Refactor this function" | "Rework this melody" |
| "Add a test for X" | "Add a counter-melody for X" |
| Agentic loop with tools | Same loop, same pattern, audio domain |

### 1.3 Why this works as a product

1. **Extensible by default.** Every new capability is a new tool. The core agent loop never changes.
2. **Open-ended commands.** We don't have to predict every intent — the model composes tools in ways we didn't design for.
3. **Real moat.** The LLM is a commodity. The **tool layer and agent scaffolding inside FL** is the defensible asset, and it compounds: more tools → more capable agent → more users → more tool ideas.
4. **Natural GTM.** The demo video writes itself.

### 1.4 What it is not

- Not a VST plugin. It's a companion application that controls FL from outside.
- Not a generative-only tool. Generation is one capability among many.
- Not a chatbot. It's an agent — it acts, verifies, and iterates.
- Not a replacement for the producer. It's a collaborator, and every action is reversible.

---

## 2. Architecture Overview

### 2.1 The bridge to FL Studio

FL Studio exposes a **Python-based MIDI scripting API** (the same one used for Novation/Akai/Launchpad integrations). It's official, supported, and survives FL updates. Through it we can:

- Read and write: channels, patterns, mixer tracks, plugin parameters, transport, piano roll notes, selection state, BPM, key signature.
- Add and configure stock plugins (Fruity Parametric EQ 2, Fruity Limiter, Fruity Compressor, etc.) on any mixer track.
- Trigger rendering of stems or the master bus.
- Manipulate the playlist and patterns.

What it **does not** expose directly:

- Live audio streams of individual tracks (solved via stem rendering or virtual audio loopback).
- Some deep project metadata (solved via project file parsing when necessary).
- Third-party VST plugin internals (solved via MIDI Learn + parameter IDs).

### 2.2 Component diagram (logical)

```
┌─────────────────────────────────────────────┐
│   FL Agent Companion App (Tauri or Electron)│
│                                             │
│  ┌──────────────┐   ┌────────────────────┐  │
│  │ Chat / UI    │   │  Agent Loop        │  │
│  │ layer        │◄──┤  (plan/act/verify) │  │
│  └──────────────┘   └─────────┬──────────┘  │
│                               │             │
│                     ┌─────────▼──────────┐  │
│                     │  Tool Dispatcher   │  │
│                     └─────────┬──────────┘  │
└───────────────────────────────┼─────────────┘
                                │
           ┌────────────────────┼────────────────────┐
           │                    │                    │
           ▼                    ▼                    ▼
   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
   │ MIDI bridge  │    │ Stem renderer│    │ Audio        │
   │ (virtual     │    │ (triggers FL │    │ analysis     │
   │  MIDI +      │    │  render,     │    │ (FFT, LUFS,  │
   │  FL Python   │    │  reads wavs) │    │  transients) │
   │  script)     │    │              │    │              │
   └──────┬───────┘    └──────┬───────┘    └──────────────┘
          │                   │
          ▼                   ▼
   ┌─────────────────────────────────┐
   │        FL Studio                │
   │  (running, with our Python      │
   │   device script loaded)         │
   └─────────────────────────────────┘
```

### 2.3 Control channels

- **Virtual MIDI port** (loopMIDI on Windows) carries structured commands between the companion app and FL.
- **FL Python device script** (shipped with the app, installed once into FL's `Hardware` folder) receives those commands, executes them against the FL API, and returns structured responses over another MIDI channel or a local socket.
- **Filesystem** is used for stem rendering (FL writes wavs, the app reads them).
- **UI automation** (win32 / pywinauto) is the escape hatch for any operation the scripting API cannot reach.

---

## 3. The Tool Layer

### 3.1 Read tools (non-destructive, fast)

| Tool | Purpose |
|---|---|
| `read_project_state()` | Returns a structured snapshot: BPM, key, channels (name, type, mixer routing), mixer tracks (name, volume, pan, inserts), patterns, playlist layout, selection. |
| `read_channel(channel_id)` | Deep read of a single channel: plugin type, parameter values, notes in its patterns. |
| `read_mixer_track(track_id)` | Insert chain with each plugin's parameters. |
| `read_pattern(pattern_id)` | MIDI notes, timing, velocities per channel. |

### 3.2 Render / analyze tools (the "ears")

| Tool | Purpose |
|---|---|
| `render_stem(mixer_track_id, bars_range)` | Bounces one track to wav, returns file path. |
| `render_master(bars_range)` | Bounces the full mix. |
| `analyze_audio(wav_path)` | Returns spectral profile, LUFS, true peak, transient density, detected key, tempo confidence. |
| `compare_audio(wav_a, wav_b)` | Diff two renders — useful for before/after after an agent action. |
| `detect_masking(stems[])` | Finds frequency ranges where multiple stems fight for the same space. |

### 3.3 Write tools (destructive — require snapshot)

| Tool | Purpose |
|---|---|
| `set_channel_param(channel_id, param, value)` | Volume, pan, mute, solo. |
| `set_mixer_param(track_id, param, value)` | Same for mixer. |
| `add_plugin(track_id, plugin_name, slot)` | Adds a stock plugin to an insert slot. |
| `set_plugin_param(track_id, slot, param_id, value)` | Configures plugin params (EQ bands, compressor threshold, etc.). |
| `write_midi_notes(pattern_id, channel_id, notes[])` | Writes/replaces notes in a pattern. |
| `set_bpm(value)` / `set_key(value)` | Project-level changes. |

### 3.4 Safety tools

| Tool | Purpose |
|---|---|
| `snapshot(label)` | Captures full project state before a risky operation. |
| `revert(snapshot_id)` | Rolls back to a previous snapshot. |
| `list_snapshots()` | Shows snapshot history with labels and timestamps. |
| `diff_snapshots(a, b)` | Human-readable summary of what changed. |

### 3.5 Meta tools

| Tool | Purpose |
|---|---|
| `ask_user(question, options?)` | The agent requests clarification when ambiguous. |
| `report(summary)` | Structured summary of what was done, used for UI display. |

---

## 4. The Agent Loop

```
while not done:
    1. model receives: user goal + project state summary + prior action log
    2. model decides: next tool call (or "done" + final report)
    3. dispatcher executes tool, returns result
    4. if destructive action:
         auto-snapshot before executing
         show preview in UI, optionally gate on user confirm
    5. append result to action log
    6. if the tool was a render/analyze step:
         model compares against goal, decides whether to iterate
    7. repeat
```

Key design principles:

- **Preview by default.** First-run users see every planned action before execution. Trusted users can switch to auto mode per-session.
- **Every action is reversible.** Snapshots before any mutating operation.
- **The agent listens to its own work.** After any audible change, the agent re-renders and analyzes to verify the outcome matches the goal.
- **The user can interrupt at any point.** Pause button stops the loop cleanly.

---

## 5. MVP Scope

### 5.1 The MVP thesis

Ship the smallest tool set that enables **one jaw-dropping demo**: *"Open a rough project, type 'mix this professionally', watch the agent work."*

### 5.2 MVP tool set (6 tools)

1. `read_project_state()`
2. `render_stem(mixer_track_id, bars_range)`
3. `analyze_audio(wav_path)`
4. `add_plugin(track_id, "Fruity Parametric EQ 2")`
5. `set_plugin_param(track_id, slot, param_id, value)`
6. `snapshot()` / `revert()`

### 5.3 Out of MVP scope (explicitly)

- MIDI generation / melody writing
- Arrangement building
- Third-party VST control
- Style references
- Multi-session memory
- Compression, reverb, saturation tools (EQ only for MVP)
- Mobile / remote control

---

## 6. Roadmap Beyond MVP

| Phase | Focus |
|-------|-------|
| Phase 2 | Compression, sidechain, reverb/delay, stereo width, automation |
| Phase 3 | MIDI generation (melody, bassline, drums, fills) |
| Phase 4 | Arrangement (structure, transitions, variations) |
| Phase 5 | Style references, user palette, genre presets |
| Phase 6 | Third-party VST, community skills, other DAWs |
