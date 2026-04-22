# StudioMind

> Claude Code, but for music production.

An agentic AI producer that lives beside FL Studio. It reads your project, makes changes through real tools, verifies its work by listening, and iterates.

## Status

**Pre-development** — API research phase

## Architecture

```
Companion App (Tauri)
  ├── Chat UI
  ├── Agent Loop (plan → act → verify → iterate)
  └── Tool Dispatcher
        ├── FL Bridge (Python device script via virtual MIDI)
        ├── Stem Renderer (triggers FL render, reads wavs)
        └── Audio Analyzer (FFT, LUFS, transients, key)
```

## Project Structure

```
studiomind/
├── docs/           # Vision, specs, API reference
├── research/       # API research, prototyping notes
├── src/
│   ├── agent/      # Agent loop, tool dispatcher, LLM integration
│   ├── bridge/     # FL Studio communication (MIDI + Python script)
│   ├── analyzer/   # Audio analysis (FFT, LUFS, transients)
│   └── ui/         # Companion app frontend
└── scripts/        # FL Studio Python device scripts
```

## MVP

EQ-only mixing agent. 6 tools. One demo: "Open a rough project, type 'mix this professionally', watch the agent work."

See [docs/VISION.md](docs/VISION.md) for the full spec.
