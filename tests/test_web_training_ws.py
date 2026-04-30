"""End-to-end test of /ws/training with a scripted Anthropic client.

The test drives the websocket like a real browser would:

  1. Send {type:"start", ...} with plugin/track/slot.
  2. Receive `request_readback` events and reply with the canned
     readback the wizard expects.
  3. Receive `proposed_writes` and `proposed_commit` events and
     send back {type:"approve", token, action} over the WS.
  4. Wait for the `done` event with a non-empty commit_sha.

Relies on the same FakeFL + canned-readback recipe used by the
mock-LLM agent test, plus monkeypatched factories so the WS endpoint
builds the agent with a scripted Anthropic client (no real API key).
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ───────────────────────────── fakes (copy of test_learning_agent.py) ─

class FakeFL:
    PARAMS = [
        {"id": 0, "name": "Threshold", "default_value": 0.8},
        {"id": 1, "name": "Style", "default_value": 0.0},
        {"id": 2, "name": "Mix", "default_value": 1.0},
    ]

    def __init__(self) -> None:
        self.set_calls: list[dict] = []

    def get_plugin_params(self, track_id: int, slot: int) -> dict:
        return {"params": self.PARAMS}

    def set_plugin_param(self, track_id: int, slot: int, param_id: int, value: float) -> dict:
        self.set_calls.append({"track_id": track_id, "slot": slot,
                               "param_id": param_id, "value": value})
        return {"ok": True, "param_id": param_id, "new_value": value, "display": str(value)}

    def disconnect(self) -> None:
        pass


class _Block:
    def __init__(self, type: str, **kwargs: Any) -> None:
        self.type = type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason


def _text(text: str) -> _Block:
    return _Block("text", text=text)


def _tool_use(name: str, tool_input: dict | None = None, id: str | None = None) -> _Block:
    return _Block(
        "tool_use",
        name=name,
        input=tool_input or {},
        id=id or f"toolu_{name}_{secrets.token_hex(4)}",
    )


# Readback recipe (mirrors test_training_e2e.py)
CLASSIFY_THRESHOLD = ["-30 dB", "-15 dB"]
CLASSIFY_STYLE = ["hard", "smooth"]
CLASSIFY_MIX = ["25 %", "75 %"]
SWEEP_THRESHOLD = ["-60 dB", "-48 dB", "-36 dB", "-24 dB", "-12 dB", "0 dB"]
SWEEP_MIX = ["0 %", "20 %", "40 %", "60 %", "80 %", "100 %"]
VAL_PROBE_POINTS = [0.05, 0.30, 0.55, 0.95]
VAL_THRESHOLD = [f"{60.0 * p - 60.0:.4f} dB" for p in VAL_PROBE_POINTS]
VAL_MIX = [f"{100.0 * p:.4f} %" for p in VAL_PROBE_POINTS]


# ───────────────────────────── fixtures ───────────────────────────────


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skills_dir = repo / "src" / "studiomind" / "skills"
    skills_dir.mkdir(parents=True)
    (repo / "src" / "studiomind" / "__init__.py").write_text("")
    (skills_dir / "__init__.py").write_text("")
    (repo / "pyproject.toml").write_text("[project]\nname='t'\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


@pytest.fixture(autouse=True)
def _patch_sandbox(monkeypatch, repo_root: Path) -> None:
    from studiomind.learning import sandbox as sb
    monkeypatch.setattr(sb, "REPO_ROOT", repo_root.resolve())
    monkeypatch.setattr(
        sb, "SKILLS_DIR",
        (repo_root / "src" / "studiomind" / "skills").resolve(),
    )


@pytest.fixture(autouse=True)
def _patch_global_session_path(monkeypatch, tmp_path: Path) -> None:
    """Don't let a leftover ~/StudioMind/state/training-session.json
    fail a clean run. Each test gets its own session path."""
    from studiomind.learning import session_state as ss
    monkeypatch.setattr(ss, "SESSION_PATH", tmp_path / "training-session.json")


@pytest.fixture(autouse=True)
def _patch_mode_lock(monkeypatch, tmp_path: Path) -> None:
    from studiomind.learning import mode_lock
    monkeypatch.setattr(mode_lock, "LOCK_PATH", tmp_path / "mode_lock.json")


# ───────────────────────────── scripted Anthropic client ─────────────


def _scripted_response_specs() -> list[tuple[str, str, dict | None]]:
    """The canonical wizard tool-use sequence — same as
    test_learning_agent.py's response_specs."""
    return [
        ("tool_use", "enumerate", {}),
        ("tool_use", "classify_param", {"param_id": 0}),
        ("tool_use", "classify_param", {"param_id": 1}),
        ("tool_use", "classify_param", {"param_id": 2}),
        ("tool_use", "sweep_param", {"param_id": 0}),
        ("tool_use", "sweep_param", {"param_id": 2}),
        ("tool_use", "fit_param", {"param_id": 0}),
        ("tool_use", "fit_param", {"param_id": 2}),
        ("tool_use", "validate_param", {"param_id": 0}),
        ("tool_use", "validate_param", {"param_id": 2}),
        ("tool_use", "codegen", {}),
        ("tool_use", "request_writes_approval", {}),
        ("tool_use_with_writes_token", "apply_writes", {}),
        ("tool_use", "run_pytest", {}),
        ("tool_use", "build_commit_proposal", {}),
        ("tool_use", "request_commit_approval", {}),
        ("tool_use_with_commit_token", "apply_commit", {}),
        ("text", "Acquired Demo Plugin via training mode.", None),
    ]


def _make_scripted_client():
    """A scripted-response Anthropic client whose `messages.create`
    walks the wizard sequence, extracting the token from the previous
    tool_result for apply_writes / apply_commit."""
    response_specs = _scripted_response_specs()

    class ScriptedMessages:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self._cursor = 0

        def create(self, **kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            if self._cursor >= len(response_specs):
                return _Response([_text("Done.")], stop_reason="end_turn")
            kind, name_or_text, args = response_specs[self._cursor]
            self._cursor += 1
            messages = kwargs.get("messages", [])

            if kind == "text":
                return _Response([_text(name_or_text)], stop_reason="end_turn")

            if kind == "tool_use_with_writes_token":
                token = self._extract_last_token(messages)
                return _Response(
                    [_tool_use(name_or_text, {"token": token})],
                    stop_reason="tool_use",
                )

            if kind == "tool_use_with_commit_token":
                token = self._extract_last_token(messages)
                return _Response(
                    [_tool_use(name_or_text, {"token": token})],
                    stop_reason="tool_use",
                )

            return _Response(
                [_tool_use(name_or_text, args)],
                stop_reason="tool_use",
            )

        @staticmethod
        def _extract_last_token(messages: list[dict[str, Any]]) -> str:
            for msg in reversed(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        try:
                            payload = json.loads(block["content"])
                        except (ValueError, KeyError):
                            continue
                        if isinstance(payload, dict) and "token" in payload:
                            return payload["token"]
            raise RuntimeError("No token found in conversation history")

    class ScriptedClient:
        def __init__(self) -> None:
            self.messages = ScriptedMessages()

    return ScriptedClient()


# ───────────────────────────── the test ───────────────────────────────


def test_ws_training_drives_full_acquisition(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Walk a complete plugin acquisition over /ws/training using a
    scripted Anthropic client + readback queue. Asserts the final
    commit lands and the proposed_writes/commit events surface to
    the UI with valid tokens."""
    from studiomind.web import app as app_module

    # 1) Anthropic key gate
    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "sk-test-fake")
    # 2) Repo root
    from studiomind import logging_setup
    monkeypatch.setattr(logging_setup, "find_repo_root", lambda: repo_root)
    # 3) FL bridge
    fl_holder: dict[str, FakeFL] = {}

    def fake_build_fl():
        fl = FakeFL()
        fl_holder["fl"] = fl
        return fl

    monkeypatch.setattr(app_module, "_build_training_fl", fake_build_fl)

    # 4) Agent factory injects scripted client
    scripted = _make_scripted_client()

    def fake_build_agent(orch, *, on_message, on_tool_call,
                        on_tool_result, on_step):
        from studiomind.agent.learning_loop import (
            TrainingAgent, TrainingAgentConfig,
        )
        return TrainingAgent(
            orch,
            config=TrainingAgentConfig(
                model="claude-mock", max_turns=80,
                on_message=on_message, on_tool_call=on_tool_call,
                on_tool_result=on_tool_result, on_step=on_step,
            ),
            anthropic_client=scripted,
        )

    monkeypatch.setattr(app_module, "_build_training_agent", fake_build_agent)

    client = TestClient(app_module.app)
    # Reset registry between tests
    app_module._set_active_training(None)

    readbacks = (
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    readback_queue = list(readbacks)

    received: list[dict] = []
    proposed_writes_token: str | None = None
    proposed_commit_token: str | None = None
    final: dict | None = None

    with client.websocket_connect("/ws/training") as ws:
        ws.send_json({
            "type": "start",
            "plugin_name": "Demo Plugin",
            "skill_name": "demo_plugin",
            "tool_name": "set_demo",
            "fl_version": "21.2.10",
            "track_id": 4,
            "slot": 0,
        })

        while True:
            event = ws.receive_json()
            received.append(event)
            t = event.get("type")
            if t == "request_readback":
                if not readback_queue:
                    pytest.fail(
                        f"Readback queue exhausted but agent asked: {event['prompt']!r}"
                    )
                ws.send_json({"type": "readback", "value": readback_queue.pop(0)})
            elif t == "proposed_writes":
                proposed_writes_token = event["token"]
                assert isinstance(event["payload"], list) and event["payload"]
                ws.send_json({
                    "type": "approve",
                    "action": "writes",
                    "token": proposed_writes_token,
                })
            elif t == "proposed_commit":
                proposed_commit_token = event["token"]
                assert isinstance(event["proposal"], dict)
                ws.send_json({
                    "type": "approve",
                    "action": "commit",
                    "token": proposed_commit_token,
                })
            elif t == "done":
                final = event
                break
            elif t == "error":
                pytest.fail(f"Unexpected error: {event}")
            # step / tool_call / tool_result / approved / system: ignore for assertions

    assert final is not None, "No 'done' event received"
    assert final.get("step") == "done", f"Wizard ended at step {final.get('step')!r}"
    assert final.get("commit_sha")
    assert len(final["commit_sha"]) == 40

    # Both approval tokens must have surfaced as dedicated events.
    assert proposed_writes_token is not None
    assert proposed_commit_token is not None

    # Skill files landed on disk
    skill_dir = repo_root / "src" / "studiomind" / "skills" / "demo_plugin"
    for fname in ("manifest.json", "wrapper.py", "tool.py",
                  "knowledge.md", "tests.py", "__init__.py"):
        assert (skill_dir / fname).exists(), f"missing {fname}"

    # Commit landed in git with the trailer
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%B", final["commit_sha"]],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout
    assert "Skill-Acquired-Via: studiomind-training-mode" in msg
    assert "Skill-Name: demo_plugin" in msg

    # The active session registry was cleared on disconnect.
    assert app_module._get_active_training() is None


def test_ws_training_rejects_start_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If no Anthropic key is configured, the server tells the UI to
    finish setup before training can start."""
    from studiomind.web import app as app_module
    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "")

    client = TestClient(app_module.app)
    app_module._set_active_training(None)

    with client.websocket_connect("/ws/training") as ws:
        event = ws.receive_json()
        assert event["type"] == "needs_setup"


def test_ws_training_rejects_first_message_other_than_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from studiomind.web import app as app_module
    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "sk-test-fake")

    client = TestClient(app_module.app)
    app_module._set_active_training(None)

    with client.websocket_connect("/ws/training") as ws:
        ws.send_json({"type": "readback", "value": "x"})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert "start" in event["content"].lower()


def test_ws_training_streams_param_id_in_tool_results(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The per-param progress sidebar relies on tool_result events
    carrying param_id for classify/sweep/fit/validate. The orchestrator's
    tool outputs don't include it, so the WS layer pairs it from the
    matching tool_call.input."""
    from studiomind.web import app as app_module

    monkeypatch.setattr(app_module, "get_anthropic_key", lambda: "sk-test-fake")
    from studiomind import logging_setup
    monkeypatch.setattr(logging_setup, "find_repo_root", lambda: repo_root)

    fl_holder: dict[str, FakeFL] = {}
    def fake_build_fl():
        fl = FakeFL()
        fl_holder["fl"] = fl
        return fl
    monkeypatch.setattr(app_module, "_build_training_fl", fake_build_fl)

    scripted = _make_scripted_client()
    def fake_build_agent(orch, *, on_message, on_tool_call,
                        on_tool_result, on_step):
        from studiomind.agent.learning_loop import (
            TrainingAgent, TrainingAgentConfig,
        )
        return TrainingAgent(
            orch,
            config=TrainingAgentConfig(
                model="claude-mock", max_turns=80,
                on_message=on_message, on_tool_call=on_tool_call,
                on_tool_result=on_tool_result, on_step=on_step,
            ),
            anthropic_client=scripted,
        )
    monkeypatch.setattr(app_module, "_build_training_agent", fake_build_agent)

    client = TestClient(app_module.app)
    app_module._set_active_training(None)

    readbacks = (
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    rb = list(readbacks)

    classify_results: list[dict] = []
    sweep_results: list[dict] = []
    fit_results: list[dict] = []
    validate_results: list[dict] = []

    with client.websocket_connect("/ws/training") as ws:
        ws.send_json({
            "type": "start",
            "plugin_name": "Demo Plugin",
            "skill_name": "demo_plugin",
            "tool_name": "set_demo",
            "fl_version": "21.2.10",
            "track_id": 4,
            "slot": 0,
        })
        while True:
            ev = ws.receive_json()
            t = ev.get("type")
            if t == "request_readback":
                ws.send_json({"type": "readback", "value": rb.pop(0)})
            elif t == "proposed_writes":
                ws.send_json({"type": "approve", "action": "writes", "token": ev["token"]})
            elif t == "proposed_commit":
                ws.send_json({"type": "approve", "action": "commit", "token": ev["token"]})
            elif t == "tool_result":
                if ev["tool"] == "classify_param":
                    classify_results.append(ev["result"])
                elif ev["tool"] == "sweep_param":
                    sweep_results.append(ev["result"])
                elif ev["tool"] == "fit_param":
                    fit_results.append(ev["result"])
                elif ev["tool"] == "validate_param":
                    validate_results.append(ev["result"])
            elif t == "done":
                break
            elif t == "error":
                pytest.fail(f"Unexpected error: {ev}")

    # Three classify calls (params 0, 1, 2) — each result must carry param_id.
    assert len(classify_results) == 3
    classify_ids = sorted([r["param_id"] for r in classify_results])
    assert classify_ids == [0, 1, 2]

    # Two sweep calls (params 0, 2 — param 1 was enum).
    assert len(sweep_results) == 2
    assert sorted([r["param_id"] for r in sweep_results]) == [0, 2]

    # Two fit calls.
    assert len(fit_results) == 2
    assert sorted([r["param_id"] for r in fit_results]) == [0, 2]
    assert all(r.get("ok") for r in fit_results)

    # Two validate calls.
    assert len(validate_results) == 2
    assert sorted([r["param_id"] for r in validate_results]) == [0, 2]
    assert all(r.get("passed") for r in validate_results)
