# StudioMind

> **One-liner:** Claude Code, but for music production. An agentic AI producer that lives beside FL Studio.

## Quick Info

| Key | Value |
|-----|-------|
| **Project Dir** | `~/studiomind` |
| **Status** | Core infrastructure complete — needs Windows FL Studio testing |
| **Target DAW** | FL Studio (Windows) |
| **Stack** | Python CLI · Claude API (Sonnet/Opus) · SysEx over virtual MIDI · FL Python device script |
| **MVP Scope** | EQ-focused mixing agent (12 tools, Pro-Q 3 support) |
| **Lines of Code** | ~2400 across 15 Python files |
| **Tests** | 7 protocol tests passing |

## Architecture

```
Python CLI / Agent Loop
  ├── Claude API (tool use) — plan → act → verify → iterate
  ├── Tool Executor — dispatches to FL bridge or local analysis
  └── MIDI Client (python-rtmidi)
        ↕ SysEx over loopMIDI virtual port
FL Studio
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
| `src/studiomind/agent/loop.py` | Core agent loop with Claude tool use |
| `src/studiomind/agent/tools.py` | 10 tool schemas + `ToolExecutor` |
| `src/studiomind/agent/prompt.py` | Mixing engineer system prompt (3800 chars) |
| `src/studiomind/analyzer/spectral.py` | FFT, LUFS, spectral balance, masking detection |
| `src/studiomind/cli.py` | CLI: ports, ping, state, eq, agent, chat, shell |

## MVP Tools (12)

**Read (safe):** `read_project_state`, `read_mixer_track`, `read_channel`, `analyze_audio`
**Render (safe):** `render_and_analyze` (master, stem, or full_mix mode — triggers FL export via pywinauto)
**Write (destructive — require snapshot):** `set_builtin_eq`, `set_proq3`, `set_plugin_param`, `set_mixer_volume`, `set_mixer_pan`
**Safety:** `snapshot`, `revert`

## Plugin Profiles

| Plugin | File | Status |
|--------|------|--------|
| FabFilter Pro-Q 3 | `src/studiomind/plugins/fabfilter_proq3.py` | Complete — 10 bands, Hz/dB/Q conversions |

## API Constraints (Discovered)

- `add_plugin()` is NOT in FL API → use built-in 3-band EQ (`mixer.setEqGain/Frequency/Bandwidth`)
- `render/bounce` is NOT in FL API → need pywinauto UI automation
- MIDI notes not accessible from controller scripts → use PyFLP for offline parsing
- FL Python has NO sockets, filesystem, or subprocess — SysEx over MIDI only
- Built-in EQ params are normalized 0.0-1.0 (gain 0.5 = unity/0dB)
- VST `getParamCount` always returns 4240 — check param names to find real ones

## Development Status

1. ~~API Research~~ — Complete, 1000-line reference doc in vault
2. ~~SysEx Protocol~~ — Complete, 7 tests passing
3. ~~FL Device Script~~ — Complete, 13 commands
4. ~~MIDI Client~~ — Complete, threaded async
5. ~~Agent Loop~~ — Complete, Claude tool use + preview gate
6. **Windows round-trip test** ← NEXT
7. Vertical slice ("Cut 2dB at 300Hz on piano")
8. Full MVP ("Mix this professionally")

## DO NOT

- Do not auto-execute destructive actions without snapshot
- Do not hardcode FL version-specific parameter IDs — use version detection
- Do not assume virtual MIDI driver is installed — handle gracefully
- Do not ship third-party VST support in MVP — stock plugins only
- Do not use `setChannelPitch(mode=2)` — it's BROKEN in the FL API
