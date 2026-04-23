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


def cmd_project(args: argparse.Namespace) -> None:
    """Detect the active FL project and open its StudioMind workspace.

    Detection priority:
      1. --name CLI override (always wins)
      2. FL Python API (general.getName / getFilename) — empty on FL 2025
      3. OS window title of FL.exe (via Windows user32) — the reliable fallback
      4. "untitled"
    """
    from studiomind.fl_detect import detect_fl_project, enumerate_all_visible_windows
    from studiomind.workspace import open_project, project_name_from_fl_path

    if getattr(args, "list_windows", False):
        print("=== All visible top-level windows ===")
        for t in enumerate_all_visible_windows():
            print(f"  {t!r}")
        return

    fl_info: dict = {}
    try:
        with FLStudio() as fl:
            fl_info = fl.get_project_name()
    except Exception as e:
        print(f"[warn] Could not reach FL for project metadata: {e}")

    os_name, os_title = detect_fl_project()

    print("=== FL API response ===")
    print(json.dumps(fl_info, indent=2) if fl_info else "  (no response)")
    print("\n=== OS window title ===")
    print(f"  title: {os_title!r}")
    print(f"  parsed project: {os_name!r}")

    override = getattr(args, "name", None)
    name = (
        override
        or fl_info.get("name")
        or project_name_from_fl_path(fl_info.get("path"))
        or os_name
        or "untitled"
    )

    project = open_project(name, fl_project_path=fl_info.get("path") or None)
    print(f"\n=== StudioMind workspace ===")
    print(f"  name:     {project.name}")
    print(f"  root:     {project.root}")
    print(f"  stems:    {project.stems_dir}")
    print(f"  masters:  {project.masters_dir}")
    print(f"  manifest: {project.manifest_path}")


def _open_active_workspace(fl: FLStudio) -> "WorkspaceSession":
    """Detect the active FL project and return a started WorkspaceSession for it."""
    from studiomind.fl_detect import detect_fl_project
    from studiomind.workspace import WorkspaceSession, open_project, project_name_from_fl_path

    fl_info = {}
    try:
        fl_info = fl.get_project_name()
    except Exception:
        pass

    os_name, _ = detect_fl_project()
    name = (
        fl_info.get("name")
        or project_name_from_fl_path(fl_info.get("path"))
        or os_name
        or "untitled"
    )
    project = open_project(name, fl_project_path=fl_info.get("path") or None)
    session = WorkspaceSession(fl, project)
    session.start()
    return session


def cmd_test_autorender(args: argparse.Namespace) -> None:
    """Diagnose why auto-render is or isn't working."""
    print("\n=== Auto-render diagnostic ===\n")

    # 1. pywinauto import
    try:
        from pywinauto import Desktop
        from pywinauto.keyboard import send_keys  # noqa: F401
        print("  [OK] pywinauto is installed and importable")
    except ImportError as e:
        print(f"  [FAIL] pywinauto import failed: {e}")
        print("  Fix: python -m pip install pywinauto")
        print("       Then restart the StudioMind web server.")
        return

    # 2. Find FL Studio window
    try:
        desktop = Desktop(backend="uia")
        all_windows = [(w.window_text() or "") for w in desktop.windows() if w.window_text()]
        fl_wins = [t for t in all_windows if "FL Studio" in t]
        if fl_wins:
            print(f"  [OK] FL Studio window found: {fl_wins[0]!r}")
        else:
            print("  [FAIL] No FL Studio window found.")
            print("  Make sure FL Studio is open and not minimized.")
            print("  All visible windows:", all_windows[:10])
            return
    except Exception as e:
        print(f"  [FAIL] Could not enumerate windows: {e}")
        return

    # 3. Can we focus it?
    try:
        fl_win = [w for w in desktop.windows() if "FL Studio" in (w.window_text() or "")][0]
        fl_win.set_focus()
        print("  [OK] FL Studio window focused successfully")
    except Exception as e:
        print(f"  [WARN] Could not focus FL window: {e}")
        print("  Auto-render may still work but is less reliable.")

    print("\n  Auto-render should work. Make sure:")
    print("  1. The StudioMind web server was restarted after installing pywinauto")
    print("  2. FL's export path is set to your workspace stems/ folder")
    print("     (do one manual File->Export->WAV to save the path)")
    print()


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

    config_kwargs: dict[str, Any] = {
        "auto_approve": args.auto,
        "on_message": on_message,
        "on_tool_call": on_tool_call,
        "on_tool_result": on_tool_result,
    }
    if args.model:
        config_kwargs["model"] = args.model
    config = AgentConfig(**config_kwargs)

    print(f"Connecting to FL Studio...")
    with FLStudio() as fl:
        workspace = _open_active_workspace(fl)
        print(f"Connected. Project: {workspace.project.name}. Running agent with goal: {goal}\n")
        agent = AgentLoop(fl, config, workspace=workspace)
        try:
            result = agent.run(goal)
        except KeyboardInterrupt:
            print("\n\nAgent interrupted by user.")
            result = None
        finally:
            workspace.stop()

        print(f"\n{agent.action_log.summary()}")


def cmd_chat(args: argparse.Namespace) -> None:
    """Interactive agent chat — multiple goals in one session."""
    from studiomind.agent.loop import AgentConfig, AgentLoop

    def on_message(text: str) -> None:
        print(f"\n{text}")

    def on_tool_call(tool_name: str, tool_input: dict) -> bool:
        print(f"\n  [{tool_name}({json.dumps(tool_input, default=str)[:100]})]")
        return True  # Auto-approve in chat mode

    config_kwargs: dict[str, Any] = {
        "auto_approve": True,
        "on_message": on_message,
        "on_tool_call": on_tool_call,
    }
    model_arg = getattr(args, "model", None)
    if model_arg:
        config_kwargs["model"] = model_arg
    config = AgentConfig(**config_kwargs)

    print("Connecting to FL Studio...")
    with FLStudio() as fl:
        workspace = _open_active_workspace(fl)
        try:
            agent = AgentLoop(fl, config, workspace=workspace)
            print(f"Connected. Project: {workspace.project.name}. Chat with StudioMind (Ctrl+C to quit):\n")
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
        finally:
            workspace.stop()

    print("\nDisconnected.")


def cmd_web(args: argparse.Namespace) -> None:
    """Launch the web chat UI."""
    try:
        from studiomind.web.app import start
    except ImportError:
        print("Web UI requires extra dependencies. Install with:")
        print("  pip install studiomind[web]")
        return
    start(host=args.host, port=args.port, reload=getattr(args, "reload", False))


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

    # test-autorender
    sub.add_parser("test-autorender", help="Check if auto-render via pywinauto is working")

    # project
    project_parser = sub.add_parser(
        "project", help="Show FL project name and open StudioMind workspace"
    )
    project_parser.add_argument(
        "--name", type=str, help="Override auto-detection (use a specific project name)"
    )
    project_parser.add_argument(
        "--list-windows",
        action="store_true",
        help="Dump all visible window titles (diagnostic for FL detection)",
    )

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

    # web
    web_parser = sub.add_parser("web", help="Launch web chat UI")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=8040, help="Port (default: 8040)")
    web_parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev mode)")

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
        "project": cmd_project,
        "test-autorender": cmd_test_autorender,
        "eq": cmd_eq,
        "agent": cmd_agent,
        "chat": cmd_chat,
        "web": cmd_web,
        "shell": cmd_interactive,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
