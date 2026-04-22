"""
SysEx protocol for StudioMind ↔ FL Studio communication.

Message format:
    F0 7D <type> <seq_hi> <seq_lo> <chunk_idx> <chunk_total> <payload...> F7

- F0/F7: SysEx start/end
- 7D: Non-commercial manufacturer ID
- type: 0x01=request, 0x02=response, 0x03=error, 0x04=event
- seq_hi/seq_lo: 14-bit sequence ID (7 bits each)
- chunk_idx: 0-based chunk index
- chunk_total: total chunks (1-based)
- payload: base64-encoded JSON (all bytes 0x00-0x7F safe)
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

# Protocol constants
SYSEX_START = 0xF0
SYSEX_END = 0xF7
MANUFACTURER_ID = 0x7D  # Non-commercial / development use

# Message types
MSG_REQUEST = 0x01
MSG_RESPONSE = 0x02
MSG_ERROR = 0x03
MSG_EVENT = 0x04

# Header: F0 + manufacturer + type + seq_hi + seq_lo + chunk_idx + chunk_total = 7 bytes
# Footer: F7 = 1 byte
# Conservative max SysEx payload to avoid driver issues
HEADER_SIZE = 7
FOOTER_SIZE = 1
MAX_SYSEX_SIZE = 1024
MAX_PAYLOAD_PER_CHUNK = MAX_SYSEX_SIZE - HEADER_SIZE - FOOTER_SIZE  # 1016 bytes


@dataclass
class Message:
    """A decoded protocol message."""

    msg_type: int
    seq_id: int
    data: Any  # Parsed JSON payload

    @property
    def is_request(self) -> bool:
        return self.msg_type == MSG_REQUEST

    @property
    def is_response(self) -> bool:
        return self.msg_type == MSG_RESPONSE

    @property
    def is_error(self) -> bool:
        return self.msg_type == MSG_ERROR


class SequenceCounter:
    """Thread-safe 14-bit sequence ID generator."""

    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        seq = self._value
        self._value = (self._value + 1) & 0x3FFF  # 14-bit wrap
        return seq


def encode(data: Any, msg_type: int, seq_id: int) -> list[bytes]:
    """
    Encode a Python object into one or more SysEx messages.

    Returns a list of complete SysEx messages (each starting with F0, ending with F7).
    """
    # Serialize → base64
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    b64_bytes = base64.b64encode(json_bytes)

    # Split into chunks
    chunks = []
    total = max(1, (len(b64_bytes) + MAX_PAYLOAD_PER_CHUNK - 1) // MAX_PAYLOAD_PER_CHUNK)

    for i in range(total):
        start = i * MAX_PAYLOAD_PER_CHUNK
        end = start + MAX_PAYLOAD_PER_CHUNK
        chunk_payload = b64_bytes[start:end]

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
        msg.extend(chunk_payload)
        msg.append(SYSEX_END)

        chunks.append(bytes(msg))

    return chunks


def decode_header(sysex: bytes) -> tuple[int, int, int, int, bytes] | None:
    """
    Decode SysEx header, returning (msg_type, seq_id, chunk_idx, chunk_total, payload).
    Returns None if the message is not a StudioMind protocol message.
    """
    if len(sysex) < HEADER_SIZE + FOOTER_SIZE:
        return None
    if sysex[0] != SYSEX_START or sysex[-1] != SYSEX_END:
        return None
    if sysex[1] != MANUFACTURER_ID:
        return None

    msg_type = sysex[2]
    seq_id = (sysex[3] << 7) | sysex[4]
    chunk_idx = sysex[5]
    chunk_total = sysex[6]
    payload = sysex[HEADER_SIZE:-FOOTER_SIZE]

    return msg_type, seq_id, chunk_idx, chunk_total, payload


def decode_payload(b64_bytes: bytes) -> Any:
    """Decode base64 payload back to a Python object."""
    json_bytes = base64.b64decode(b64_bytes)
    return json.loads(json_bytes)


@dataclass
class MessageAssembler:
    """Reassembles chunked SysEx messages into complete Messages."""

    _pending: dict[int, dict] = field(default_factory=dict)

    def feed(self, sysex: bytes) -> Message | None:
        """
        Feed a raw SysEx message. Returns a complete Message when all chunks are received,
        or None if still waiting for more chunks.
        """
        parsed = decode_header(sysex)
        if parsed is None:
            return None

        msg_type, seq_id, chunk_idx, chunk_total, payload = parsed

        # Single-chunk message — fast path
        if chunk_total == 1:
            data = decode_payload(payload)
            return Message(msg_type=msg_type, seq_id=seq_id, data=data)

        # Multi-chunk — accumulate
        key = seq_id
        if key not in self._pending:
            self._pending[key] = {
                "msg_type": msg_type,
                "total": chunk_total,
                "chunks": {},
            }

        entry = self._pending[key]
        entry["chunks"][chunk_idx] = payload

        if len(entry["chunks"]) == entry["total"]:
            # All chunks received — reassemble in order
            full_payload = b"".join(entry["chunks"][i] for i in range(entry["total"]))
            del self._pending[key]
            data = decode_payload(full_payload)
            return Message(msg_type=msg_type, seq_id=seq_id, data=data)

        return None

    def clear_stale(self, max_pending: int = 100) -> None:
        """Remove oldest pending entries if too many accumulate."""
        while len(self._pending) > max_pending:
            oldest_key = next(iter(self._pending))
            del self._pending[oldest_key]
