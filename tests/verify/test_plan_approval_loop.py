"""M10 集成测试:计划审批门(VerifiedLoop 层)。

语义:
- require_plan_approval=True + 注入计划 → 闭环在**任何 worktree/写入之前**调 plan 门。
- 拒绝 → 提前返回 RiskReport(plan_rejected=True):零 worktree、零分支、零 session 文件
  (连 `.git/whycode-sessions/` 目录都不建)、零命令执行、主仓库工作树干净。
- 批准 → 正常进入闭环,并把 PhaseRecord(phase="plan_approved")落盘(**含完整计划**)。
- resume:既有会话若已记 plan_approved → **复用落盘计划**(不重跑 Planner)、不再询问
  (spy gate 断言调用次数不增)。

测试方式:注入 plan(不触 Planner 真实 LLM);Agent 用 MockTransport 驱动。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop
from verify.plan_gate import auto_plan_gate, reject_plan_gate

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


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _write_round(content: str, id_: str) -> str:
    args = json.dumps({"path": "math_utils.py", "content": content})
    return tool_delta(0, id_=id_, name="write_file", args=args) + finish("tool_calls") + usage_block()


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


def _make_transport(requests: list[str]) -> httpx.MockTransport:
    it = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(it))

    return httpx.MockTransport(handler)


@pytest.fixture()
def plan_math_utils() -> Plan:
    return Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--short"], capture_output=True, text=True
    ).stdout


class TestPlanRejected:
    """计划被拒 → 零 worktree/零写入,安全提前返回。"""

    async def test_reject_returns_early_no_side_effects(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """拒绝 → plan_rejected 报告;主仓库干净、无 worktree、无 session 文件、无命令执行。"""
        base = subprocess.run(
            ["git", "-C", str(demo_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        sessions_dir = demo_repo / ".git" / "whycode-sessions"
        # Agent 若被调用,请求序列为空会抛 StopIteration → 用空 transport 证明「未进入写阶段」
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport([]),
            require_plan_approval=True,
            plan_approval_gate=reject_plan_gate(),
        )

        report = await loop.run(demo_repo, "修复 math_utils")

        assert report.plan_rejected is True
        assert report.passed is True  # 拒绝不是失败
        assert report.changed_files == []
        assert report.diff == ""
        assert report.rounds == []
        assert "计划被用户拒绝" in report.remaining_risk
        # 主仓库工作树干净、HEAD 未动
        assert _git_status(demo_repo) == ""
        assert subprocess.run(
            ["git", "-C", str(demo_repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() == base
        # 无残留 worktree(只有主 worktree)
        listing = subprocess.run(
            ["git", "-C", str(demo_repo), "worktree", "list"], capture_output=True, text=True
        ).stdout
        assert len(listing.splitlines()) == 1
        # 分支未创建(未被 commit_all)
        branches = subprocess.run(
            ["git", "-C", str(demo_repo), "branch"], capture_output=True, text=True
        ).stdout
        assert report.branch not in branches.split()
        # 零 session 文件 **且零 session 目录**(SessionStore 目录延迟创建;此前声称未断言)
        assert not sessions_dir.exists(), f"拒绝路径不应产生 session 目录: {sessions_dir}"
        assert not (sessions_dir / f"{report.branch}.jsonl").exists()
        # 渲染含「计划被拒绝」小节
        assert "计划被拒绝" in report.render()


class TestPlanApproved:
    """计划批准 → 正常闭环;批准 phase 落盘。"""

    async def test_approve_runs_loop_and_records_phase(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport(requests),
            require_plan_approval=True,
            plan_approval_gate=auto_plan_gate(),
        )

        report = await loop.run(demo_repo, "修复 math_utils")

        assert report.plan_rejected is False
        assert report.passed is True
        assert report.changed_files == ["math_utils.py"]
        # 批准 phase 落盘(含**完整计划**,供 resume 复用;不是只存 files)
        session_path = demo_repo / ".git" / "whycode-sessions" / f"{report.branch}.jsonl"
        text = session_path.read_text(encoding="utf-8")
        assert "plan_approved" in text
        assert "python -m pytest tests/ -q" in text  # commands 也落盘了

    async def test_gate_receives_plan(self, demo_repo: Path, plan_math_utils: Plan) -> None:
        """门接收到的计划 = 注入的计划(文件/命令透传)。"""
        seen: list[Plan] = []

        async def spy(plan: Plan) -> bool:
            seen.append(plan)
            return True

        requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport(requests),
            require_plan_approval=True,
            plan_approval_gate=spy,
        )
        await loop.run(demo_repo, "修复 math_utils")

        assert len(seen) == 1
        assert seen[0].files == ["math_utils.py"]
        assert seen[0].commands == ["python -m pytest tests/ -q"]


class TestPlanApprovalResume:
    """resume:既有会话已批准过 → 复用落盘计划 + 不重复询问。"""

    async def test_resume_skips_gate_when_already_approved(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        session_id = "sess-plan-resume"
        calls: list[Plan] = []

        async def counting_gate(plan: Plan) -> bool:
            calls.append(plan)
            return True

        # 第一次 run:批准门被调用一次
        requests = [_write_round(FIXED_CONTENT, "call_w1"), _text_round()]
        loop1 = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport(requests),
            session_id=session_id,
            require_plan_approval=True,
            plan_approval_gate=counting_gate,
        )
        await loop1.run(demo_repo, "修复 math_utils")
        assert len(calls) == 1
        assert loop1.store is not None
        # 第一次已写 plan_approved phase
        assert any(
            getattr(r, "phase", "") == "plan_approved" for r in loop1.store.records()
        )

        # 第二次 run(resume):既有 plan_approved → 门不再调用
        # resume 复用分支已通过验证 → 提前返回,无需模型请求
        loop2 = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport([]),
            session_id=session_id,
            require_plan_approval=True,
            plan_approval_gate=counting_gate,
        )
        await loop2.run(demo_repo, "修复 math_utils")
        assert len(calls) == 1  # 未新增调用

    async def test_resume_reuses_approved_plan_not_regenerates(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        """resume **复用落盘计划**:不重跑 Planner,执行的是同一份已批准计划。

        构造:① 开 strict + 批准门跑一次,批准计划落盘;② 再次 resume 但**不注入计划**
        (模拟 API resume:api/app.py 的 _run_task 不传 plan),且给一份「不可能被消费」的
        空 transport —— 若实现回去调 build_plan,空序列会 StopIteration;若复用落盘计划,
        则计划一致、报告 plan 与首次相同。
        """
        session_id = "sess-plan-reuse"

        async def approve_gate(plan: Plan) -> bool:
            return True

        requests = [_write_round(FIXED_CONTENT, "call_w1"), _text_round()]
        loop1 = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport(requests),
            session_id=session_id,
            require_plan_approval=True,
            plan_approval_gate=approve_gate,
        )
        first = await loop1.run(demo_repo, "修复 math_utils")
        assert first.plan is not None and first.plan.commands == ["python -m pytest tests/ -q"]

        # resume:不注入 plan + 空 transport(证明没有重新问 Planner/模型)
        loop2 = VerifiedLoop(
            _config(),
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport([]),
            session_id=session_id,
            require_plan_approval=True,
            plan_approval_gate=approve_gate,
        )
        second = await loop2.run(demo_repo, "修复 math_utils")

        # 复用落盘计划(不是新生成的):files/commands 与首次一致
        assert second.plan is not None
        assert second.plan.files == first.plan.files
        assert second.plan.commands == first.plan.commands
        assert second.test_command == "python -m pytest tests/ -q"


class TestPlanApprovalDisabledByDefault:
    """require_plan_approval 默认 False → 完全无变化(向后兼容)。"""

    async def test_gate_not_called_when_not_required(
        self, demo_repo: Path, plan_math_utils: Plan
    ) -> None:
        calls: list[Plan] = []

        async def spy(plan: Plan) -> bool:
            calls.append(plan)
            return False  # 若被调用会拒绝

        requests = [_write_round(FIXED_CONTENT, "call_w"), _text_round()]
        loop = VerifiedLoop(
            _config(),
            plan=plan_math_utils,
            strict=True,
            approval_gate=_auto_approve,
            transport=_make_transport(requests),
            plan_approval_gate=spy,
            require_plan_approval=False,  # 默认;门不生效
        )
        report = await loop.run(demo_repo, "修复 math_utils")

        assert calls == []
        assert report.plan_rejected is False
        assert report.passed is True
