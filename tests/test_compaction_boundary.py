"""
Regression tests for conversation compaction boundary handling.

The Anthropic API rejects any request where a `tool_result` block appears in a
user message without a matching `tool_use` in the immediately preceding
assistant message. Compaction splits the conversation into old (summarised)
and recent (kept verbatim); if the cut lands between an assistant(tool_use)
and its user(tool_result), the surviving tail starts with an orphan
tool_result and the API returns 400. Observed in prod 2026-04-23:

    messages.2.content.0: unexpected `tool_use_id` found in `tool_result`
    blocks. Each `tool_result` block must have a corresponding `tool_use`
    block in the previous message.

_find_compaction_cut is the fix. These tests pin its behaviour.
"""

from __future__ import annotations


def _user_text(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _assistant_tool_use(tool_id: str, name: str = "some_tool") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        ],
    }


def _user_tool_result(tool_id: str, result: str = "ok") -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": result},
        ],
    }


def _get_finder():
    from studiomind.agent.loop import AgentLoop
    return AgentLoop._find_compaction_cut


def test_helper_detects_tool_result():
    from studiomind.agent.loop import AgentLoop
    assert AgentLoop._msg_has_tool_result(_user_tool_result("toolu_1")) is True
    assert AgentLoop._msg_has_tool_result(_user_text("hi")) is False
    assert AgentLoop._msg_has_tool_result(_assistant_tool_use("toolu_1")) is False


def test_helper_detects_tool_use():
    from studiomind.agent.loop import AgentLoop
    assert AgentLoop._msg_has_tool_use(_assistant_tool_use("toolu_1")) is True
    assert AgentLoop._msg_has_tool_use(_assistant_text("hi")) is False
    assert AgentLoop._msg_has_tool_use(_user_tool_result("toolu_1")) is False


def test_cut_lands_on_orphan_tool_result_gets_walked_back():
    """The regression. Cut index lands on a user(tool_result). Must walk back."""
    find = _get_finder()
    messages = [
        _user_text("analyze the mix"),              # 0
        _assistant_tool_use("toolu_1"),             # 1
        _user_tool_result("toolu_1"),               # 2
        _assistant_tool_use("toolu_2"),             # 3
        _user_tool_result("toolu_2"),               # 4  ← naive cut at 4
        _assistant_text("here's the analysis"),     # 5
        _user_text("cut 2dB at 300Hz"),             # 6
        _assistant_tool_use("toolu_3"),             # 7
        _user_tool_result("toolu_3"),               # 8
        _assistant_text("done"),                    # 9
    ]
    # keep_count=6 => naive cut is len-6 = 4, which is a tool_result. Must move to 3.
    cut = find(messages, keep_count=6)
    assert cut == 3
    assert messages[cut]["role"] == "assistant"


def test_cut_on_clean_user_message_is_unchanged():
    find = _get_finder()
    messages = [
        _assistant_tool_use("toolu_1"),             # 0
        _user_tool_result("toolu_1"),               # 1
        _assistant_text("analysis"),                # 2
        _user_text("thanks, now cut 2dB"),          # 3  ← clean cut
        _assistant_tool_use("toolu_2"),             # 4
        _user_tool_result("toolu_2"),               # 5
    ]
    cut = find(messages, keep_count=3)
    assert cut == 3  # already clean, no adjustment needed


def test_cut_on_clean_assistant_text_is_unchanged():
    find = _get_finder()
    messages = [
        _user_text("start"),                        # 0
        _assistant_text("ok"),                      # 1  ← clean cut
        _user_text("continue"),                     # 2
        _assistant_text("done"),                    # 3
    ]
    cut = find(messages, keep_count=3)
    assert cut == 1
    assert messages[cut]["role"] == "assistant"


def test_multiple_consecutive_tool_pairs_walks_back_past_all():
    """If several consecutive tool pairs straddle the cut, walk back past all."""
    find = _get_finder()
    messages = [
        _user_text("start"),                        # 0
        _assistant_tool_use("toolu_1"),             # 1
        _user_tool_result("toolu_1"),               # 2  ← naive
        _assistant_tool_use("toolu_2"),             # 3
        _user_tool_result("toolu_2"),               # 4
    ]
    # keep_count=3 => naive cut=2 (tool_result). Walk back to 1 (tool_use) —
    # but this is on assistant(tool_use) whose tool_result follows (index 2),
    # so the mirror check confirms the boundary is safe.
    cut = find(messages, keep_count=3)
    assert cut == 1
    assert messages[cut]["role"] == "assistant"
    assert messages[cut + 1]["role"] == "user"
    # Verify the kept tail is a valid Anthropic payload:
    tail = messages[cut:]
    assert tail[0]["content"][0]["type"] == "tool_use"
    assert tail[1]["content"][0]["tool_use_id"] == tail[0]["content"][0]["id"]


def test_boundary_landing_on_tool_use_without_matching_result_walks_back():
    """A malformed conversation with a trailing tool_use gets backed off further."""
    find = _get_finder()
    messages = [
        _user_text("start"),                        # 0
        _assistant_text("ok"),                      # 1
        _user_text("continue"),                     # 2
        _assistant_tool_use("toolu_orphan"),        # 3  ← cut lands here, no result follows
    ]
    cut = find(messages, keep_count=1)
    # naive cut = 3 (assistant(tool_use) with no following tool_result).
    # Mirror check backs off to 2 (user text — clean).
    assert cut == 2
    assert messages[cut]["role"] == "user"


def test_short_history_returns_cut_at_start():
    find = _get_finder()
    messages = [_user_text("hi"), _assistant_text("hello")]
    cut = find(messages, keep_count=5)  # keep_count > len
    assert cut == 0


def test_empty_history():
    find = _get_finder()
    assert find([], keep_count=5) == 0
