"""Tests for the SysEx protocol encoding/decoding."""

from studiomind.protocol import (
    MSG_REQUEST,
    MSG_RESPONSE,
    MAX_PAYLOAD_PER_CHUNK,
    Message,
    MessageAssembler,
    SequenceCounter,
    decode_header,
    encode,
)


def test_encode_decode_simple():
    """Round-trip a simple command through encode/decode."""
    data = {"method": "ping", "params": {}}
    chunks = encode(data, MSG_REQUEST, seq_id=42)

    assert len(chunks) == 1  # Small message fits in one chunk

    sysex = chunks[0]
    assert sysex[0] == 0xF0  # SysEx start
    assert sysex[-1] == 0xF7  # SysEx end
    assert sysex[1] == 0x7D  # Manufacturer ID
    assert sysex[2] == MSG_REQUEST  # Message type

    # Decode it back
    parsed = decode_header(sysex)
    assert parsed is not None
    msg_type, seq_id, chunk_idx, chunk_total, payload = parsed
    assert msg_type == MSG_REQUEST
    assert seq_id == 42
    assert chunk_idx == 0
    assert chunk_total == 1


def test_assembler_single_chunk():
    """MessageAssembler handles single-chunk messages."""
    data = {"method": "read_project_state", "params": {}}
    chunks = encode(data, MSG_RESPONSE, seq_id=7)

    assembler = MessageAssembler()
    msg = assembler.feed(chunks[0])

    assert msg is not None
    assert isinstance(msg, Message)
    assert msg.msg_type == MSG_RESPONSE
    assert msg.seq_id == 7
    assert msg.data["method"] == "read_project_state"


def test_assembler_multi_chunk():
    """MessageAssembler reassembles multi-chunk messages."""
    # Create a payload large enough to require multiple chunks
    data = {"big": "x" * 2000}
    chunks = encode(data, MSG_RESPONSE, seq_id=100)

    assert len(chunks) > 1  # Should need multiple chunks

    assembler = MessageAssembler()
    msg = None
    for chunk in chunks:
        msg = assembler.feed(chunk)
        if msg is not None:
            break

    assert msg is not None
    assert msg.data["big"] == "x" * 2000
    assert msg.seq_id == 100


def test_sequence_counter():
    """SequenceCounter wraps at 14 bits."""
    counter = SequenceCounter()
    first = counter.next()
    assert first == 0

    second = counter.next()
    assert second == 1

    # Force wrap
    counter._value = 0x3FFF
    val = counter.next()
    assert val == 0x3FFF

    wrapped = counter.next()
    assert wrapped == 0  # Should wrap


def test_all_bytes_7bit_safe():
    """All bytes in SysEx payload (between F0 and F7) must be 0x00-0x7F."""
    data = {"test": "hello world", "number": 42, "unicode": "alpha"}
    chunks = encode(data, MSG_REQUEST, seq_id=0)

    for chunk in chunks:
        # Skip first byte (F0) and last byte (F7) — they're allowed to be >= 0x80
        inner = chunk[1:-1]
        for i, byte in enumerate(inner):
            assert byte <= 0x7F, f"Byte at position {i+1} is 0x{byte:02X}, must be <= 0x7F"


def test_non_studiomind_sysex_ignored():
    """SysEx messages with different manufacturer ID are ignored."""
    sysex = bytes([0xF0, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x41, 0xF7])
    parsed = decode_header(sysex)
    assert parsed is None  # Not our manufacturer ID


def test_large_project_state():
    """Simulate a large project state response."""
    state = {
        "bpm": 140,
        "channels": [{"index": i, "name": f"Ch {i}", "volume": 0.78} for i in range(50)],
        "mixer_tracks": [{"index": i, "name": f"Track {i}", "volume": 0.8} for i in range(30)],
    }
    chunks = encode(state, MSG_RESPONSE, seq_id=999)

    # Reassemble
    assembler = MessageAssembler()
    msg = None
    for chunk in chunks:
        msg = assembler.feed(chunk)
    assert msg is not None
    assert msg.data["bpm"] == 140
    assert len(msg.data["channels"]) == 50
    assert len(msg.data["mixer_tracks"]) == 30
