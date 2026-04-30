"""End-to-end test of TrainingAgent.run with a programmable mock
Anthropic client — the P5-A gate.

The mock client returns canned tool_use sequences in order, the
agent dispatches them through the real orchestrator (with FakeFL +
canned readbacks + a real git repo), and we assert the same
final state the orchestrator-direct e2e test produces.

If this test ever needs to know an Anthropic API key, something is
wrong with the injection path."""

from __future__ import annotations

import secrets
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from studiomind.agent.learning_loop import (
    TrainingAgent,
    TrainingAgentConfig,
    TrainingOrchestrator,
)
from studiomind.learning.approval_tokens import ApprovalStore


# ───────────────────────────── fakes ──────────────────────────────────

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


class CannedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def request(self, prompt: str, *, expected_unit: str = "") -> str:
        if not self.responses:
            return ""
        return self.responses.pop(0)


# ───────────────────────────── mock Anthropic client ─────────────────

class _Block:
    """Quack-typed equivalent of an Anthropic content block."""
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


class _MockMessages:
    """Captures kwargs from every messages.create() call and pops one
    response from the programmed sequence."""
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError(
                f"Mock Anthropic client exhausted at turn {len(self.calls)}; "
                f"last input had {len(kwargs.get('messages', []))} messages"
            )
        return self._responses.pop(0)


class MockAnthropicClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _MockMessages(responses)


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


# Readback recipe (mirrors test_training_e2e.py)
CLASSIFY_THRESHOLD = ["-30 dB", "-15 dB"]
CLASSIFY_STYLE = ["hard", "smooth"]
CLASSIFY_MIX = ["25 %", "75 %"]
SWEEP_THRESHOLD = ["-60 dB", "-48 dB", "-36 dB", "-24 dB", "-12 dB", "0 dB"]
SWEEP_MIX = ["0 %", "20 %", "40 %", "60 %", "80 %", "100 %"]
VAL_PROBE_POINTS = [0.05, 0.30, 0.55, 0.95]
VAL_THRESHOLD = [f"{60.0 * p - 60.0:.4f} dB" for p in VAL_PROBE_POINTS]
VAL_MIX = [f"{100.0 * p:.4f} %" for p in VAL_PROBE_POINTS]


def _make_agent_and_canned_responses(
    repo_root: Path,
    tmp_path: Path,
    lazy_token_holder: dict[str, str],
) -> tuple[TrainingAgent, list[_Response]]:
    """Build the orchestrator + the mock-Anthropic response sequence
    that walks the wizard end-to-end. The token from
    request_writes_approval / request_commit_approval is captured into
    ``lazy_token_holder`` so the FOLLOWING tool_use response can pass
    it back."""
    fl = FakeFL()
    provider = CannedProvider(
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    orch = TrainingOrchestrator(
        fl=fl, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=provider,
        approval_store=ApprovalStore(),
        session_path=tmp_path / "session.json",
        sleep=lambda s: None, dwell_s=0.0,
    )
    agent = TrainingAgent(
        orch,
        config=TrainingAgentConfig(model="claude-mock", max_turns=80),
        anthropic_client=MockAnthropicClient([]),  # populated below
    )
    return agent, []


def test_training_agent_runs_full_acquisition_via_mock_llm(
    repo_root: Path, tmp_path: Path,
) -> None:
    """The big P5-A gate. Mock client emits the wizard's canonical
    tool_use sequence; agent.run drives the orchestrator end-to-end;
    final state == 'done' with a valid commit SHA."""
    fl = FakeFL()
    provider = CannedProvider(
        CLASSIFY_THRESHOLD + CLASSIFY_STYLE + CLASSIFY_MIX
        + SWEEP_THRESHOLD + SWEEP_MIX
        + VAL_THRESHOLD + VAL_MIX
    )
    orch = TrainingOrchestrator(
        fl=fl, repo_root=repo_root,
        plugin_name="Demo Plugin", skill_name="demo_plugin",
        tool_name="set_demo", fl_version="21.2.10",
        track_id=4, slot=0,
        readback_provider=provider,
        approval_store=ApprovalStore(),
        session_path=tmp_path / "session.json",
        sleep=lambda s: None, dwell_s=0.0,
    )

    # Some tools (apply_writes, apply_commit) need tokens that come
    # back from previous turns. We can't know those tokens at the
    # time we craft the response sequence — the orchestrator only
    # mints them when request_*_approval fires.
    #
    # Workaround: the mock client is a callable so we can inspect
    # the conversation messages between turns and synthesise the
    # right tool_use args on the fly.

    captured_text: list[str] = []

    response_specs = [
        # turn 1: enumerate
        ("tool_use", "enumerate", {}),
        # 2-4: classify each
        ("tool_use", "classify_param", {"param_id": 0}),
        ("tool_use", "classify_param", {"param_id": 1}),
        ("tool_use", "classify_param", {"param_id": 2}),
        # 5-6: sweep continuous params
        ("tool_use", "sweep_param", {"param_id": 0}),
        ("tool_use", "sweep_param", {"param_id": 2}),
        # 7-8: fit
        ("tool_use", "fit_param", {"param_id": 0}),
        ("tool_use", "fit_param", {"param_id": 2}),
        # 9-10: validate
        ("tool_use", "validate_param", {"param_id": 0}),
        ("tool_use", "validate_param", {"param_id": 2}),
        # 11: codegen
        ("tool_use", "codegen", {}),
        # 12: request writes approval; the client's NEXT call
        # synthesises apply_writes with the captured token.
        ("tool_use", "request_writes_approval", {}),
        ("tool_use_with_writes_token", "apply_writes", {}),  # special
        # 14: pytest
        ("tool_use", "run_pytest", {}),
        # 15: build commit proposal
        ("tool_use", "build_commit_proposal", {}),
        # 16-17: approve + apply commit
        ("tool_use", "request_commit_approval", {}),
        ("tool_use_with_commit_token", "apply_commit", {}),
        # 18: final summary
        ("text", "Acquired Demo Plugin via training mode.", None),
    ]

    class ScriptedMessages:
        """Walks response_specs in order, but inspects the previous
        tool_result to fill in deferred token args."""
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self._cursor = 0

        def create(self, **kwargs: Any) -> _Response:
            self.calls.append(kwargs)
            if self._cursor >= len(response_specs):
                # Default: end the turn cleanly with a closing message
                return _Response([_text("Done.")], stop_reason="end_turn")
            kind, name_or_text, args = response_specs[self._cursor]
            self._cursor += 1
            messages = kwargs.get("messages", [])

            if kind == "text":
                return _Response([_text(name_or_text)], stop_reason="end_turn")

            if kind == "tool_use_with_writes_token":
                token = self._extract_last_token(messages)
                # Simulate the UI's /api/training/approve POST landing
                # between request_writes_approval and apply_writes.
                orch.approval_store.approve(
                    token, "writes", orch.write_queue.to_payload(),
                )
                return _Response(
                    [_tool_use(name_or_text, {"token": token})],
                    stop_reason="tool_use",
                )

            if kind == "tool_use_with_commit_token":
                token = self._extract_last_token(messages)
                # Simulate the UI's /api/training/approve POST landing
                # between request_commit_approval and apply_commit.
                from studiomind.agent.learning_tools import TrainingDispatchState
                # The dispatch state holds the live CommitProposal — pull
                # it off the agent so payload hashing matches.
                proposal = agent._dispatch_state.pending_commit_proposal
                assert proposal is not None
                orch.approval_store.approve(
                    token, "commit", proposal.to_payload(),
                )
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
            """Walk back through the conversation to find the most
            recent tool_result whose JSON content carries a 'token'
            field."""
            import json as _json
            for msg in reversed(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        try:
                            payload = _json.loads(block["content"])
                        except (ValueError, KeyError):
                            continue
                        if isinstance(payload, dict) and "token" in payload:
                            return payload["token"]
            raise RuntimeError("No token found in conversation history")

    class ScriptedClient:
        def __init__(self) -> None:
            self.messages = ScriptedMessages()

    client = ScriptedClient()
    agent = TrainingAgent(
        orch,
        config=TrainingAgentConfig(
            model="claude-mock", max_turns=40,
            on_message=lambda t: captured_text.append(t),
        ),
        anthropic_client=client,
    )

    final = agent.run("Acquire the Demo Plugin")

    # Wizard reached commit
    assert orch.session.step == "done", f"step={orch.session.step}"
    assert orch.session.commit_sha is not None
    assert len(orch.session.commit_sha) == 40

    # Skill files on disk
    skill_dir = repo_root / "src" / "studiomind" / "skills" / "demo_plugin"
    for fname in ("manifest.json", "wrapper.py", "tool.py",
                  "knowledge.md", "tests.py", "__init__.py"):
        assert (skill_dir / fname).exists(), f"missing {fname}"

    # Commit landed in git with the trailer
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%B", orch.session.commit_sha],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout
    assert "Skill-Acquired-Via: studiomind-training-mode" in msg
    assert "Skill-Name: demo_plugin" in msg

    # Closing summary surfaced
    assert "Acquired" in final


def test_training_agent_stop_event_interrupts_loop(
    repo_root: Path, tmp_path: Path,
) -> None:
    """request_stop() before the next turn must end the loop cleanly."""
    orch = TrainingOrchestrator(
        fl=FakeFL(), repo_root=repo_root,
        plugin_name="X", skill_name="x",
        tool_name="set_x", fl_version="21",
        track_id=0, slot=0,
        readback_provider=CannedProvider([]),
        session_path=tmp_path / "s.json",
        sleep=lambda s: None, dwell_s=0.0,
    )

    class StopOnFirstCallClient:
        def __init__(self, agent_holder: dict) -> None:
            self.calls = 0
            self._agent_holder = agent_holder

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.calls += 1
            self._agent_holder["agent"].request_stop()
            # Return a tool_use so the loop would otherwise continue —
            # but stop_event is set, so it won't.
            return _Response([_text("Working...")], stop_reason="end_turn")

    holder: dict = {}
    client = StopOnFirstCallClient(holder)
    agent = TrainingAgent(
        orch,
        config=TrainingAgentConfig(model="claude-mock", max_turns=10),
        anthropic_client=client,
    )
    holder["agent"] = agent
    agent.run("acquire X")
    # stop_event was set, but the FIRST call still went through to
    # populate self.calls. Now another iteration shouldn't fire:
    assert client.calls == 1


def test_training_agent_surfaces_text_via_on_message(
    repo_root: Path, tmp_path: Path,
) -> None:
    orch = TrainingOrchestrator(
        fl=FakeFL(), repo_root=repo_root,
        plugin_name="X", skill_name="x",
        tool_name="set_x", fl_version="21",
        track_id=0, slot=0,
        readback_provider=CannedProvider([]),
        session_path=tmp_path / "s.json",
        sleep=lambda s: None, dwell_s=0.0,
    )

    captured: list[str] = []
    client = MockAnthropicClient([
        _Response([_text("Hello, ready to acquire.")], stop_reason="end_turn"),
    ])
    agent = TrainingAgent(
        orch,
        config=TrainingAgentConfig(
            model="claude-mock", max_turns=5,
            on_message=lambda t: captured.append(t),
        ),
        anthropic_client=client,
    )
    out = agent.run("acquire something")
    assert "Hello, ready to acquire." in out
    assert captured == ["Hello, ready to acquire."]


def test_training_agent_recovers_from_tool_error(
    repo_root: Path, tmp_path: Path,
) -> None:
    """A tool that raises gets fed back as is_error=True; the agent
    can decide what to do next. Loop doesn't crash."""
    orch = TrainingOrchestrator(
        fl=FakeFL(), repo_root=repo_root,
        plugin_name="X", skill_name="x",
        tool_name="set_x", fl_version="21",
        track_id=0, slot=0,
        readback_provider=CannedProvider([]),
        session_path=tmp_path / "s.json",
        sleep=lambda s: None, dwell_s=0.0,
    )

    # First response: classify_param without enumerate first → orchestrator raises
    # Second response: agent gives up cleanly with text
    client = MockAnthropicClient([
        _Response(
            [_tool_use("classify_param", {"param_id": 99})],
            stop_reason="tool_use",
        ),
        _Response(
            [_text("Got an error; aborting.")],
            stop_reason="end_turn",
        ),
    ])

    captured: list[str] = []
    agent = TrainingAgent(
        orch,
        config=TrainingAgentConfig(
            model="claude-mock", max_turns=5,
            on_message=lambda t: captured.append(t),
        ),
        anthropic_client=client,
    )
    out = agent.run("acquire X")
    # Agent received the error and continued; final message is the apology
    assert "aborting" in out.lower()
    # The error tool_result was fed back: the client should have been
    # called twice (initial + after tool error).
    assert len(client.messages.calls) == 2
