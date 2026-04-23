"""
MIDI client for communicating with FL Studio via virtual MIDI ports.

Uses python-rtmidi to send/receive SysEx messages over loopMIDI virtual ports.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from studiomind.protocol import (
    MSG_REQUEST,
    Message,
    MessageAssembler,
    SequenceCounter,
    encode,
)

try:
    import rtmidi
except ImportError:
    rtmidi = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Port name patterns to search for.
#
# On Microsoft MIDI Services Basic MIDI 1.0 Loopback, the default endpoints
# are "Default App Loopback (A)" and "Default App Loopback (B)", cross-wired
# so that writes to (A) are readable at (B) and vice versa. The companion
# uses endpoint (A) for both its MidiOut and MidiIn; FL Studio attaches to
# endpoint (B) for both its Input and Output in MIDI Settings.
#
# Override via STUDIOMIND_MIDI_IN / STUDIOMIND_MIDI_OUT env vars if you've
# renamed the endpoints or set up a different transport (e.g. loopMIDI).
import os as _os
DEFAULT_INPUT_PORT = _os.environ.get("STUDIOMIND_MIDI_IN", "Default App Loopback (A)")
DEFAULT_OUTPUT_PORT = _os.environ.get("STUDIOMIND_MIDI_OUT", "Default App Loopback (A)")


def find_port(api: rtmidi.MidiIn | rtmidi.MidiOut, pattern: str) -> int | None:
    """Find a MIDI port whose name contains `pattern`. Returns port index or None."""
    for i, name in enumerate(api.get_ports()):
        if pattern.lower() in name.lower():
            return i
    return None


def _require_rtmidi() -> None:
    if rtmidi is None:
        raise ImportError(
            "python-rtmidi is required for MIDI communication. "
            "Install it with: pip install python-rtmidi"
        )


def list_ports() -> dict[str, list[str]]:
    """List all available MIDI input and output ports."""
    _require_rtmidi()
    midi_in = rtmidi.MidiIn()
    midi_out = rtmidi.MidiOut()
    result = {
        "inputs": midi_in.get_ports(),
        "outputs": midi_out.get_ports(),
    }
    del midi_in, midi_out
    return result


class MidiClient:
    """
    Bidirectional MIDI client for the StudioMind ↔ FL Studio bridge.

    Sends SysEx commands to FL Studio and receives responses via virtual MIDI ports.
    """

    def __init__(
        self,
        input_port_pattern: str = DEFAULT_INPUT_PORT,
        output_port_pattern: str = DEFAULT_OUTPUT_PORT,
    ) -> None:
        self._input_pattern = input_port_pattern
        self._output_pattern = output_port_pattern
        self._midi_in: rtmidi.MidiIn | None = None
        self._midi_out: rtmidi.MidiOut | None = None
        self._seq = SequenceCounter()
        self._assembler = MessageAssembler()
        self._response_events: dict[int, threading.Event] = {}
        self._responses: dict[int, Message] = {}
        self._event_callback: Callable[[Message], None] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Open MIDI input and output ports."""
        _require_rtmidi()
        # Output port (companion → FL)
        self._midi_out = rtmidi.MidiOut()
        out_idx = find_port(self._midi_out, self._output_pattern)
        if out_idx is None:
            available = self._midi_out.get_ports()
            raise ConnectionError(
                f"No MIDI output port matching '{self._output_pattern}'. "
                f"Available: {available}. Is loopMIDI running?"
            )
        self._midi_out.open_port(out_idx)
        logger.info("Opened MIDI output: %s", self._midi_out.get_ports()[out_idx])

        # Input port (FL → companion)
        self._midi_in = rtmidi.MidiIn()
        self._midi_in.ignore_types(sysex=False)  # We NEED SysEx!
        in_idx = find_port(self._midi_in, self._input_pattern)
        if in_idx is None:
            available = self._midi_in.get_ports()
            raise ConnectionError(
                f"No MIDI input port matching '{self._input_pattern}'. "
                f"Available: {available}. Is loopMIDI running?"
            )
        self._midi_in.open_port(in_idx)
        self._midi_in.set_callback(self._on_midi_message)
        logger.info("Opened MIDI input: %s", self._midi_in.get_ports()[in_idx])

        self._connected = True

    def disconnect(self) -> None:
        """Close MIDI ports."""
        if self._midi_in:
            self._midi_in.close_port()
            self._midi_in = None
        if self._midi_out:
            self._midi_out.close_port()
            self._midi_out = None
        self._connected = False

    def on_event(self, callback: Callable[[Message], None]) -> None:
        """Register a callback for async events (notifications from FL)."""
        self._event_callback = callback

    def send(self, data: dict, timeout: float = 10.0) -> Message:
        """
        Send a command to FL Studio and wait for the response.

        Args:
            data: Command dict (e.g., {"method": "read_project_state", "params": {}})
            timeout: Max seconds to wait for response

        Returns:
            The response Message

        Raises:
            ConnectionError: If not connected
            TimeoutError: If no response within timeout
        """
        if not self._connected or not self._midi_out:
            raise ConnectionError("Not connected to FL Studio")

        seq_id = self._seq.next()
        event = threading.Event()
        self._response_events[seq_id] = event

        # Encode and send
        chunks = encode(data, MSG_REQUEST, seq_id)
        for chunk in chunks:
            self._midi_out.send_message(list(chunk))
            if len(chunks) > 1:
                time.sleep(0.002)  # Small delay between chunks to avoid buffer overflow

        logger.debug("Sent request seq=%d method=%s (%d chunks)", seq_id, data.get("method"), len(chunks))

        # Wait for response
        if not event.wait(timeout):
            self._response_events.pop(seq_id, None)
            raise TimeoutError(f"No response from FL Studio within {timeout}s (seq={seq_id})")

        self._response_events.pop(seq_id, None)
        response = self._responses.pop(seq_id)

        if response.is_error:
            raise RuntimeError(f"FL Studio error: {response.data}")

        return response

    def _on_midi_message(self, event: tuple, data: None = None) -> None:
        """Callback for incoming MIDI messages from FL Studio."""
        message_bytes, _delta_time = event

        if not message_bytes or message_bytes[0] != 0xF0:
            return  # Not SysEx

        sysex = bytes(message_bytes)
        msg = self._assembler.feed(sysex)

        if msg is None:
            return  # Incomplete multi-chunk message

        if msg.is_response or msg.is_error:
            # Match to pending request
            self._responses[msg.seq_id] = msg
            evt = self._response_events.get(msg.seq_id)
            if evt:
                evt.set()
            else:
                logger.warning("Received response for unknown seq=%d", msg.seq_id)
        elif msg.msg_type == 0x04 and self._event_callback:
            # Async event from FL
            self._event_callback(msg)

    def __enter__(self) -> MidiClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()
