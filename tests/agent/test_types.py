"""M1 单元测试:`to_openai_messages` 序列化(AgentMessage → OpenAI Chat Completions 消息数组)。

覆盖:
- assistant 带 tool_calls → 透传 id / type / function{name, arguments}
- assistant content 为空但带 tool_calls → content 序列化为 null
- assistant 无 tool_calls → 普通 role+content,不含 tool_calls 字段
- tool 消息 → role=tool + tool_call_id + content
- system / user 消息透传
- 完整混合消息历史的结构与顺序
"""

from __future__ import annotations

from typing import Any, cast

from agent.types import AgentMessage, ToolCall, to_openai_messages


def test_assistant_with_tool_calls_serialized() -> None:
    m = AgentMessage(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="call_1", name="read_file", arguments='{"path":"a.py"}')],
    )
    out = to_openai_messages([m])
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] is None  # 空 content 序列化为 null
    assert out[0]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        }
    ]


def test_assistant_multiple_tool_calls_order_preserved() -> None:
    m = AgentMessage(
        role="assistant",
        tool_calls=[
            ToolCall(id="a", name="read", arguments="{}"),
            ToolCall(id="b", name="write", arguments="{}"),
        ],
    )
    out = to_openai_messages([m])
    tcs = cast(list[dict[str, Any]], out[0]["tool_calls"])
    assert [t["id"] for t in tcs] == ["a", "b"]
    assert [t["function"]["name"] for t in tcs] == ["read", "write"]


def test_assistant_without_tool_calls_is_plain() -> None:
    m = AgentMessage(role="assistant", content="hi")
    out = to_openai_messages([m])
    assert out[0] == {"role": "assistant", "content": "hi"}
    assert "tool_calls" not in out[0]


def test_tool_message_serialized() -> None:
    m = AgentMessage(role="tool", content="文件内容", tool_call_id="call_1")
    out = to_openai_messages([m])
    assert out[0] == {"role": "tool", "tool_call_id": "call_1", "content": "文件内容"}


def test_system_and_user_passthrough() -> None:
    msgs = [
        AgentMessage(role="system", content="sys"),
        AgentMessage(role="user", content="hello"),
    ]
    out = to_openai_messages(msgs)
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


def test_full_mixed_history_sequence() -> None:
    msgs = [
        AgentMessage(role="system", content="sys"),
        AgentMessage(role="user", content="task"),
        AgentMessage(
            role="assistant",
            tool_calls=[ToolCall(id="call_1", name="read_file", arguments="{}")],
        ),
        AgentMessage(role="tool", content="ok", tool_call_id="call_1"),
        AgentMessage(role="assistant", content="final"),
    ]
    out = to_openai_messages(msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "assistant"]
    tcs = cast(list[dict[str, object]], out[2]["tool_calls"])
    assert tcs[0]["id"] == "call_1"
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}
    assert out[4] == {"role": "assistant", "content": "final"}


def test_empty_message_list_returns_empty() -> None:
    assert to_openai_messages([]) == []
