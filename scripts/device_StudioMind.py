# name=StudioMind Agent Bridge
# url=https://github.com/anchinous/studiomind
# supportedDevices=StudioMind

"""
FL Studio MIDI Controller Script for StudioMind.

This script runs inside FL Studio's embedded Python interpreter. It:
1. Receives SysEx commands from the companion app via virtual MIDI
2. Dispatches them to FL Studio's Python API
3. Returns structured JSON responses via SysEx

CONSTRAINTS:
- No filesystem, no sockets, no subprocess, no pip packages
- Only FL built-in modules + limited stdlib (json, binascii, struct, mmap)
- Must be self-contained — all protocol logic is embedded here
"""

import channels
import device
import general
import json
import midi
import mixer
import patterns
import plugins
import transport
import ui

# ═══════════════════════════════════════════════════════════════════
# EMBEDDED PROTOCOL (mirrors studiomind.protocol but standalone)
# ═══════════════════════════════════════════════════════════════════

import binascii

SYSEX_START = 0xF0
SYSEX_END = 0xF7
MANUFACTURER_ID = 0x7D
MSG_REQUEST = 0x01
MSG_RESPONSE = 0x02
MSG_ERROR = 0x03
MSG_EVENT = 0x04
HEADER_SIZE = 7
MAX_PAYLOAD = 900  # Conservative payload per chunk


def _b64encode(data):
    """Base64 encode bytes. Returns bytes."""
    return binascii.b2a_base64(data, newline=False)


def _b64decode(data):
    """Base64 decode bytes. Returns bytes."""
    return binascii.a2b_base64(data)


def _encode_sysex(data_dict, msg_type, seq_id):
    """Encode a dict into one or more SysEx messages."""
    json_str = json.dumps(data_dict, separators=(",", ":"))
    json_bytes = json_str.encode("utf-8")
    b64 = _b64encode(json_bytes)

    total = max(1, (len(b64) + MAX_PAYLOAD - 1) // MAX_PAYLOAD)
    messages = []

    for i in range(total):
        start = i * MAX_PAYLOAD
        end = start + MAX_PAYLOAD
        chunk = b64[start:end]

        seq_hi = (seq_id >> 7) & 0x7F
        seq_lo = seq_id & 0x7F

        msg = bytearray()
        msg.append(SYSEX_START)
        msg.append(MANUFACTURER_ID)
        msg.append(msg_type & 0x7F)
        msg.append(seq_hi)
        msg.append(seq_lo)
        msg.append(i & 0x7F)
        msg.append(total & 0x7F)
        msg.extend(chunk)
        msg.append(SYSEX_END)

        messages.append(bytes(msg))

    return messages


def _decode_sysex(sysex_bytes):
    """
    Decode a SysEx message header.
    Returns (msg_type, seq_id, chunk_idx, chunk_total, payload) or None.
    """
    if len(sysex_bytes) < HEADER_SIZE + 1:
        return None
    if sysex_bytes[0] != SYSEX_START or sysex_bytes[-1] != SYSEX_END:
        return None
    if sysex_bytes[1] != MANUFACTURER_ID:
        return None

    msg_type = sysex_bytes[2]
    seq_id = (sysex_bytes[3] << 7) | sysex_bytes[4]
    chunk_idx = sysex_bytes[5]
    chunk_total = sysex_bytes[6]
    payload = sysex_bytes[HEADER_SIZE:-1]

    return msg_type, seq_id, chunk_idx, chunk_total, payload


# Multi-chunk message assembly
_pending_messages = {}  # seq_id -> {total, chunks: {idx: payload}}


def _assemble_message(sysex_bytes):
    """
    Feed a SysEx message. Returns (msg_type, seq_id, data_dict) when complete,
    or None if still waiting for chunks.
    """
    parsed = _decode_sysex(sysex_bytes)
    if parsed is None:
        return None

    msg_type, seq_id, chunk_idx, chunk_total, payload = parsed

    # Single chunk — fast path
    if chunk_total == 1:
        json_bytes = _b64decode(payload)
        data = json.loads(json_bytes)
        return msg_type, seq_id, data

    # Multi-chunk
    if seq_id not in _pending_messages:
        _pending_messages[seq_id] = {"total": chunk_total, "chunks": {}}

    entry = _pending_messages[seq_id]
    entry["chunks"][chunk_idx] = payload

    if len(entry["chunks"]) == entry["total"]:
        full = b"".join(entry["chunks"][i] for i in range(entry["total"]))
        del _pending_messages[seq_id]
        json_bytes = _b64decode(full)
        data = json.loads(json_bytes)
        return msg_type, seq_id, data

    return None


def _send_response(data, seq_id):
    """Encode and send a response back to the companion app."""
    messages = _encode_sysex(data, MSG_RESPONSE, seq_id)
    for msg in messages:
        device.midiOutSysex(msg)


def _send_error(error_msg, seq_id):
    """Send an error response."""
    messages = _encode_sysex({"error": str(error_msg)}, MSG_ERROR, seq_id)
    for msg in messages:
        device.midiOutSysex(msg)


# ═══════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

def _handle_ping(params):
    """Test connection — returns FL Studio version info."""
    return {
        "ok": True,
        "api_version": general.getVersion(),
        "fl_version": ui.getVersion(),
        "script": "StudioMind Agent Bridge",
    }


def _handle_get_project_name(params):
    """
    Return the current FL project name + path, best-effort.

    FL's Python API exposes project metadata in different places across versions,
    and none of these are guaranteed to exist. We try each and return whatever
    we can; the companion app derives a StudioMind project folder from the result.
    """
    result = {"name": "", "path": "", "window_title": "", "saved": False}

    try:
        result["window_title"] = ui.getProgTitle()
    except Exception:
        pass

    # general.getName() — returns the project name string on recent FL versions
    getter = getattr(general, "getName", None)
    if getter is not None:
        try:
            name = getter()
            if isinstance(name, str):
                result["name"] = name
        except Exception:
            pass

    # general.getFilename() — full .flp path on recent FL versions
    getter = getattr(general, "getFilename", None)
    if getter is not None:
        try:
            path = getter()
            if isinstance(path, str):
                result["path"] = path
        except Exception:
            pass

    # Has the project been saved at least once? Unsaved projects have no path.
    try:
        result["saved"] = not general.getChangedFlag() or bool(result["path"])
    except Exception:
        pass

    return result


def _handle_read_project_state(params):
    """Read full project snapshot."""
    state = {
        "bpm": mixer.getCurrentTempo(True),
        "ppq": general.getRecPPQ(),
        "playing": transport.isPlaying(),
        "recording": transport.isRecording(),
        "loop_mode": transport.getLoopMode(),  # 0=pattern, 1=song
        "changed": general.getChangedFlag(),
        "channels": [],
        "mixer_tracks": [],
        "patterns": [],
    }

    # Channels
    ch_count = channels.channelCount(True)  # global count
    for i in range(ch_count):
        ch = {
            "index": i,
            "name": channels.getChannelName(i, True),
            "type": channels.getChannelType(i, True),
            "volume": round(channels.getChannelVolume(i, False, True), 4),
            "pan": round(channels.getChannelPan(i, True), 4),
            "muted": channels.isChannelMuted(i, True),
            "solo": channels.isChannelSolo(i, True),
            "mixer_track": channels.getTargetFxTrack(i, True),
            "selected": channels.isChannelSelected(i, True),
        }
        state["channels"].append(ch)

    # Mixer tracks (skip unused — check name and enabled status)
    track_count = mixer.trackCount()
    for i in range(track_count):
        name = mixer.getTrackName(i)
        # Include master (0), named tracks, and tracks with active channels routed to them
        is_master = i == 0
        has_name = name and name != f"Insert {i}" and name != "Master" or is_master
        has_volume = mixer.getTrackVolume(i) > 0.001

        # For a summary, include master + first 20 + any named tracks
        if not (is_master or has_name or (i <= 20 and has_volume)):
            continue

        track = {
            "index": i,
            "name": name,
            "volume": round(mixer.getTrackVolume(i), 4),
            "pan": round(mixer.getTrackPan(i), 4),
            "muted": mixer.isTrackMuted(i),
            "solo": mixer.isTrackSolo(i),
            "enabled": mixer.isTrackEnabled(i),
            "armed": mixer.isTrackArmed(i),
            "eq": _read_eq(i),
            "plugins": _read_track_plugins(i),
        }
        state["mixer_tracks"].append(track)

    # Patterns (only non-empty)
    pat_count = patterns.patternCount()
    for i in range(1, pat_count + 1):
        if patterns.isPatternDefault(i):
            continue
        pat = {
            "index": i,
            "name": patterns.getPatternName(i),
            "length": patterns.getPatternLength(i),
            "selected": patterns.isPatternSelected(i),
        }
        state["patterns"].append(pat)

    return state


def _read_eq(track_id):
    """Read the built-in 3-band EQ for a mixer track."""
    bands = []
    for b in range(3):
        bands.append({
            "gain": round(mixer.getEqGain(track_id, b), 4),
            "gain_db": round(mixer.getEqGain(track_id, b, 1), 2),
            "frequency": round(mixer.getEqFrequency(track_id, b), 4),
            "frequency_hz": round(mixer.getEqFrequency(track_id, b, 1), 1),
            "bandwidth": round(mixer.getEqBandwidth(track_id, b), 4),
        })
    return {"band_count": 3, "bands": bands}


def _read_track_plugins(track_id):
    """Enumerate plugins on a mixer track's insert slots."""
    result = []
    for slot in range(10):  # FL has 10 insert slots per mixer track
        if not plugins.isValid(track_id, slot):
            continue
        result.append({
            "slot": slot,
            "name": plugins.getPluginName(track_id, slot),
            "user_name": plugins.getPluginName(track_id, slot, True),
            "param_count": plugins.getParamCount(track_id, slot),
        })
    return result


def _handle_read_mixer_track(params):
    """Read detailed info for a single mixer track."""
    tid = params["track_id"]
    track = {
        "index": tid,
        "name": mixer.getTrackName(tid),
        "volume": round(mixer.getTrackVolume(tid), 4),
        "volume_db": round(mixer.getTrackVolume(tid, 1), 2),
        "pan": round(mixer.getTrackPan(tid), 4),
        "stereo_sep": round(mixer.getTrackStereoSep(tid), 4),
        "muted": mixer.isTrackMuted(tid),
        "solo": mixer.isTrackSolo(tid),
        "enabled": mixer.isTrackEnabled(tid),
        "armed": mixer.isTrackArmed(tid),
        "slots_enabled": mixer.isTrackSlotsEnabled(tid),
        "eq": _read_eq(tid),
        "plugins": [],
        "routing": [],
    }

    # Detailed plugin info with parameter names
    for slot in range(10):
        if not plugins.isValid(tid, slot):
            continue
        p_info = {
            "slot": slot,
            "name": plugins.getPluginName(tid, slot),
            "params": [],
        }
        param_count = min(plugins.getParamCount(tid, slot), 128)  # Cap for sanity
        for pi in range(param_count):
            pname = plugins.getParamName(pi, tid, slot)
            if not pname:
                continue  # Skip empty/unused params
            p_info["params"].append({
                "id": pi,
                "name": pname,
                "value": round(plugins.getParamValue(pi, tid, slot), 6),
                "display": plugins.getParamValueString(pi, tid, slot),
            })
        track["plugins"].append(p_info)

    # Routing — which tracks does this send to?
    total_tracks = mixer.trackCount()
    for dest in range(total_tracks):
        if dest == tid:
            continue
        if mixer.getRouteSendActive(tid, dest):
            track["routing"].append({
                "dest": dest,
                "dest_name": mixer.getTrackName(dest),
                "level": round(mixer.getRouteToLevel(tid, dest), 4),
            })

    return track


def _handle_read_channel(params):
    """Read detailed info for a single channel."""
    cid = params["channel_id"]
    ch = {
        "index": cid,
        "name": channels.getChannelName(cid, True),
        "type": channels.getChannelType(cid, True),
        "volume": round(channels.getChannelVolume(cid, False, True), 4),
        "volume_db": round(channels.getChannelVolume(cid, True, True), 2),
        "pan": round(channels.getChannelPan(cid, True), 4),
        "muted": channels.isChannelMuted(cid, True),
        "solo": channels.isChannelSolo(cid, True),
        "mixer_track": channels.getTargetFxTrack(cid, True),
        "selected": channels.isChannelSelected(cid, True),
    }

    # Plugin info for channel instrument
    if plugins.isValid(cid, -1, True):
        param_count = min(plugins.getParamCount(cid, -1, True), 64)
        ch["plugin"] = {
            "name": plugins.getPluginName(cid, -1, False, True),
            "params": [],
        }
        for pi in range(param_count):
            pname = plugins.getParamName(pi, cid, -1, True)
            if not pname:
                continue
            ch["plugin"]["params"].append({
                "id": pi,
                "name": pname,
                "value": round(plugins.getParamValue(pi, cid, -1, True), 6),
            })

    return ch


def _handle_set_eq(params):
    """Set built-in 3-band EQ parameters on a mixer track."""
    tid = params["track_id"]
    band = params["band"]

    # Save undo state before mutation
    general.saveUndo("StudioMind: EQ change", 0, 0)

    if "gain" in params:
        mixer.setEqGain(tid, band, params["gain"])
    if "frequency" in params:
        mixer.setEqFrequency(tid, band, params["frequency"])
    if "bandwidth" in params:
        mixer.setEqBandwidth(tid, band, params["bandwidth"])

    return {"ok": True, "eq": _read_eq(tid)}


def _handle_get_eq(params):
    """Read the built-in EQ state for a mixer track."""
    return _read_eq(params["track_id"])


def _handle_set_plugin_param(params):
    """Set a plugin parameter value."""
    tid = params["track_id"]
    slot = params.get("slot", -1)
    pid = params["param_id"]
    value = params["value"]

    general.saveUndo("StudioMind: plugin param change", 0, 0)
    plugins.setParamValue(value, pid, tid, slot)

    return {
        "ok": True,
        "param_id": pid,
        "new_value": round(plugins.getParamValue(pid, tid, slot), 6),
        "display": plugins.getParamValueString(pid, tid, slot),
    }


def _handle_get_plugin_params(params):
    """Read all parameters for a plugin."""
    tid = params["track_id"]
    slot = params.get("slot", -1)

    if not plugins.isValid(tid, slot):
        return {"error": f"No valid plugin at track {tid} slot {slot}"}

    result = {
        "name": plugins.getPluginName(tid, slot),
        "params": [],
    }
    param_count = min(plugins.getParamCount(tid, slot), 128)
    for pi in range(param_count):
        pname = plugins.getParamName(pi, tid, slot)
        if not pname:
            continue
        result["params"].append({
            "id": pi,
            "name": pname,
            "value": round(plugins.getParamValue(pi, tid, slot), 6),
            "display": plugins.getParamValueString(pi, tid, slot),
        })

    return result


def _handle_set_mixer_param(params):
    """Set a mixer track parameter."""
    tid = params["track_id"]
    param = params["param"]
    value = params["value"]

    general.saveUndo("StudioMind: mixer param change", 0, 0)

    if param == "volume":
        mixer.setTrackVolume(tid, value)
    elif param == "pan":
        mixer.setTrackPan(tid, value)
    elif param == "mute":
        mixer.muteTrack(tid, int(value))
    elif param == "solo":
        mixer.soloTrack(tid, int(value))
    elif param == "stereo_sep":
        mixer.setTrackStereoSep(tid, value)
    else:
        return {"error": f"Unknown mixer param: {param}"}

    return {"ok": True}


def _handle_snapshot(params):
    """Save undo state."""
    label = params.get("label", "StudioMind snapshot")
    general.saveUndo(label, 0, 0)
    return {"ok": True, "label": label}


def _handle_revert(params):
    """Undo the last change."""
    general.undo()
    return {"ok": True}


def _handle_transport(params):
    """Control transport (play/stop/record)."""
    action = params["action"]
    if action == "play":
        transport.start()
    elif action == "stop":
        transport.stop()
    elif action == "record":
        transport.record()
    elif action == "set_loop_mode":
        transport.setLoopMode()
    else:
        return {"error": f"Unknown transport action: {action}"}
    return {"ok": True, "playing": transport.isPlaying(), "recording": transport.isRecording()}


def _handle_get_bpm(params):
    """Get current BPM."""
    return {"bpm": mixer.getCurrentTempo(True)}


# Command dispatch table
COMMANDS = {
    "ping": _handle_ping,
    "get_project_name": _handle_get_project_name,
    "read_project_state": _handle_read_project_state,
    "read_mixer_track": _handle_read_mixer_track,
    "read_channel": _handle_read_channel,
    "set_eq": _handle_set_eq,
    "get_eq": _handle_get_eq,
    "set_plugin_param": _handle_set_plugin_param,
    "get_plugin_params": _handle_get_plugin_params,
    "set_mixer_param": _handle_set_mixer_param,
    "snapshot": _handle_snapshot,
    "revert": _handle_revert,
    "transport": _handle_transport,
    "get_bpm": _handle_get_bpm,
}


def _dispatch(command_data, seq_id):
    """Parse a command and dispatch to the appropriate handler."""
    method = command_data.get("method")
    params = command_data.get("params", {})

    if method not in COMMANDS:
        _send_error(f"Unknown method: {method}", seq_id)
        return

    try:
        result = COMMANDS[method](params)
        _send_response(result, seq_id)
    except Exception as e:
        _send_error(f"{method} failed: {e}", seq_id)


# ═══════════════════════════════════════════════════════════════════
# FL STUDIO CALLBACKS
# ═══════════════════════════════════════════════════════════════════

def OnInit():
    """Called when the script is loaded."""
    ui.setHintMsg("StudioMind: Connected")


def OnDeInit():
    """Called when the script is unloaded."""
    ui.setHintMsg("StudioMind: Disconnected")


def OnSysEx(event):
    """Main entry point — receives SysEx commands from the companion app."""
    result = _assemble_message(bytes(event.sysex))
    if result is None:
        return  # Not our message or incomplete multi-chunk

    msg_type, seq_id, data = result

    if msg_type == MSG_REQUEST:
        _dispatch(data, seq_id)
        event.handled = True


def OnIdle():
    """Called ~every 20ms. Reserved for future use (mmap polling, keepalive)."""
    pass


def OnRefresh(flags):
    """Called when FL Studio state changes. Could push notifications to companion."""
    pass


def OnDirtyMixerTrack(index):
    """Called when a mixer track changes."""
    pass


def OnProjectLoad(status):
    """Called when a project is loaded (status: 0=started, 100=complete)."""
    if status == 100:
        ui.setHintMsg("StudioMind: Project loaded")
