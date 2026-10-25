"""M3 端到端测试:完整 Verified Coding Loop(真实 git + mock LLM + 真实 pytest)。

验证:修改→测试→修复→Diff/风险报告 全链路可跑通;worktree 用后即清。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx

from agent.config import LLMConfig
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop

# 修复 divide(加 ValueError)与 fib(负数返回 -1)—— 与 demo_repo 测试期望一致
FIXED_CONTENT = '''"""供 Agent 修改的样例模块。"""

from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def fib(n: int) -> int:
    if n < 0:
        return -1
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
'''


def _write_round(id_: str) -> str:
    args = json.dumps({"path": "math_utils.py", "content": FIXED_CONTENT})
    return tool_delta(0, id_=id_, name="write_file", args=args) + finish("tool_calls") + usage_block()


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


async def test_e2e_verified_loop_full_closure(demo_repo: Path) -> None:
    """一次写对 → 验证通过 → 完整报告;worktree 清理无残留。"""
    requests = [
        _write_round("call_w"),
        text_delta("完成") + finish() + usage_block(),
    ]
    plan = Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])
    loop = VerifiedLoop(
        LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10),
        plan=plan,
        approval_gate=_auto_approve,
        transport=httpx.MockTransport(_handler(requests)),
    )

    report = await loop.run(demo_repo, "修复 math_utils 的 divide 和 fib")

    # 报告完整
    assert report.passed is True
    assert report.rounds_used == 1
    assert report.changed_files == ["math_utils.py"]
    assert "ValueError" in report.diff
    assert "低:所有验证命令通过" in report.remaining_risk
    rendered = report.render()
    assert "风险报告" in rendered and "Diff" in rendered
    # demo_repo 主仓库工作树未被修改(隔离验证)
    status = subprocess.run(
        ["git", "-C", str(demo_repo), "status", "--short"],
        capture_output=True, text=True,
    ).stdout
    assert status == ""
    # 无残留 worktree
    listing = subprocess.run(
        ["git", "-C", str(demo_repo), "worktree", "list"],
        capture_output=True, text=True,
    ).stdout
    assert len(listing.splitlines()) == 1


def _handler(requests: list[str]) -> Any:
    it = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(it))

    return handler
