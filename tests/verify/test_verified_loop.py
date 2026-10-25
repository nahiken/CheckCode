"""M3 集成测试:VerifiedLoop 闭环(httpx.MockTransport 驱动 Agent + 真实 pytest)。

场景:
- 通过路径:Agent 一次写对 → 真实 pytest 通过 → report.passed=True, rounds==1。
- 修复路径:第一次写错 → pytest 失败 → 第二次写对 → report.passed=True, rounds==2。
- 限轮次:始终写错 → 达到 max_fix_rounds → report.passed=False, 剩余风险高。
- 隔离:闭环结束后主仓库工作树无修改、worktree 已清理。
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
from worktree.manager import WorktreeManager

# 正确修复的 math_utils.py(让 demo_repo 全部测试通过)
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

# 破坏版:divide 直接抛异常(所有 divide 相关测试失败)
BROKEN_CONTENT = "def divide(a, b):\n    raise Exception('boom')\n"


def _write_args(content: str) -> str:
    return json.dumps({"path": "math_utils.py", "content": content})


def _write_round(content: str, id_: str) -> str:
    """一轮「write_file 调用 + 模型结束」的 SSE 流。"""
    return tool_delta(0, id_=id_, name="write_file", args=_write_args(content)) + finish("tool_calls") + usage_block()


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
    from policy.models import Plan

    return Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])


async def test_verified_loop_pass_rounds_1(demo_repo: Path, plan_math_utils: Plan) -> None:
    """Agent 一次写对 → 验证通过 → 报告完整。"""
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    assert report.passed is True
    assert report.rounds_used == 1
    assert report.changed_files == ["math_utils.py"]
    assert "math_utils.py" in report.diff
    assert "低:所有验证命令通过" in report.remaining_risk
    assert report.branch.startswith("sess-")  # M4:branch 即 session_id
    # 渲染可读
    assert "验证记录" in report.render()


async def test_verified_loop_fix_path_rounds_2(demo_repo: Path, plan_math_utils: Plan) -> None:
    """第一次写错(验证失败)→ 回灌修复 → 第二次写对 → 通过。"""
    requests = [
        _write_round(BROKEN_CONTENT, "call_1"),
        _text_round("尝试修复"),
        _write_round(FIXED_CONTENT, "call_2"),
        _text_round("修复完成"),
    ]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    assert report.passed is True
    assert report.rounds_used == 2
    assert len(report.rounds) == 2
    # 第一轮失败、第二轮通过
    assert report.rounds[0].passed is False
    assert report.rounds[1].passed is True
    assert "低:所有验证命令通过" in report.remaining_risk


async def test_verified_loop_limited_rounds(demo_repo: Path, plan_math_utils: Plan) -> None:
    """始终写错 → 达到轮次上限停止,标记高剩余风险。"""
    requests = [
        _write_round(BROKEN_CONTENT, "call_1"),
        _text_round("尝试1"),
        _write_round(BROKEN_CONTENT, "call_2"),
        _text_round("尝试2"),
    ]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests), max_fix_rounds=2,
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    assert report.passed is False
    assert report.rounds_used == 2
    assert len(report.rounds) == 2
    assert all(not r.passed for r in report.rounds)
    assert "高:" in report.remaining_risk
    assert "第 2 轮后测试仍失败" in report.remaining_risk


async def test_verified_loop_isolation_main_repo_untouched(demo_repo: Path, plan_math_utils: Plan) -> None:
    """闭环结束后:主仓库工作树无修改、worktree 已清理。"""
    base = WorktreeManager(demo_repo).base_commit()
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    await loop.run(demo_repo, "修复 math_utils")

    # 主仓库工作树干净
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(demo_repo), "status", "--short"],
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""
    # HEAD 未动
    assert WorktreeManager(demo_repo).base_commit() == base
    # 无残留 worktree
    listing = subprocess.run(
        ["git", "-C", str(demo_repo), "worktree", "list"],
        capture_output=True,
        text=True,
    ).stdout
    assert len(listing.splitlines()) == 1  # 只有主 worktree


async def test_verified_loop_audit_persists_after_cleanup(
    demo_repo: Path, plan_math_utils: Plan
) -> None:
    """P2 修复回归:审计落盘在 worktree 之外,拆除后仍可回溯。"""
    requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    # 审计在 .git/whycode-audits/<branch>.jsonl,worktree 已拆除但审计仍在
    audit_path = demo_repo / ".git" / "whycode-audits" / f"{report.branch}.jsonl"
    assert audit_path.exists()
    records = audit_path.read_text(encoding="utf-8").splitlines()
    # write_file(permit 或 ask 后批准)产生 before+after 两段,至少 2 条
    assert len(records) >= 2
    assert any("write_file" in line for line in records)


async def test_verified_loop_default_test_command(demo_repo: Path, plan_math_utils: Plan) -> None:
    """plan 的命令是测试类命令 → 直接采用;否则默认 pytest。"""
    loop = VerifiedLoop(_config(), plan=plan_math_utils, transport=_make_transport([]))
    assert loop._resolve_test_command(plan_math_utils) == "python -m pytest tests/ -q"

    from policy.models import Plan

    plan_cmd = Plan(files=["math_utils.py"], commands=["git status"])
    assert loop._resolve_test_command(plan_cmd) == "python -m pytest tests/ -q"  # 非测试类 → 回退默认


# === M9 W6:Planner 空计划 → 只读分析 pass(不建分支/不验证/不改文件) ===

# 无 [计划] 标记的纯 NL 文本(Planner 空输出 → 只读分流)
_PLANNER_NO_PLAN = text_delta("该任务无需修改任何文件。") + finish() + usage_block()


def _readonly_tool_args(path: str = "math_utils.py") -> str:
    return json.dumps({"path": path})


async def test_verified_loop_readonly_nl_task(demo_repo: Path) -> None:
    """Planner 空输出(NL 只读任务)→ 只读分析 pass:无分支/无验证/无改动,报告带回答。

    请求序列:① Planner 无计划文本 → ② 只读 agent read_file → ③ 只读 agent 作答。
    全程模型无权写文件;报告 readonly=True、rounds=[]、changed_files=[]、passed=True。
    """
    requests = [
        _PLANNER_NO_PLAN,
        tool_delta(0, id_="call_read", name="read_file", args=_readonly_tool_args())
        + finish("tool_calls")
        + usage_block(),
        _text_round("math_utils.py 含 add/subtract/divide/fib 四个函数;divide 除零抛异常"),
    ]
    loop = VerifiedLoop(
        _config(), strict=False, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "描述 math_utils 的函数")

    # 只读分流生效
    assert report.readonly is True
    assert report.passed is True  # 只读 pass 恒真(无验收失败面)
    assert report.rounds == []
    assert report.rounds_used == 0
    assert report.changed_files == []
    assert report.diff == ""
    # 模型只读分析的回答被带出
    assert "divide" in report.readonly_reply
    # 渲染含只读小节
    assert "只读分析" in report.render()
    # M12:只读 pass 也带 run_id,且落盘(header + run phase + done)→ Trace 可摘要
    assert report.run_id.startswith("run-")
    _sess = demo_repo / ".git" / "whycode-sessions" / f"{report.branch}.jsonl"
    assert _sess.exists()
    _recs = [json.loads(ln) for ln in _sess.read_text(encoding="utf-8").splitlines() if ln]
    assert all(r["run_id"] == report.run_id for r in _recs)  # 每条都带本次 run_id
    _phases = [r.get("phase") for r in _recs if r["type"] == "phase"]
    assert "run" in _phases and "done" in _phases
    assert any(r["type"] == "header" and r["session_id"] == report.branch for r in _recs)
    # 主仓库工作树干净(无分支、无改动)
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(demo_repo), "status", "--short"],
        capture_output=True, text=True,
    ).stdout
    assert status == ""
    listing = subprocess.run(
        ["git", "-C", str(demo_repo), "worktree", "list"],
        capture_output=True, text=True,
    ).stdout
    assert len(listing.splitlines()) == 1  # 无残留 worktree


async def test_verified_loop_readonly_write_is_denied(demo_repo: Path) -> None:
    """只读 pass 里模型试图写 → strict + 空计划下写被 deny 回灌,任务仍干净完成。

    请求序列:① Planner 无计划 → ② 只读 agent write_file(被 deny,error 回灌)
    → ③ 只读 agent 收到 deny 后作答完成。断言:report readonly、改动为空、
    审计记录里 write_file 决策 = deny(granted=False)。
    """
    requests = [
        _PLANNER_NO_PLAN,
        tool_delta(0, id_="call_write", name="write_file",
                   args=json.dumps({"path": "math_utils.py", "content": "x"}))
        + finish("tool_calls")
        + usage_block(),
        _text_round("已了解,不能修改文件;divide 除零需抛 ValueError"),
    ]
    loop = VerifiedLoop(
        _config(), strict=False, approval_gate=_auto_approve,
        transport=_make_transport(requests),
    )

    report = await loop.run(demo_repo, "分析 math_utils 缺陷(不要改动)")

    assert report.readonly is True
    assert report.passed is True
    assert report.changed_files == []
    # 主仓库未被越权修改(严格+空计划 deny 生效)
    import subprocess

    status = subprocess.run(
        ["git", "-C", str(demo_repo), "status", "--short"],
        capture_output=True, text=True,
    ).stdout
    assert status == ""
    # 审计落盘:write_file 被 deny(决策 deny、未授予执行)
    audit_path = demo_repo / ".git" / "whycode-audits" / f"{report.branch}.jsonl"
    assert audit_path.exists()
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    write_recs = [
        json.loads(line) for line in lines if json.loads(line).get("tool_name") == "write_file"
    ]
    assert write_recs, "应有 write_file 审计记录"
    assert write_recs[0]["decision"] == "deny"
    assert write_recs[0]["granted"] is False


# === M9 W3:装配层 extra_tools 注入 VerifiedLoop(闭环内可被模型调用) ===

class _FakeExtraTool:
    """伪 MCP 工具(fake,不经真实 server):实现 ToolRunner 需要的鸭子协议。

    tools_spec_from 需要 description/schema_dict/read_only/path_key/parallel_safe;
    ToolRunner 执行走 async_run(async 路径,与 McpTool 同契约)。
    """

    def __init__(self, name: str, *, read_only: bool) -> None:
        self._name = name
        self._ro = read_only
        self.calls: list[dict[str, Any]] = []
        self.read_only = read_only
        self.path_key: str | None = None
        self.parallel_safe = False

    @property
    def description(self) -> str:
        return f"[fake] {self._name}"

    @property
    def schema_dict(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    @property
    def args_model(self) -> None:
        return None

    async def async_run(self, args: dict[str, Any]) -> Any:
        self.calls.append(args)
        content = f"{self._name}:ok" if self._ro else f"{self._name}:written"
        return type("R", (), {"content": content, "is_error": False})()


async def test_w3_extra_tool_injected_and_callable(demo_repo: Path, plan_math_utils: Plan) -> None:
    """extra_tools(MCP 装配)→ 注入 ToolRunner → 闭环里模型可调用并拿到结果。"""
    extra = _FakeExtraTool("mcp_lookup", read_only=True)  # 只读 → 无需审批放行
    # 回合1: 模型发起 mcp_lookup(计划外工具,但因只读而放行)
    r1 = tool_delta(0, id_="call_m", name="mcp_lookup", args='{"k":"v"}') + finish("tool_calls") + usage_block()
    r2 = _text_round("已完成")
    loop = VerifiedLoop(
        _config(), plan=plan_math_utils, approval_gate=_auto_approve,
        transport=_make_transport([r1, r2]),
        test_command="python -c pass",  # 验证命令恒过(本测试只验证 extra 工具可调用)
        extra_tools={"mcp_lookup": extra},  # type: ignore[dict-item]
    )

    report = await loop.run(demo_repo, "查一下 mcp_lookup")

    assert extra.calls == [{"k": "v"}]  # 工具确实被执行
    assert report.passed is True
    # 工具未改任何文件 → diff 空
    assert report.changed_files == []
