"""M7 集成测试:VerifiedLoop.run(events=...) 实时事件透传。

M4 已预埋 AgentLoop.run(events=...) 实时回调;M7 让 VerifiedLoop.run 也接收
events 并转发:AgentLoop 事件(thinking/tool_exec_start/tool_result/finish)+
每轮验证结果(verify)+ 恢复标记(resume)。供 FastAPI SSE 消费。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _write_args(content: str) -> str:
    import json

    return json.dumps({"path": "math_utils.py", "content": content})


FIXED = """def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def fib(n):
    if n < 0:
        return -1
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
"""


def _make_transport() -> httpx.MockTransport:
    """单个 round:写正确 math_utils → 结束。验证将通过。"""
    write = tool_delta(0, id_="t1", name="write_file", args=_write_args(FIXED)) + finish(
        "tool_calls"
    ) + usage_block()
    text = text_delta("完成") + finish() + usage_block()

    calls = iter([write, text])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(calls))

    return httpx.MockTransport(handler)


@pytest.fixture()
def plan_math_utils() -> Plan:
    return Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


class TestLoopEvents:
    async def test_events_received_all_kinds(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """events 回调收到 AgentLoop 事件 + verify 事件。"""
        got: list[str] = []
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            approval_gate=_auto_approve,
            transport=_make_transport(),
        )

        async def collect(ev: Any) -> None:
            got.append(ev.kind)

        report = await loop.run(demo_repo, "修复 math_utils.py", events=collect)

        # AgentLoop 事件齐全(至少含写工具与文本完成)
        assert "tool_exec_start" in got
        assert "tool_result" in got
        assert "finish" in got
        # verify 事件:至少一轮
        assert "verify" in got
        # 报告通过
        assert report.passed is True

    async def test_verify_event_carries_round_result(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """verify 事件带轮次号与 TestResult 数据(前端渲染验证记录)。"""
        verifies: list[dict[str, Any]] = []
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            approval_gate=_auto_approve,
            transport=_make_transport(),
        )

        async def collect(ev: Any) -> None:
            if ev.kind == "verify":
                verifies.append(ev.data)

        await loop.run(demo_repo, "修复 math_utils.py", events=collect)

        assert len(verifies) >= 1
        first = verifies[0]
        assert first["round"] >= 1
        assert first["passed"] is True
        assert "result" in first  # TestResult 序列化
        assert first["result"]["command"]  # 命令非空

    async def test_no_events_callback_is_noop(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """不传 events → 行为不变(向后兼容,既有调用零破坏)。"""
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            approval_gate=_auto_approve,
            transport=_make_transport(),
        )
        report = await loop.run(demo_repo, "修复 math_utils.py")
        assert report.passed is True

    async def test_tool_events_carry_name_and_content(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """tool_exec_start 带工具名列表,tool_result 带内容(前端渲染协议)。"""
        got: list[dict[str, Any]] = []
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            approval_gate=_auto_approve,
            transport=_make_transport(),
        )

        async def collect(ev: Any) -> None:
            got.append({"kind": ev.kind, "data": ev.data})

        await loop.run(demo_repo, "修复 math_utils.py", events=collect)

        starts = [g for g in got if g["kind"] == "tool_exec_start"]
        results = [g for g in got if g["kind"] == "tool_result"]
        assert starts, "应有 tool_exec_start 事件"
        assert "write_file" in starts[0]["data"]["tools"]
        assert results, "应有 tool_result 事件"
        assert "content" in results[0]["data"]
        assert "is_error" in results[0]["data"]

    async def test_fix_round_events_forwarded(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """修复轮路径也透传 events:第一次写错 → 修复任务事件 → 第二次写对。

        F9:仅正常路径测 events 不够——修复轮(loop.run(fix_task))同样要透传。
        """
        import json as _json

        from tests.agent.helpers import finish, text_delta, tool_delta, usage_block

        def _write(content: str, id_: str) -> str:
            args = _json.dumps({"path": "math_utils.py", "content": content})
            return tool_delta(0, id_=id_, name="write_file", args=args) + finish("tool_calls") + usage_block()

        broken = "def divide(a, b):\n    raise Exception('boom')\n"
        requests = [
            _write(broken, "call_1"),
            text_delta("尝试") + finish() + usage_block(),
            _write(FIXED, "call_2"),
            text_delta("修复完成") + finish() + usage_block(),
        ]

        def _transport() -> httpx.MockTransport:
            calls = iter(requests)

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, text=next(calls))

            return httpx.MockTransport(handler)

        kinds: list[str] = []
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            approval_gate=_auto_approve,
            transport=_transport(),
        )

        async def collect(ev: Any) -> None:
            kinds.append(ev.kind)

        report = await loop.run(demo_repo, "修复 math_utils", events=collect)

        assert report.passed is True
        assert report.rounds_used == 2  # 两次验证(失败→通过)
        assert "verify" in kinds
        # 两次工具执行都透传(第一轮 + 修复轮)
        assert kinds.count("tool_exec_start") == 2
