"""
StudioMind CLI — connect to FL Studio, run commands, test the bridge.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from studiomind.bridge.midi_client import MidiClient, list_ports
from studiomind.bridge.commands import FLStudio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("studiomind")


def cmd_ports(args: argparse.Namespace) -> None:
    """List available MIDI ports."""
    ports = list_ports()
    print("\n=== MIDI Input Ports ===")
    for i, name in enumerate(ports["inputs"]):
        print(f"  [{i}] {name}")
    if not ports["inputs"]:
        print("  (none)")

    print("\n=== MIDI Output Ports ===")
    for i, name in enumerate(ports["outputs"]):
        print(f"  [{i}] {name}")
    if not ports["outputs"]:
        print("  (none)")

    print("\nTip: Install loopMIDI and create a port named 'StudioMind'.")


def cmd_ping(args: argparse.Namespace) -> None:
    """Test the connection to FL Studio."""
    with FLStudio() as fl:
        result = fl.ping()
        print(f"Connected! FL Studio API v{result['api_version']}, FL v{result['fl_version']}")


def cmd_state(args: argparse.Namespace) -> None:
    """Read and print the full project state."""
    with FLStudio() as fl:
        state = fl.read_project_state()
        print(json.dumps(state, indent=2))


def cmd_eq(args: argparse.Namespace) -> None:
    """Get or set EQ on a mixer track."""
    with FLStudio() as fl:
        if args.gain is not None or args.freq is not None or args.bw is not None:
            result = fl.set_eq(
                track_id=args.track,
                band=args.band,
                gain=args.gain,
                frequency=args.freq,
                bandwidth=args.bw,
            )
            print(f"EQ updated: {json.dumps(result, indent=2)}")
        else:
            result = fl.get_eq(args.track)
            print(f"EQ for track {args.track}: {json.dumps(result, indent=2)}")


def cmd_interactive(args: argparse.Namespace) -> None:
    """Interactive command shell."""
    with FLStudio() as fl:
        print("StudioMind connected. Type commands (ping, state, eq, quit):")
        while True:
            try:
                line = input("studiomind> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not line or line == "quit":
                break

            parts = line.split()
            cmd = parts[0]

            try:
                if cmd == "ping":
                    print(fl.ping())
                elif cmd == "state":
                    print(json.dumps(fl.read_project_state(), indent=2))
                elif cmd == "bpm":
                    print(f"BPM: {fl.get_bpm()}")
                elif cmd == "eq":
                    if len(parts) < 2:
                        print("Usage: eq <track_id> [band gain freq bw]")
                        continue
                    track = int(parts[1])
                    if len(parts) >= 4:
                        fl.set_eq(track, int(parts[2]), gain=float(parts[3]))
                        print("EQ set.")
                    else:
                        print(json.dumps(fl.get_eq(track), indent=2))
                elif cmd == "snapshot":
                    fl.snapshot(label=" ".join(parts[1:]) or "manual")
                    print("Snapshot saved.")
                elif cmd == "undo":
                    fl.revert()
                    print("Reverted.")
                elif cmd == "help":
                    print("Commands: ping, state, bpm, eq, snapshot, undo, quit")
                else:
                    print(f"Unknown command: {cmd}. Type 'help' for commands.")
            except Exception as e:
                print(f"Error: {e}")

    print("Disconnected.")


def main() -> None:
    parser = argparse.ArgumentParser(description="StudioMind — AI producer for FL Studio")
    sub = parser.add_subparsers(dest="command")

    # ports
    sub.add_parser("ports", help="List MIDI ports")

    # ping
    sub.add_parser("ping", help="Test FL Studio connection")

    # state
    sub.add_parser("state", help="Read full project state")

    # eq
    eq_parser = sub.add_parser("eq", help="Get/set mixer track EQ")
    eq_parser.add_argument("track", type=int, help="Mixer track ID")
    eq_parser.add_argument("--band", type=int, default=0, help="EQ band (0-2)")
    eq_parser.add_argument("--gain", type=float, help="Gain (0.0-1.0)")
    eq_parser.add_argument("--freq", type=float, help="Frequency (0.0-1.0)")
    eq_parser.add_argument("--bw", type=float, help="Bandwidth (0.0-1.0)")

    # interactive
    sub.add_parser("shell", help="Interactive command shell")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "ports": cmd_ports,
        "ping": cmd_ping,
        "state": cmd_state,
        "eq": cmd_eq,
        "shell": cmd_interactive,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
