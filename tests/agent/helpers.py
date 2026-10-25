"""M1 测试共享工具:SSE 构造 helper 与 mock loop 构造器。

多个测试文件(test_loop / test_e2e_react)重复定义 `_sse_line/_tool_delta/...`,
统一收敛到这里。用法:`from tests.agent.helpers import text_delta, tool_delta, ...`
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from agent.config import LLMConfig
from agent.types import AgentResult

if TYPE_CHECKING:
    from agent.loop import AgentLoop


def sse_line(obj: object) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def text_delta(content: str) -> str:
    return sse_line({"choices": [{"delta": {"content": content}, "finish_reason": None}]})


def reasoning_delta(content: str) -> str:
    """DeepSeek 思考增量(reasoning_content 字段)。"""
    return sse_line({"choices": [{"delta": {"reasoning_content": content}, "finish_reason": None}]})


def tool_delta(
    index: int,
    *,
    id_: str | None = None,
    name: str | None = None,
    args: str | None = None,
) -> str:
    func: dict[str, object] = {}
    if name is not None:
        func["name"] = name
    if args is not None:
        func["arguments"] = args
    tc: dict[str, object] = {"index": index}
    if id_ is not None:
        tc["id"] = id_
    if func:
        tc["function"] = func
    return sse_line({"choices": [{"delta": {"tool_calls": [tc]}, "finish_reason": None}]})


def finish(reason: str | None = "stop") -> str:
    return sse_line({"choices": [{"delta": {}, "finish_reason": reason}]})


def usage_block() -> str:
    return sse_line({"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": []})


def make_mock_loop(
    requests: list[str],
    dispatch: Callable[[str, dict[str, object]], Awaitable[AgentResult]] | None = None,
    max_steps: int = 20,
) -> AgentLoop:
    """根据预置的请求-响应序列构造 AgentLoop(mock transport)。"""
    from agent.client import StreamingClient
    from agent.loop import AgentLoop

    request_iter = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    transport = httpx.MockTransport(handler)
    config = LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=max_steps)
    if dispatch is None:
        dispatch = noop_dispatch
    return AgentLoop(
        StreamingClient(config, transport=transport),
        {
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
        },
        dispatch,
        system_prompt="你是测试助手",
        max_steps=max_steps,
    )


async def noop_dispatch(name: str, args: dict[str, Any]) -> AgentResult:
    return AgentResult(content="ok")
