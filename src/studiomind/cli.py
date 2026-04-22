"""
StudioMind CLI — connect to FL Studio, run commands, test the bridge.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

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


def cmd_agent(args: argparse.Namespace) -> None:
    """Run the AI agent with a natural language goal."""
    from studiomind.agent.loop import AgentConfig, AgentLoop

    goal = " ".join(args.goal)
    if not goal:
        print("Usage: studiomind agent <goal>")
        print('Example: studiomind agent "Mix this professionally"')
        return

    def on_message(text: str) -> None:
        print(f"\n{text}")

    def on_tool_call(tool_name: str, tool_input: dict) -> bool:
        print(f"\n  [Agent wants to: {tool_name}({json.dumps(tool_input, default=str)})]")
        if args.auto:
            print("  [Auto-approved]")
            return True
        try:
            answer = input("  Approve? [Y/n] ").strip().lower()
            return answer in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def on_tool_result(tool_name: str, result: Any) -> None:
        # Show brief result for read operations
        if tool_name.startswith("read_") or tool_name == "analyze_audio":
            preview = json.dumps(result, default=str)
            if len(preview) > 200:
                preview = preview[:200] + "..."
            print(f"  [Result: {preview}]")
        elif isinstance(result, dict) and result.get("ok"):
            print(f"  [Done: {tool_name}]")

    config = AgentConfig(
        model=args.model or "claude-sonnet-4-5-20250929",
        auto_approve=args.auto,
        on_message=on_message,
        on_tool_call=on_tool_call,
        on_tool_result=on_tool_result,
    )

    print(f"Connecting to FL Studio...")
    with FLStudio() as fl:
        print(f"Connected. Running agent with goal: {goal}\n")
        agent = AgentLoop(fl, config)
        try:
            result = agent.run(goal)
        except KeyboardInterrupt:
            print("\n\nAgent interrupted by user.")
            result = None

        print(f"\n{agent.action_log.summary()}")


def cmd_chat(args: argparse.Namespace) -> None:
    """Interactive agent chat — multiple goals in one session."""
    from studiomind.agent.loop import AgentConfig, AgentLoop

    def on_message(text: str) -> None:
        print(f"\n{text}")

    def on_tool_call(tool_name: str, tool_input: dict) -> bool:
        print(f"\n  [{tool_name}({json.dumps(tool_input, default=str)[:100]})]")
        return True  # Auto-approve in chat mode

    config = AgentConfig(
        model=args.model if hasattr(args, "model") and args.model else "claude-sonnet-4-5-20250929",
        auto_approve=True,
        on_message=on_message,
        on_tool_call=on_tool_call,
    )

    print("Connecting to FL Studio...")
    with FLStudio() as fl:
        agent = AgentLoop(fl, config)
        print("Connected. Chat with StudioMind (Ctrl+C to quit):\n")
        first_message = True
        while True:
            try:
                goal = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not goal:
                continue
            if goal.lower() in ("quit", "exit"):
                break
            try:
                agent.run(goal, continue_conversation=not first_message)
                first_message = False
            except KeyboardInterrupt:
                print("\n[Interrupted]")
            except Exception as e:
                print(f"\n[Error: {e}]")

    print("\nDisconnected.")


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

    # agent
    agent_parser = sub.add_parser("agent", help="Run AI agent with a natural language goal")
    agent_parser.add_argument("goal", nargs="+", help="What you want the agent to do")
    agent_parser.add_argument("--model", type=str, help="Claude model (default: sonnet)")
    agent_parser.add_argument("--auto", action="store_true", help="Auto-approve destructive actions")

    # chat
    chat_parser = sub.add_parser("chat", help="Interactive agent chat session")
    chat_parser.add_argument("--model", type=str, help="Claude model (default: sonnet)")

    # shell (low-level)
    sub.add_parser("shell", help="Low-level command shell (no AI)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "ports": cmd_ports,
        "ping": cmd_ping,
        "state": cmd_state,
        "eq": cmd_eq,
        "agent": cmd_agent,
        "chat": cmd_chat,
        "shell": cmd_interactive,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
