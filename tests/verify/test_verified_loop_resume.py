"""M4 集成测试:VerifiedLoop 会话持久化 + 恢复闭环 + 不重放副作用。

复用 M3 的 mock transport 模式(httpx.MockTransport 驱动 Agent + 真实 pytest
在 worktree 内验证)。核心验收:
- 正常 run 后 store 落盘(header/消息/tool/phase)。
- 已有相同 (session_id, branch) 的 store → resume=True,复用既有 worktree。
- **不重放副作用**:上一 session 已有已完成写,resume 后模型不重复执行写。
- 超长工具输出落盘裁剪。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop

# 正确修复的 math_utils.py
FIXED_CONTENT = """def add(a, b):
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

# 破坏版:divide 直接抛异常
BROKEN_CONTENT = "def divide(a, b):\n    raise Exception('boom')\n"


def _write_args(content: str) -> str:
    return json.dumps({"path": "math_utils.py", "content": content})


def _write_round(content: str, id_: str) -> str:
    return tool_delta(0, id_=id_, name="write_file", args=_write_args(content)) + finish("tool_calls") + usage_block()


def _read_round(path: str, id_: str) -> str:
    return tool_delta(0, id_=id_, name="read_file", args=json.dumps({"path": path})) + finish("tool_calls") + usage_block()


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


def _make_transport(requests: list[str]) -> httpx.MockTransport:
    request_iter = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    return httpx.MockTransport(handler)


@pytest.fixture()
def plan_math_utils() -> Plan:
    return Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])


def _session_dir(repo: Path) -> Path:
    return repo / ".git" / "whycode-sessions"


async def test_v1_store_landed_after_run(demo_repo: Path, plan_math_utils: Plan) -> None:
    """正常 run 后 store 落盘:含 header/消息/tool/phase 记录。"""
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    session_file = _session_dir(demo_repo) / f"{report.branch}.jsonl"
    assert session_file.exists()
    text = session_file.read_text(encoding="utf-8")
    assert '"type":"header"' in text
    assert '"type":"message"' in text
    assert '"type":"tool_call"' in text
    assert '"type":"phase"' in text
    # P1 修复回归:tool_call 落盘含审批边界字段——真实 ToolRunner dispatch 下
    # write_file 计划内放行,audit 随 AgentResult 透传,记为 granted=true + decision=permit
    assert '"tool_name":"write_file"' in text
    assert '"granted":true' in text
    assert '"decision":"permit"' in text
    # P3 修复回归:tool 结果消息已落盘(role=tool),store 能识别「已完成回灌」
    assert '"role":"tool"' in text


async def test_v2_resume_detected_reuses_branch(demo_repo: Path, plan_math_utils: Plan) -> None:
    """已有相同 (session_id, branch) 的 store → resume=True,复用既有 branch。"""
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    # 第二次同 session:检测到既有 store → resume
    resumed = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport([_text_round()]),
        session_id=report.branch,
    )
    report2 = await resumed.run(demo_repo, "修复 math_utils(继续)")
    assert report2.branch == report.branch  # 复用既有 branch
    assert resumed.last_resumed is True
    # worktree 复用而非新建:主仓 worktree list 只有主 worktree(前次已清理)
    import subprocess

    listing = subprocess.run(
        ["git", "-C", str(demo_repo), "worktree", "list"],
        capture_output=True,
        text=True,
    ).stdout
    assert len(listing.splitlines()) == 1


async def test_v3_no_replay_of_completed_write(demo_repo: Path, plan_math_utils: Plan) -> None:
    """核心:上一 session 已完成写(已提交到分支);resume 复用分支,不重放写。

    第一段:Agent 读→写(修复)→文本;验证通过,写已提交到分支。
    第二段 resume:复用既有分支 → 直接重验证通过即完成,**模型不被再次调用**。
    """
    first_segment = [
        _read_round("math_utils.py", "call_r1"),
        _write_round(FIXED_CONTENT, "call_w1"),
        _text_round("已修复"),
    ]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(first_segment),
    )

    report = await loop.run(demo_repo, "修复 math_utils")
    assert report.branch.startswith("sess-")
    assert report.passed is True

    # 第二段 resume:记录 handler 是否被调用(若被调用说明模型被再次请求 → 重放)
    handler_calls: list[str] = []

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        handler_calls.append("model_call")
        return httpx.Response(200, text=_text_round("继续完成"))

    resumed = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=httpx.MockTransport(_recording_handler),
        session_id=report.branch,
    )
    report2 = await resumed.run(demo_repo, "修复 math_utils(继续)")

    assert report2.branch == report.branch  # 复用既有分支
    assert resumed.last_resumed is True
    # 写已在上次完成且验证通过 → resume 无需模型 → 无重放(handler 未被调用)
    assert handler_calls == []
    assert report2.passed is True


async def test_v4_load_store_visible(demo_repo: Path, plan_math_utils: Plan) -> None:
    """resume 后 _load_store 可见会话消息(复盘/调试可读取)。"""
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )
    report = await loop.run(demo_repo, "修复 math_utils")

    resumed = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport([_text_round()]),
        session_id=report.branch,
    )
    await resumed.run(demo_repo, "继续")

    assert resumed.store is not None
    records = resumed.store.records()
    assert any(r.type == "header" for r in records)
    assert any(r.type == "message" for r in records)
    assert any(r.type == "tool_call" for r in records)


async def test_v5_truncated_tool_output_landed(demo_repo: Path, plan_math_utils: Plan) -> None:
    """超长工具输出落盘裁剪:store 内记录内容 ≤ 上限 + 截断标记。"""
    big = "x" * 20000

    def _huge_read_round(path: str, id_: str) -> str:
        # read_file 返回超长内容 → dispatch 结果超长 → 落盘裁剪
        return tool_delta(0, id_=id_, name="read_file", args=json.dumps({"path": path})) + finish("tool_calls") + usage_block()

    async def _big_dispatch(name: str, args: dict[str, Any]) -> Any:
        from agent.types import AgentResult

        if name == "read_file":
            return AgentResult(content=big, is_error=False)
        if name == "write_file":
            # 真实写入 worktree(通过 loop._wt.path 闭包引用),让验证通过
            assert loop._wt is not None
            (loop._wt.path / args["path"]).write_text(args["content"], encoding="utf-8")
            return AgentResult(content="ok")
        return AgentResult(content="ok")

    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(
            [
                _huge_read_round("math_utils.py", "call_r"),
                _write_round(FIXED_CONTENT, "call_w"),
                _text_round("修复完成"),
            ]
        ),
        dispatch=_big_dispatch,
    )
    report = await loop.run(demo_repo, "修复 math_utils")

    session_file = _session_dir(demo_repo) / f"{report.branch}.jsonl"
    text = session_file.read_text(encoding="utf-8")
    # 落盘内容被裁剪(不超过默认上限 8000)
    assert '"truncated":true' in text
