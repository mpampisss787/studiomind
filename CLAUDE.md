# StudioMind

> **One-liner:** Claude Code, but for music production. An agentic AI producer that lives beside FL Studio.

## Quick Info

| Key | Value |
|-----|-------|
| **Project Dir** | `~/studiomind` |
| **Status** | Pre-development — API research phase |
| **Target DAW** | FL Studio (Windows) |
| **Stack** | Tauri (Rust + Web frontend) · Python FL bridge · Claude API |
| **MVP Scope** | EQ-only mixing agent (6 tools) |

## Architecture

```
Companion App (Tauri)
  ├── Chat UI
  ├── Agent Loop (plan → act → verify → iterate)
  └── Tool Dispatcher
        ├── MIDI Bridge → FL Python device script
        ├── Stem Renderer → triggers FL render, reads wavs
        └── Audio Analyzer → FFT, LUFS, transients, key
```

### Control Channels
- **Virtual MIDI** (loopMIDI) — structured commands between app and FL
- **FL Python device script** — installed in FL's `Hardware` folder, executes API calls
- **Filesystem** — stem rendering (FL writes wavs, app reads them)
- **UI automation** (pywinauto) — escape hatch for unreachable API operations

## MVP Tools (6)

1. `read_project_state()` — project snapshot (BPM, key, channels, mixer, patterns)
2. `render_stem(mixer_track_id, bars_range)` — bounce one track to wav
3. `analyze_audio(wav_path)` — spectral, LUFS, peak, transients, key
4. `add_plugin(track_id, "Fruity Parametric EQ 2")` — add stock EQ
5. `set_plugin_param(track_id, slot, param_id, value)` — configure EQ bands
6. `snapshot()` / `revert()` — safety net

## Development Phases

1. **API Research** — Map FL Python scripting API surface ← CURRENT
2. **Spike: MIDI round-trip** — Prove bridge plumbing works
3. **Spike: stem render** — Trigger FL render from companion app
4. **Tool schemas** — JSON schemas for 6 MVP tools
5. **Agent loop** — LLM + tool dispatcher + preview gate + chat UI
6. **Vertical slice** — "Cut 2dB at 300Hz on the piano" end-to-end
7. **Full MVP** — "Mix this professionally" flow

## Key Design Principles

- **Preview by default** — show plan before executing
- **Every action reversible** — auto-snapshot before mutations
- **Agent listens to its work** — re-render + analyze after changes
- **User can interrupt** — pause stops the loop cleanly

## DO NOT

- Do not auto-execute destructive actions without snapshot
- Do not hardcode FL version-specific parameter IDs — use version detection + param map
- Do not assume virtual MIDI driver is installed — handle gracefully
- Do not ship third-party VST support in MVP — stock plugins only
