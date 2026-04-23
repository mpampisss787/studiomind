# StudioMind

> **One-liner:** Claude Code, but for music production. An agentic AI producer that lives beside FL Studio.

## Quick Info

| Key | Value |
|-----|-------|
| **Project Dir** | `~/studiomind` |
| **Status** | Live on Windows — ping + state round-trip verified over MS MIDI Services loopback |
| **Target DAW** | FL Studio (Windows, x64 — also runs under Prism on Win11 ARM64) |
| **Stack** | Python CLI · Claude API (Sonnet/Opus) · SysEx over MS MIDI Services loopback · FL Python device script |
| **MVP Scope** | EQ-focused mixing agent (12 tools, Pro-Q 3 support) |
| **Lines of Code** | ~2400 across 15 Python files |
| **Tests** | 7 protocol tests passing |

## Architecture

```
Python CLI / Agent Loop
  ├── Claude API (tool use) — plan → act → verify → iterate
  ├── Tool Executor — dispatches to FL bridge or local analysis
  └── MIDI Client (python-rtmidi)           <-- companion uses endpoint (A)
        ↕ SysEx over MS MIDI Services Basic MIDI 1.0 Loopback
        ↕ (cross-wired: A writes appear at B reads, and vice versa)
FL Studio                                    <-- FL attaches to endpoint (B)
  └── device_StudioMind.py (MIDI Controller Script)
        ├── OnSysEx() → decode → dispatch → respond
        └── 13 command handlers (read/write/safety)
```

### Key Files

| File | Purpose |
|------|---------|
| `scripts/device_StudioMind.py` | FL Studio controller script (self-contained, 420 lines) |
| `src/studiomind/protocol.py` | SysEx encode/decode/chunk protocol |
| `src/studiomind/bridge/midi_client.py` | MIDI I/O (python-rtmidi, threaded) |
| `src/studiomind/bridge/commands.py` | Typed `FLStudio` class |
| `src/studiomind/workspace.py` | Project folder + session.json manifest + staleness |
| `src/studiomind/agent/loop.py` | Core agent loop with Claude tool use |
| `src/studiomind/agent/tools.py` | 10 tool schemas + `ToolExecutor` |
| `src/studiomind/agent/prompt.py` | Mixing engineer system prompt (3800 chars) |
| `src/studiomind/analyzer/spectral.py` | FFT, LUFS, spectral balance, masking detection |
| `src/studiomind/cli.py` | CLI: ports, ping, state, eq, agent, chat, shell |

### Workspace (per-project folders)

One FL project maps to one StudioMind project folder, mirroring the FL project name:

```
~/StudioMind/projects/<ProjectName>/
    stems/              - per-track renders, deterministic filenames (overwritten)
    masters/            - timestamped master renders (history kept for A/B)
    references/         - drag-dropped reference tracks
    .studiomind/
        session.json    - manifest: every render + status + fl_state_hash + analysis
```

**Invariants:**
- Stem filenames are `track_{id:03d}_{slug}.wav`, derived from FL state. User and agent cannot disagree about which file is which track.
- `session.json` is the single source of truth for "what audio do I have and is it still fresh." Agent reads it at session start; does not rely on chat-history memory.
- Each render is tagged with a hash of the relevant FL state at render time. When FL state changes, stems whose track changed are flagged `stale` — agent refuses to trust stale analysis and re-renders.

## MVP Tools

**Read (safe):** `read_project_state`, `read_mixer_track`, `read_channel`, `analyze_audio`
**Workspace (safe):** `get_workspace_status`, `refresh_staleness`
**Render (user-assisted):** `prepare_batch_render` (preferred), `prepare_stem_render`, `prepare_master_render`, `collect_render`, `collect_all_renders`
**Write (destructive — require snapshot):** `set_builtin_eq`, `set_proq3`, `set_plugin_param`, `set_mixer_volume`, `set_mixer_pan`
**Safety:** `snapshot`, `revert`

### Render flow

The agent doesn't auto-render — FL's Python API doesn't expose export. Instead:
1. Agent calls `prepare_batch_render` → solos/unsolos the right tracks, writes pending manifest entries.
2. Agent speaks the instruction ("Ctrl+R → Tracks (separate audio files) → save into `stems/`").
3. User does one FL export. File-watcher sees WAVs land, fuzzy-matches them to mixer tracks by slug.
4. Agent calls `collect_all_renders` → blocks until every pending render is READY, returns analyses.

This is the path for initial full-mix analysis. `prepare_stem_render` + `collect_render` is for targeted re-checks after a single change.

## FL Commands (14)

`ping`, `get_project_name`, `read_project_state`, `read_mixer_track`, `read_channel`, `set_eq`, `get_eq`, `set_plugin_param`, `get_plugin_params`, `set_mixer_param`, `snapshot`, `revert`, `transport`, `get_bpm`.

## Plugin Profiles

| Plugin | File | Status |
|--------|------|--------|
| FabFilter Pro-Q 3 | `src/studiomind/plugins/fabfilter_proq3.py` | Complete — 10 bands, Hz/dB/Q conversions |

## API Constraints (Discovered)

- `add_plugin()` is NOT in FL API → use built-in 3-band EQ (`mixer.setEqGain/Frequency/Bandwidth`)
- `render/bounce` is NOT in FL API → need pywinauto UI automation
- MIDI notes not accessible from controller scripts → use PyFLP for offline parsing
- FL runs its MIDI scripts in a Python 3.12 **sub-interpreter**. That blocks `_ctypes`, `_socket.socket()` construction, and parts of `tempfile` (confirmed via `scripts/device_probe.py`). Named-pipe / TCP server inside FL is impossible. `os`, `threading`, `subprocess` import OK. Stick with SysEx.
- Built-in EQ params are normalized 0.0-1.0 (gain 0.5 = unity/0dB)
- VST `getParamCount` always returns 4240 — check param names to find real ones
- loopMIDI / teVirtualMIDI driver fails to install on Win11 25H2 (kernel refuses signature). Use Microsoft MIDI Services + Basic MIDI 1.0 Loopback plugin (rc-3) instead — Microsoft-signed, zero third-party driver.

## Development Status

1. ~~API Research~~ — Complete, 1000-line reference doc in vault
2. ~~SysEx Protocol~~ — Complete, 7 tests passing
3. ~~FL Device Script~~ — Complete, 13 commands
4. ~~MIDI Client~~ — Complete, threaded async
5. ~~Agent Loop~~ — Complete, Claude tool use + preview gate
6. ~~Windows round-trip test~~ — Live on Win11 25H2 ARM64 via MS MIDI Services loopback (2026-04-23)
7. ~~Workspace data model~~ — Project folders + session.json + staleness hashing (2026-04-23, 15 tests)
8. ~~Render loop Phase 1b~~ — prepare_batch_render + file-watcher + collect_all_renders (2026-04-23, 44 tests)
9. **Live end-to-end test on Windows** ← NEXT
10. Memory + sample library — user.json, decisions.json, samples/ fingerprints
11. Vertical slice ("Cut 2dB at 300Hz on piano")
12. Full MVP ("Mix this professionally")

## Windows Setup (driver-free)

1. Install **Microsoft MIDI Services Runtime + Tools** (rc-4) + **Basic MIDI 1.0 Loopback plugin** (rc-3) from `github.com/microsoft/MIDI/releases`. x64 installers run under Prism on ARM64.
2. Enable **Windows Developer Mode** (`Settings → For developers`) before running the loopback installer — preview MSIX requires it.
3. Open **Windows MIDI Settings** app → "Finish MIDI Setup" → defaults create `Default App Loopback (A)` / `(B)`.
4. FL Studio: F10 → attach `Default App Loopback (B)` as both Input and Output with controller type `StudioMind Agent Bridge`, same port number on both rows.
5. Install Python 3.12 **x64** (`python-3.12.x-amd64.exe` from python.org — winget may pick ARM64 which has no `python-rtmidi` wheels).
6. `pip install -e .` → all x64 wheels resolve.
7. `python -m studiomind ping`. Override endpoint names via `STUDIOMIND_MIDI_IN` / `STUDIOMIND_MIDI_OUT` env vars if you're on loopMIDI or renamed endpoints.

## DO NOT

- Do not auto-execute destructive actions without snapshot
- Do not hardcode FL version-specific parameter IDs — use version detection
- Do not assume virtual MIDI driver is installed — handle gracefully
- Do not ship third-party VST support in MVP — stock plugins only
- Do not use `setChannelPitch(mode=2)` — it's BROKEN in the FL API
