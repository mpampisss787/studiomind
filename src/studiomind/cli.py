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
from studiomind.logging_setup import (
    USER_LOGS_DIR,
    bundle_logs,
    configure_session_logging,
    find_repo_root,
    latest_logs,
)

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


def cmd_debug_bundle(args: argparse.Namespace) -> None:
    """Copy the last N session log file(s) into the repo's debug/ folder
    so they can be ``git push``-ed for review."""
    n = max(1, args.last)
    logs = latest_logs(n)
    if not logs:
        print(f"No session logs found in {USER_LOGS_DIR}.")
        print("Run a session first (e.g. `studiomind web`) to generate one.")
        return

    repo = find_repo_root()
    if repo is None:
        print("Couldn't locate the studiomind repo.")
        print(f"Logs are in: {USER_LOGS_DIR}")
        print("Copy them manually into wherever you want them reviewed.")
        return

    copied = bundle_logs(last_n=n)
    print(f"Bundled {len(copied)} log file(s) into {repo / 'debug'}:")
    for p in copied:
        print(f"  → {p.relative_to(repo)}")
    print()
    print("Now run, from the repo root:")
    print("  git add debug/")
    print('  git commit -m "Debug logs"')
    print("  git push")


def cmd_install_device(args: argparse.Namespace) -> None:
    """Deploy ``scripts/device_StudioMind.py`` into FL's Hardware folder.

    Idempotent: matching hashes report "up to date" and exit 0.
    Stale + ``--check`` exits 1 so CI / shell automation can gate on
    it. Default (no flags) copies the bundled script into place.
    """
    from pathlib import Path
    from studiomind import device_install
    from studiomind.logging_setup import find_repo_root

    repo_root = find_repo_root()
    if repo_root is None:
        print("ERROR: cannot locate the studiomind repo (editable install required)")
        sys.exit(1)
    bundled = repo_root / "scripts" / device_install.DEVICE_SCRIPT_NAME
    target_dir = Path(args.target).expanduser() if args.target else None

    try:
        status = device_install.get_device_status(
            bundled_script=bundled, target_dir=target_dir,
        )
    except device_install.DeviceInstallError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if status.target_path is None:
        print("ERROR: cannot locate FL's Hardware folder.")
        print("  Pass --target with the full path, e.g.:")
        print(r"    --target 'C:\Users\<you>\Documents\Image-Line\FL Studio\Settings\Hardware'")
        sys.exit(1)

    print(f"Bundled : {status.bundled_path}")
    print(f"Deployed: {status.target_path}")
    print(f"State   : {status.state}")
    if status.bundled_hash:
        print(f"  bundled  sha256: {status.bundled_hash[:16]}...")
    if status.deployed_hash:
        print(f"  deployed sha256: {status.deployed_hash[:16]}...")

    if status.state == "up_to_date" and not args.force:
        print()
        print("[ok] Device script is already up to date. Nothing to do.")
        return

    if args.check:
        print()
        print(f"[stale] --check mode: not copying.")
        print("Re-run without --check to install.")
        sys.exit(1)

    try:
        new_status = device_install.install_device(
            bundled_script=bundled, target_dir=target_dir, force=args.force,
        )
    except device_install.DeviceInstallError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print()
    print(f"[installed] {new_status.target_path}")
    print()
    print("Now reload the script in FL:")
    print("  F10 -> MIDI -> toggle the StudioMind controller row OFF and ON,")
    print("  or restart FL Studio.")


def cmd_train(args: argparse.Namespace) -> None:
    """Launch the training-mode wizard.

    P4 ships the orchestrator + a stdin-driven readback provider; the
    full Anthropic-driven loop and web UI come in P5. For now,
    --dry-run is the recommended way to verify everything is wired
    without touching FL.
    """
    from pathlib import Path
    from studiomind.agent.learning_loop import (
        TrainingOrchestrator,
        resume_orchestrator,
    )
    from studiomind.learning import codegen as codegen_mod
    from studiomind.learning import session_state as ss
    from studiomind.learning.approval_tokens import ApprovalStore
    from studiomind.learning.calibration import ReadbackProvider
    from studiomind.logging_setup import find_repo_root

    plugin_name: str = args.plugin_name
    skill_name = args.skill_name or codegen_mod.default_skill_name(plugin_name)
    tool_name = args.tool_name or codegen_mod.default_tool_name(plugin_name)

    repo_root = find_repo_root()
    if repo_root is None:
        print("ERROR: cannot locate the studiomind repo (editable install?)")
        sys.exit(1)

    print(f"Training mode — acquiring '{plugin_name}'")
    print(f"  skill_name = {skill_name}")
    print(f"  tool_name  = {tool_name}")
    print(f"  fl_version = {args.fl_version}")
    print(f"  track={args.track} slot={args.slot}")
    print(f"  repo_root  = {repo_root}")

    if args.dry_run:
        print()
        print("Wizard plan:")
        print("  1. enumerate_plugin_params(track, slot)")
        print("  2. classify_param(id) for each enumerated param")
        print("  3. sweep_param(id) for each continuous param (6 readbacks)")
        print("  4. fit_param(id) — picks simplest curve with R² ≥ 0.99")
        print("  5. validate_param(id) — 4 deterministic probes")
        print("  6. codegen() + apply_writes(approval_token)")
        print("  7. run_pytest() — must pass before commit")
        print("  8. apply_commit(approval_token) — never pushes")
        print()
        print("--dry-run mode: nothing was written. Drop --dry-run when "
              "FL is open with the plugin loaded.")
        return

    # ── Live mode below — needs an actual FL bridge ──
    from studiomind.bridge.commands import FLStudio
    fl = FLStudio()
    fl.connect()
    try:
        provider = _StdinReadbackProvider()

        if args.resume:
            orch = resume_orchestrator(
                fl=fl, repo_root=repo_root,
                plugin_name=plugin_name, skill_name=skill_name,
                tool_name=tool_name, fl_version=args.fl_version,
                track_id=args.track, slot=args.slot,
                readback_provider=provider,
            )
            if orch is None:
                print("No in-flight session to resume.")
                sys.exit(1)
            print(f"Resumed at step '{orch.session.step}'.")
        else:
            if ss.load() is not None:
                print("WARNING: ~/StudioMind/state/training-session.json exists. "
                      "Pass --resume to continue, or `studiomind shell` and discard it.")
                sys.exit(1)
            orch = TrainingOrchestrator(
                fl=fl, repo_root=repo_root,
                plugin_name=plugin_name, skill_name=skill_name,
                tool_name=tool_name, fl_version=args.fl_version,
                track_id=args.track, slot=args.slot,
                readback_provider=provider,
                approval_store=ApprovalStore(),
            )

        # P4 ships the orchestrator + stdin provider; the full Anthropic-
        # driven walk-through is P5. For now we tell the user to use the
        # web UI (when shipped) or the shell to drive steps manually.
        print()
        print("[P4] CLI training is currently dry-run only — full agent flow "
              "lands in P5 with the /training web UI. Run `studiomind train "
              "<plugin> --track N --slot M --dry-run` to verify wiring.")
    finally:
        fl.disconnect()


def _maybe_warn_stale_device_script() -> None:
    """Run device_install.check_device_freshness() unless the user is
    explicitly running install-device (which has its own status print)."""
    # Cheap argv sniff — argparse hasn't run yet; we don't want the
    # warning fighting with install-device's own preview output.
    if any(a in ("install-device", "--help", "-h") for a in sys.argv[1:]):
        return
    try:
        from studiomind import device_install
        device_install.check_device_freshness()
    except Exception:
        # Boot-time hint is best-effort. Never block the CLI on it.
        pass


class _StdinReadbackProvider:
    """Tiny stdin-backed ReadbackProvider for CLI training. The web UI
    uses a websocket-future-backed provider (web/training_provider.py).
    Both accept the optional ``context`` kwarg per the protocol; this
    one ignores it — the structured fields are only useful for a UI."""

    def request(
        self,
        prompt: str,
        *,
        expected_unit: str = "",
        context: object = None,
    ) -> str:
        suffix = f" [{expected_unit}]" if expected_unit else ""
        try:
            return input(f"{prompt}{suffix} > ").strip()
        except EOFError:
            return ""


def main() -> None:
    # File logging on, always. CLI gets its session log automatically.
    configure_session_logging()

    # Best-effort: warn if FL's deployed device script is stale. Silent
    # when FL isn't installed or we're in a non-editable install. Skipped
    # for install-device itself (which prints its own status) so the
    # warning doesn't double-fire on the very command that fixes it.
    _maybe_warn_stale_device_script()

    parser = argparse.ArgumentParser(description="StudioMind — AI producer for FL Studio")
    sub = parser.add_subparsers(dest="command")

    # ports
    sub.add_parser("ports", help="List MIDI ports")

    # ping
    sub.add_parser("ping", help="Test FL Studio connection")

    # state
    sub.add_parser("state", help="Read full project state")

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

    # debug-bundle — copy latest session log into <repo>/debug/
    debug_parser = sub.add_parser(
        "debug-bundle",
        help="Copy the latest session log into <repo>/debug/ for sharing",
    )
    debug_parser.add_argument(
        "--last",
        type=int,
        default=1,
        help="Bundle the last N log files (default: 1, the most recent)",
    )

    # install-device — deploy device_StudioMind.py into FL's Hardware folder
    install_parser = sub.add_parser(
        "install-device",
        help="Copy scripts/device_StudioMind.py into FL's Hardware folder",
    )
    install_parser.add_argument(
        "--target", type=str, default=None,
        help="FL Hardware folder path (default: auto-discover)",
    )
    install_parser.add_argument(
        "--check", action="store_true",
        help="Report status without copying (exit 0 if up-to-date, 1 otherwise)",
    )
    install_parser.add_argument(
        "--force", action="store_true",
        help="Re-copy even when hashes already match",
    )

    # train — guided plugin acquisition (training mode)
    train_parser = sub.add_parser(
        "train",
        help="Acquire a plugin wrapper through guided dialogue (training mode, P4)",
    )
    train_parser.add_argument(
        "plugin_name", help="Display name of the plugin to acquire (e.g. 'Fruity Limiter')",
    )
    train_parser.add_argument(
        "--track", type=int, required=True,
        help="Mixer track index where the plugin is loaded",
    )
    train_parser.add_argument(
        "--slot", type=int, required=True,
        help="FX slot (0-9) where the plugin is loaded",
    )
    train_parser.add_argument(
        "--fl-version", default="21.2.10",
        help="FL Studio version for the manifest (default: 21.2.10)",
    )
    train_parser.add_argument(
        "--skill-name",
        help="Skill directory name (default: derived from plugin_name)",
    )
    train_parser.add_argument(
        "--tool-name",
        help="Mixing-agent tool name (default: 'set_<skill>'; drops 'fruity_' prefix)",
    )
    train_parser.add_argument(
        "--resume", action="store_true",
        help="Resume an in-flight session at ~/StudioMind/state/training-session.json",
    )
    train_parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate args + print the planned wizard; do not connect to FL",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "ports": cmd_ports,
        "ping": cmd_ping,
        "state": cmd_state,
        "project": cmd_project,
        "eq": cmd_eq,
        "agent": cmd_agent,
        "chat": cmd_chat,
        "web": cmd_web,
        "shell": cmd_interactive,
        "debug-bundle": cmd_debug_bundle,
        "train": cmd_train,
        "install-device": cmd_install_device,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
