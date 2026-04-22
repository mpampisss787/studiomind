# StudioMind — Revised Architecture

> Based on comprehensive FL Studio MIDI Scripting API research (2026-04-22).

---

## 1. Revised MVP Tool Set

The original spec assumed `add_plugin()` and `render_stem()` were possible via the API. They are not. Here is the revised approach:

### Original → Revised

| Original Tool | Status | Revised Approach |
|---|---|---|
| `read_project_state()` | **Works** | Channels, mixer, patterns, transport — all readable |
| `render_stem()` | **NOT in API** | OS-level automation (Ctrl+Shift+R) + read wav from filesystem |
| `analyze_audio()` | **Works** | Companion app reads rendered wavs, runs FFT/LUFS analysis |
| `add_plugin("Fruity Parametric EQ 2")` | **NOT in API** | **PIVOT:** Use built-in 3-band EQ on every mixer track (`mixer.setEqGain/Frequency/Bandwidth`) |
| `set_plugin_param()` | **Works** | `plugins.setParamValue(value, paramIdx, idx, slotIdx)` — any plugin parameter |
| `snapshot()` / `revert()` | **Works** | `general.saveUndo()` / `general.undo()` |

### Revised MVP Tools (7)

1. **`read_project_state()`** — BPM, channels, mixer tracks, routing, plugin info
2. **`set_builtin_eq(track_id, band, gain, frequency, bandwidth)`** — Built-in 3-band EQ on every mixer track
3. **`set_plugin_param(track_id, slot, param_id, value)`** — Set any plugin parameter (for existing plugins like Parametric EQ 2 if already loaded)
4. **`set_mixer_param(track_id, param, value)`** — Volume, pan, mute, solo, stereo separation
5. **`render_and_analyze(track_ids?, bars_range?)`** — Orchestrated: trigger FL render via UI automation → wait for file → analyze audio
6. **`snapshot(label)`** / **`revert()`** — FL's undo system (`saveUndo` / `undo`)
7. **`ask_user(question, options?)`** — Agent requests clarification

### The "Built-in EQ" Pivot — Why This Is Better

Every FL Studio mixer track has a **built-in 3-band parametric EQ** accessible via:
- `mixer.setEqGain(trackIndex, band, value)` — 3 bands (0, 1, 2)
- `mixer.setEqFrequency(trackIndex, band, value)` — normalized 0.0-1.0
- `mixer.setEqBandwidth(trackIndex, band, value)` — normalized 0.0-1.0

This means:
- **No need to add any plugin** — the EQ is already there
- **Direct API access** — no parameter ID hunting
- **Available since API v35** — covers FL Studio 2024+
- **If the producer has Parametric EQ 2 already loaded**, we can control it via `plugins.setParamValue()` too

For MVP, the 3-band built-in EQ is sufficient. For deeper EQ work (more bands, different filter types), the agent can use `set_plugin_param()` on an already-loaded Parametric EQ 2.

---

## 2. Communication Architecture

### Why SysEx over Virtual MIDI

FL Studio's embedded Python has **no sockets, no filesystem, no subprocess**. The only communication channel is MIDI. SysEx messages carry arbitrary binary data.

```
┌─────────────────────────────┐          ┌──────────────────────────┐
│   StudioMind Companion App  │          │      FL Studio           │
│                             │          │                          │
│  Agent Loop                 │  SysEx   │  MIDI Controller Script  │
│  ├── sends commands ────────┼──────────┤──► OnSysEx() callback    │
│  │                          │  via     │     ├── parse command     │
│  │                          │ loopMIDI │     ├── execute FL API    │
│  └── receives responses ◄───┼──────────┤◄── └── device.midiOutSysex│
│                             │          │                          │
└─────────────────────────────┘          └──────────────────────────┘
```

### SysEx Protocol Design

**Message structure:**
```
F0 7D <msg_type> <seq_id_hi> <seq_id_lo> <chunk_idx> <total_chunks> <payload...> F7
```

- `F0` = SysEx start
- `7D` = Non-commercial manufacturer ID (safe for dev use)
- `msg_type`:
  - `01` = Request (companion → FL)
  - `02` = Response (FL → companion)
  - `03` = Error (FL → companion)
  - `04` = Event/notification (FL → companion, async)
- `seq_id` = 14-bit sequence ID for request/response matching
- `chunk_idx` / `total_chunks` = for messages exceeding ~1KB MIDI buffer limit
- `payload` = 7-bit encoded JSON (base64 within SysEx safe bytes)
- `F7` = SysEx end

### Payload encoding

SysEx data bytes must be 0x00-0x7F (7-bit). Strategy:
1. Serialize command as JSON
2. Encode as base64 (all safe 7-bit chars)
3. Chunk into ~900-byte SysEx messages
4. Reassemble on the other side

### Alternative: `mmap` Shared Memory (To Investigate)

FL's Python has the `mmap` module available. If named shared memory regions work:
- **Companion app** creates a named shared memory region
- **FL script** maps the same region via `mmap`
- **OnIdle()** polls the shared memory every ~20ms
- **Much faster** than SysEx for large payloads (full project state dumps)

This needs testing but could replace SysEx for bulk data transfer.

### Virtual MIDI Setup

**Windows (primary target):**
1. loopMIDI creates virtual MIDI ports
2. Two ports: "StudioMind In" (companion → FL) and "StudioMind Out" (FL → companion)
3. FL MIDI Settings: assign "StudioMind In" as input to our controller script

**Companion app side:**
- `python-rtmidi` or `mido` library for MIDI I/O

---

## 3. Render Pipeline (UI Automation Fallback)

Since rendering isn't exposed in the API, we orchestrate it from outside:

```
Companion App                          FL Studio
     │                                     │
     ├── 1. Solo target mixer track ───────►│  (via API: mixer.soloTrack)
     │                                     │
     ├── 2. Set render range ──────────────►│  (via API: transport.setSongPos + selection)
     │                                     │
     ├── 3. Trigger export dialog ─────────►│  (via pywinauto: Ctrl+Shift+R or Alt+F8)
     │                                     │
     ├── 4. Configure export settings ─────►│  (via pywinauto: set output path, format, etc.)
     │                                     │
     ├── 5. Start render ──────────────────►│  (via pywinauto: click Start)
     │                                     │
     │   6. Wait for render complete ◄──────┤  (poll filesystem for output file)
     │                                     │
     ├── 7. Unsolo track ─────────────────►│  (via API: mixer.soloTrack)
     │                                     │
     └── 8. Analyze rendered wav            │
            └── FFT, LUFS, peaks            │
```

**Simplified approach for MVP:**
- Render the full master mix first (simpler than per-track stems)
- Use `mixer.getTrackPeaks(index, mode)` for real-time level analysis during playback as a lightweight alternative
- Full stem rendering as a "premium" analysis path

---

## 4. FL Device Script Architecture

Installed at: `Documents/Image-Line/FL Studio/Settings/Hardware/StudioMind/device_StudioMind.py`

```python
# Simplified structure
name = "StudioMind Agent Bridge"
import device, mixer, channels, patterns, transport, general, plugins, midi, ui

class CommandHandler:
    """Dispatches incoming SysEx commands to FL API calls."""

    def handle(self, command: dict) -> dict:
        method = command["method"]
        params = command.get("params", {})

        if method == "read_project_state":
            return self._read_project_state()
        elif method == "set_eq":
            return self._set_eq(**params)
        elif method == "set_plugin_param":
            return self._set_plugin_param(**params)
        # ... etc

    def _read_project_state(self) -> dict:
        state = {
            "bpm": mixer.getCurrentTempo(True),
            "channels": [],
            "mixer_tracks": [],
            "patterns": []
        }
        # ... enumerate everything
        return state

    def _set_eq(self, track_id, band, gain=None, freq=None, bw=None) -> dict:
        general.saveUndo()  # snapshot before mutation
        if gain is not None:
            mixer.setEqGain(track_id, band, gain)
        if freq is not None:
            mixer.setEqFrequency(track_id, band, freq)
        if bw is not None:
            mixer.setEqBandwidth(track_id, band, bw)
        return {"ok": True}

# Protocol handler
protocol = SysExProtocol()
handler = CommandHandler()

def OnSysEx(event):
    """Main entry point for incoming commands."""
    command = protocol.decode(event)
    if command:
        result = handler.handle(command)
        response = protocol.encode(result, msg_type=0x02)
        for chunk in response:
            device.midiOutSysex(bytes(chunk))
        event.handled = True

def OnIdle():
    """Called every ~20ms. Process queued responses if needed."""
    pass

def OnInit():
    ui.setHintMsg("StudioMind connected")

def OnDeInit():
    ui.setHintMsg("StudioMind disconnected")

def OnRefresh(flags):
    """FL state changed — notify companion app."""
    pass

def OnDirtyMixerTrack(index):
    """Mixer track changed — could push notification to companion."""
    pass
```

---

## 5. Companion App Architecture

### Stack Decision: Python CLI First, Tauri Later

For MVP, skip Tauri. Build a **Python CLI + simple web UI** that proves the agent loop works. Tauri is a premature optimization.

```
studiomind/
├── src/
│   ├── agent/
│   │   ├── loop.py           # Core agent loop (plan → act → verify → iterate)
│   │   ├── tools.py          # Tool definitions (JSON schemas + executors)
│   │   └── llm.py            # Claude API integration
│   ├── bridge/
│   │   ├── midi_client.py    # SysEx send/receive over virtual MIDI
│   │   ├── protocol.py       # SysEx encoding/decoding + chunking
│   │   └── commands.py       # High-level command wrappers
│   ├── analyzer/
│   │   ├── spectral.py       # FFT analysis
│   │   ├── loudness.py       # LUFS / true peak measurement
│   │   ├── masking.py        # Frequency masking detection
│   │   └── transients.py     # Transient density analysis
│   ├── render/
│   │   └── automation.py     # pywinauto-based render trigger
│   └── ui/
│       └── cli.py            # Simple CLI chat interface (MVP)
├── scripts/
│   └── device_StudioMind.py  # FL Studio device script
└── pyproject.toml
```

### Key Dependencies

```
python-rtmidi    # MIDI I/O
numpy            # Audio analysis
scipy            # FFT, signal processing
soundfile        # WAV reading
pyloudnorm       # LUFS measurement
anthropic        # Claude API
pywinauto        # Windows UI automation (for rendering)
```

---

## 6. Agent Loop (Detailed)

```python
async def agent_loop(user_goal: str, project_state: dict):
    messages = [
        system_prompt(project_state),
        {"role": "user", "content": user_goal}
    ]

    while True:
        response = await claude.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4096,
            tools=TOOL_DEFINITIONS,
            messages=messages
        )

        # Check if done
        if response.stop_reason == "end_turn":
            display_report(response)
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                # Preview gate: show user what's about to happen
                if is_destructive(block.name):
                    if not await user_approves(block):
                        tool_results.append({"error": "User declined"})
                        continue

                result = await execute_tool(block.name, block.input)
                tool_results.append(result)

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

### Tool Definitions for Claude

```json
[
  {
    "name": "read_project_state",
    "description": "Read the full FL Studio project state: BPM, channels, mixer tracks, routing, plugins, patterns",
    "input_schema": {
      "type": "object",
      "properties": {}
    }
  },
  {
    "name": "set_builtin_eq",
    "description": "Set the built-in 3-band parametric EQ on a mixer track. Each track has bands 0 (low), 1 (mid), 2 (high).",
    "input_schema": {
      "type": "object",
      "properties": {
        "track_id": {"type": "integer", "description": "Mixer track index (0=master)"},
        "band": {"type": "integer", "enum": [0, 1, 2], "description": "EQ band (0=low, 1=mid, 2=high)"},
        "gain": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Gain (0.0-1.0 normalized, 0.5=unity)"},
        "frequency": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Center frequency (0.0-1.0 normalized)"},
        "bandwidth": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Bandwidth/Q (0.0-1.0 normalized)"}
      },
      "required": ["track_id", "band"]
    }
  },
  {
    "name": "set_plugin_param",
    "description": "Set a parameter on any plugin loaded in a mixer insert slot or channel rack.",
    "input_schema": {
      "type": "object",
      "properties": {
        "track_id": {"type": "integer", "description": "Mixer track or channel index"},
        "slot": {"type": "integer", "default": -1, "description": "Mixer FX slot index (-1 for channel rack plugin)"},
        "param_id": {"type": "integer", "description": "Parameter index"},
        "value": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Parameter value (normalized 0.0-1.0)"}
      },
      "required": ["track_id", "param_id", "value"]
    }
  },
  {
    "name": "set_mixer_param",
    "description": "Set a mixer track parameter: volume, pan, mute, solo, stereo separation.",
    "input_schema": {
      "type": "object",
      "properties": {
        "track_id": {"type": "integer"},
        "param": {"type": "string", "enum": ["volume", "pan", "mute", "solo", "stereo_sep"]},
        "value": {"type": "number", "description": "Value (volume/pan/stereo_sep: 0.0-1.0, mute/solo: 0 or 1)"}
      },
      "required": ["track_id", "param", "value"]
    }
  },
  {
    "name": "render_and_analyze",
    "description": "Render audio from FL Studio and analyze it. Returns spectral profile, LUFS, true peak, etc.",
    "input_schema": {
      "type": "object",
      "properties": {
        "mode": {"type": "string", "enum": ["master", "stem"], "default": "master"},
        "track_id": {"type": "integer", "description": "Mixer track to solo for stem render"},
        "bars_start": {"type": "integer"},
        "bars_end": {"type": "integer"}
      }
    }
  },
  {
    "name": "snapshot",
    "description": "Save FL Studio undo state before making changes. Always call before destructive operations.",
    "input_schema": {
      "type": "object",
      "properties": {
        "label": {"type": "string", "description": "Description of what's about to change"}
      }
    }
  },
  {
    "name": "revert",
    "description": "Undo the last change (revert to previous snapshot).",
    "input_schema": {
      "type": "object",
      "properties": {}
    }
  }
]
```

---

## 7. Existing Projects to Build On

### Must-Study References

| Project | What to Learn | URL |
|---------|--------------|-----|
| **Flapi** | SysEx protocol implementation, message chunking, virtual MIDI setup | github.com/MaddyGuthridge/Flapi |
| **fl-studio-mcp** | MCP server pattern for LLM integration | github.com/calvinw/fl-studio-mcp |
| **FL Studio API Stubs** | Definitive API reference (maintained by Image-Line) | github.com/IL-Group/FL-Studio-API-Stubs |
| **PyFLP** | .flp file parsing for offline note/arrangement data | github.com/demberto/PyFLP |

### Decision: Fork Flapi or Build From Scratch?

**Recommendation: Build our own SysEx protocol, study Flapi for patterns.**

Reasons:
- Flapi is unmaintained and mid-refactor
- Our protocol needs are simpler (fewer API functions for MVP)
- We want a typed, well-documented protocol layer
- Flapi's chunking and 7-bit encoding patterns are valuable reference material

---

## 8. Development Order (Updated)

1. **FL Device Script + SysEx Protocol** — Get a command round-trip working
2. **`read_project_state()`** — First real tool
3. **`set_builtin_eq()`** — First mutation tool
4. **`snapshot()` / `revert()`** — Safety net
5. **Audio analysis pipeline** — FFT, LUFS on wav files
6. **`render_and_analyze()`** — UI automation render trigger
7. **Agent loop + CLI** — Wire Claude API with tools
8. **Vertical slice** — "Cut low-mids on the piano" end-to-end
9. **Full "mix this" flow** — Agent reads, analyzes, plans, executes, verifies
