"""agent/loop.py M4 挂接测试:会话持久化 + 记账 + 可重入 run。

复用 tests/agent/helpers 的 SSE 构造 + MockTransport;不依赖真实 LLM。
"""

from __future__ import annotations

from typing import Any

import httpx

from agent.config import LLMConfig
from agent.loop import StepEvent, build_agent
from agent.types import AgentResult
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block


def _write_round(content: str, id_: str) -> str:
    import json

    args = json.dumps({"path": "a.py", "content": content})
    return tool_delta(0, id_=id_, name="write_file", args=args) + finish("tool_calls") + usage_block()


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _transport(requests: list[str]) -> httpx.MockTransport:
    it = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(it))

    return httpx.MockTransport(handler)


async def _dispatch(name: str, args: dict[str, Any]) -> AgentResult:
    return AgentResult(content="ok")


async def test_l1_build_agent_accepts_session_id() -> None:
    """build_agent 可注入 session_id → loop 持有(持久化挂接点)。"""
    loop = build_agent(
        _config(),
        {"read_file": {"description": "读", "parameters": {"type": "object", "properties": {}}}},
        _dispatch,
        system_prompt="sys",
        transport=_transport([_text_round()]),
        session_id="sess-1",
    )
    assert loop.session_id == "sess-1"


async def test_l2_record_event_callback_receives_usage() -> None:
    """run 产生 StepEvent → record_event 回调收到(含 usage/token 记账)。"""
    events: list[StepEvent] = []
    loop = build_agent(
        _config(),
        {"read_file": {"description": "读", "parameters": {"type": "object", "properties": {}}}},
        _dispatch,
        system_prompt="sys",
        transport=_transport([_text_round()]),
        session_id="sess-2",
        record_event=events.append,
    )
    messages = await loop.run("任务")
    assert any(e.kind == "model_call" for e in events)
    assert any(e.kind == "finish" for e in events)
    assert messages[-1].role == "assistant"
    assert loop.usage.total_messages > 0  # 记账对象存在(消息数)


async def test_l3_reentrant_run_continues_session() -> None:
    """可重入 run:先跑一轮(无工具)→ 再跑一轮(会话记录追加,不重置)。"""
    loop = build_agent(
        _config(),
        {"read_file": {"description": "读", "parameters": {"type": "object", "properties": {}}}},
        _dispatch,
        system_prompt="sys",
        transport=_transport([_text_round("一轮"), _text_round("二轮")]),
        session_id="sess-3",
    )
    m1 = await loop.run("第一次")
    m2 = await loop.run("第二次")
    assert m1[-1].content == "一轮"
    assert m2[-1].content == "二轮"
    assert loop.usage.total_messages > 0
