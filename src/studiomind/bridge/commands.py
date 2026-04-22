"""
High-level command interface for FL Studio.

Wraps MidiClient with typed methods for each FL Studio operation.
"""

from __future__ import annotations

from typing import Any

from studiomind.bridge.midi_client import MidiClient


class FLStudio:
    """
    High-level interface to a running FL Studio instance.

    Usage:
        fl = FLStudio()
        fl.connect()
        state = fl.read_project_state()
        fl.set_eq(track_id=1, band=0, gain=0.4)
        fl.disconnect()
    """

    def __init__(self, client: MidiClient | None = None) -> None:
        self._client = client or MidiClient()

    @property
    def connected(self) -> bool:
        return self._client.connected

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.disconnect()

    def _call(self, method: str, **params: Any) -> Any:
        """Send a command and return the response data."""
        command = {"method": method}
        if params:
            command["params"] = {k: v for k, v in params.items() if v is not None}
        response = self._client.send(command)
        return response.data

    # ── Read Tools ──────────────────────────────────────────────

    def read_project_state(self) -> dict:
        """Read full project snapshot: BPM, channels, mixer tracks, patterns, routing."""
        return self._call("read_project_state")

    def read_mixer_track(self, track_id: int) -> dict:
        """Read detailed info for a single mixer track including all plugin params."""
        return self._call("read_mixer_track", track_id=track_id)

    def read_channel(self, channel_id: int) -> dict:
        """Read detailed info for a single channel."""
        return self._call("read_channel", channel_id=channel_id)

    # ── EQ Tools ────────────────────────────────────────────────

    def set_eq(
        self,
        track_id: int,
        band: int,
        gain: float | None = None,
        frequency: float | None = None,
        bandwidth: float | None = None,
    ) -> dict:
        """
        Set built-in 3-band EQ on a mixer track.

        Args:
            track_id: Mixer track index (0=master)
            band: EQ band (0=low, 1=mid, 2=high)
            gain: Gain (0.0-1.0 normalized, 0.5=unity)
            frequency: Center frequency (0.0-1.0 normalized)
            bandwidth: Bandwidth/Q (0.0-1.0 normalized)
        """
        return self._call(
            "set_eq",
            track_id=track_id,
            band=band,
            gain=gain,
            frequency=frequency,
            bandwidth=bandwidth,
        )

    def get_eq(self, track_id: int) -> dict:
        """Read the built-in 3-band EQ state for a mixer track."""
        return self._call("get_eq", track_id=track_id)

    # ── Plugin Tools ────────────────────────────────────────────

    def set_plugin_param(
        self,
        track_id: int,
        slot: int,
        param_id: int,
        value: float,
    ) -> dict:
        """
        Set a plugin parameter value.

        Args:
            track_id: Mixer track index
            slot: FX slot index (-1 for channel rack plugin)
            param_id: Parameter index
            value: Value (0.0-1.0 normalized)
        """
        return self._call(
            "set_plugin_param",
            track_id=track_id,
            slot=slot,
            param_id=param_id,
            value=value,
        )

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        """Read all parameters for a plugin at a mixer slot."""
        return self._call("get_plugin_params", track_id=track_id, slot=slot)

    # ── Mixer Tools ─────────────────────────────────────────────

    def set_mixer_volume(self, track_id: int, value: float) -> dict:
        """Set mixer track volume (0.0-1.0)."""
        return self._call("set_mixer_param", track_id=track_id, param="volume", value=value)

    def set_mixer_pan(self, track_id: int, value: float) -> dict:
        """Set mixer track pan (-1.0 to 1.0)."""
        return self._call("set_mixer_param", track_id=track_id, param="pan", value=value)

    def mute_track(self, track_id: int, muted: bool = True) -> dict:
        """Mute/unmute a mixer track."""
        return self._call("set_mixer_param", track_id=track_id, param="mute", value=int(muted))

    def solo_track(self, track_id: int, solo: bool = True) -> dict:
        """Solo/unsolo a mixer track."""
        return self._call("set_mixer_param", track_id=track_id, param="solo", value=int(solo))

    # ── Safety Tools ────────────────────────────────────────────

    def snapshot(self, label: str = "") -> dict:
        """Save FL Studio undo state (snapshot before destructive operations)."""
        return self._call("snapshot", label=label)

    def revert(self) -> dict:
        """Undo the last change."""
        return self._call("revert")

    # ── Transport ───────────────────────────────────────────────

    def play(self) -> dict:
        return self._call("transport", action="play")

    def stop(self) -> dict:
        return self._call("transport", action="stop")

    def get_bpm(self) -> float:
        result = self._call("get_bpm")
        return result["bpm"]

    # ── Lifecycle ───────────────────────────────────────────────

    def ping(self) -> dict:
        """Test the connection. Returns FL Studio version info."""
        return self._call("ping")

    def __enter__(self) -> FLStudio:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()
