"""M1 端到端测试:httpx.MockTransport 模拟多轮完整 ReAct 流程。

场景:读→写→读回→最终回答(四轮模型调用),验证:
- 消息历史结构(system→user→assistant/tool 交替→最终回答)
- assistant 的 tool_calls 与紧随的 tool 消息 tool_call_id 精确匹配
- 发送给 provider 的请求 payload 结构与 to_openai_messages 契约一致
- 流式客户端携带 stream_options.include_usage / tool_choice=auto
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent.config import LLMConfig
from agent.loop import build_agent
from agent.types import AgentMessage, AgentResult
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block

SYSTEM = "你是安全编码助手"

TOOLS_SPEC = {
    "read_file": {
        "description": "读文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "write_file": {
        "description": "写文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}


async def _run(
    requests: list[str],
    dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
) -> tuple[list[AgentMessage], list[dict[str, Any]]]:
    """运行 loop,返回 (messages, 捕获到的请求 payload 列表)。"""
    request_iter = iter(requests)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=next(request_iter))

    config = LLMConfig(base_url="https://test.example", api_key="k", model="m", max_steps=10)
    loop = build_agent(config, TOOLS_SPEC, dispatch, system_prompt=SYSTEM, transport=httpx.MockTransport(handler))
    messages = await loop.run("修复 math_utils 的缺陷")
    return messages, captured


async def test_full_react_flow_read_write_readback_final() -> None:
    # 请求1: read_file;请求2: write_file;请求3: read_file(读回);请求4: 最终回答
    r1 = tool_delta(0, id_="call_read", name="read_file", args='{"path":"math_utils.py"}') + finish("tool_calls") + usage_block()
    r2 = tool_delta(0, id_="call_write", name="write_file", args='{"path":"math_utils.py","content":"# fixed"}') + finish("tool_calls") + usage_block()
    r3 = tool_delta(0, id_="call_readback", name="read_file", args='{"path":"math_utils.py"}') + finish("tool_calls") + usage_block()
    r4 = text_delta("修复完成") + finish() + usage_block()

    file_state = {"content": "def divide():\n    pass\n"}

    async def dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        if name == "read_file":
            return AgentResult(content=file_state["content"])
        if name == "write_file":
            file_state["content"] = str(args["content"])
            return AgentResult(content="写入成功")
        return AgentResult(content="?", is_error=True)

    messages, captured = await _run([r1, r2, r3, r4], dispatch)

    # ---- 消息历史结构 ----
    assert [m.role for m in messages] == [
        "system", "user",
        "assistant", "tool",
        "assistant", "tool",
        "assistant", "tool",
        "assistant",
    ]
    assert messages[0].role == "system" and messages[0].content == SYSTEM
    assert messages[1].role == "user" and "math_utils" in messages[1].content

    # 每对 assistant(tool_calls) / tool 消息:tool_call_id 与请求 id 精确匹配
    for i in range(2, len(messages) - 1, 2):
        asst, tool = messages[i], messages[i + 1]
        assert asst.role == "assistant" and tool.role == "tool"
        assert len(asst.tool_calls) == 1
        assert asst.tool_calls[0].id == tool.tool_call_id

    # 读回内容应为写入后的新内容(第三个 tool 消息)
    assert messages[7].content == "# fixed"

    # 最终回答:无工具调用
    last = messages[-1]
    assert last.role == "assistant"
    assert last.content == "修复完成"
    assert last.tool_calls == []

    # ---- 请求 payload 结构 ----
    assert len(captured) == 4

    # 首个请求:system+user,带 tools / tool_choice=auto / stream_options
    req0 = captured[0]
    assert req0["model"] == "m"
    assert req0["stream"] is True
    assert req0["stream_options"] == {"include_usage": True}
    assert req0["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in req0["tools"]] == ["read_file", "write_file"]
    assert [m["role"] for m in req0["messages"]] == ["system", "user"]

    # 第 2 个请求:已携带第一个 tool 结果,且 tool_call_id 与 assistant 请求 id 一致
    req1 = captured[1]["messages"]
    assert [m["role"] for m in req1] == ["system", "user", "assistant", "tool"]
    tool_msgs = [m for m in req1 if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_read"
    assert tool_msgs[0]["content"] == "def divide():\n    pass\n"
    asst_msgs = [m for m in req1 if m["role"] == "assistant"]
    assert asst_msgs[0]["tool_calls"][0]["id"] == "call_read"
    assert asst_msgs[0]["tool_calls"][0]["type"] == "function"
    assert asst_msgs[0]["tool_calls"][0]["function"]["name"] == "read_file"

    # 最终请求:历史完整,以 user 任务开头、以之前的 tool 结果结尾
    req3 = captured[3]["messages"]
    assert req3[0]["role"] == "system"
    assert req3[1]["role"] == "user"
    assert [m["role"] for m in req3].count("tool") == 3


async def test_e2e_tool_call_id_regression_dispatch_empty_and_wrong() -> None:
    """端到端回归:tool 消息的 tool_call_id 必须用请求 id(真实 DeepSeek 曾因不匹配被拒)。

    AgentResult.tool_call_id 由 agent/tool_calls 绑定(工具层不填写)。
    """
    r1 = tool_delta(0, id_="call_r", name="read_file", args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()

    async def dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        return AgentResult(content="empty-id-result")

    messages, _ = await _run([r1, r2], dispatch)
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    # 回灌的消息必须用请求 id
    assert tool_msgs[0].tool_call_id == "call_r"
    assert tool_msgs[0].content == "empty-id-result"
