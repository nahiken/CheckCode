"""M10 集成测试:计划审批 HTTP 端点(api/app.py 层)。

与 tests/api/test_approvals_api.py 同构,但针对**计划**审批:
- strict + auto_approve=False → VerifiedLoop 调 plan 门 → awaiting_plan_approval +
  推非终止 `plan_approval` SSE 事件 → await Future。
- POST /api/tasks/{id}/plan-approvals/{aid} resolve(approve/deny)。
- deny → 闭环提前返回(plan_rejected)。

CI 全离线:monkeypatch api.app.VerifiedLoop 为假类,monkeypatch get_llm_config 给假 key。
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.config import LLMConfig
from api.app import _TaskRequest, app, manager
from verify.report import RiskReport

api_app = importlib.import_module("api.app")

_ID = [0]


def _tid(prefix: str) -> str:
    _ID[0] += 1
    return f"{prefix}-{_ID[0]}"


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _request(
    demo_repo: Path, *, strict: bool = True, auto_approve: bool = False
) -> _TaskRequest:
    return _TaskRequest(
        task="修复 math_utils.py", repo=str(demo_repo), strict=strict, auto_approve=auto_approve
    )


def _report(demo_repo: Path, *, rejected: bool) -> RiskReport:
    from policy.models import Plan
    from verify.runner import TestResult

    if rejected:
        return RiskReport(
            repo=demo_repo,
            base_commit="abc",
            branch="sess-fake",
            changed_files=[],
            test_command="python -m pytest tests/ -q",
            rounds=[],
            rounds_used=0,
            remaining_risk="计划被用户拒绝,未做任何修改",
            diff="",
            plan_rejected=True,
            plan=Plan(files=["math_utils.py"]),
        )
    return RiskReport(
        repo=demo_repo,
        base_commit="abc",
        branch="sess-fake",
        changed_files=["math_utils.py"],
        test_command="python -m pytest tests/ -q",
        rounds=[
            TestResult(command="python -m pytest tests/ -q", passed=True, exit_code=0,
                       output="ok", duration_ms=10)
        ],
        rounds_used=1,
        remaining_risk="低:所有验证命令通过",
        diff="+1",
    )


def _plan_loop_factory(demo_repo: Path, out: dict[str, Any]) -> type:
    """假 VerifiedLoop:调一次 plan 门(结果写 out['approved'])。"""
    from policy.models import Plan

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            self._plan_gate = kw.get("plan_approval_gate")
            self._require = kw.get("require_plan_approval", False)

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            plan = Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])
            # 计划门(掌握 approval_id 的一侧)发 plan_approval 事件;VerifiedLoop 不再发
            # 重复事件(此前 loop 先发一个无 approval_id 的,前端会拿到空 id)。
            approved = await self._plan_gate(plan) if self._plan_gate else True
            out["approved"] = approved
            return _report(demo_repo, rejected=not approved)

    return _FakeLoop


async def _wait_status(task_id: str, status: str, timeout: float = 3.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        h = manager.get(task_id)
        if h is not None and h.status == status:
            return h
        await asyncio.sleep(0.01)
    h = manager.get(task_id)
    raise AssertionError(
        f"任务 {task_id} 未在 {timeout}s 内进入 {status},当前 {getattr(h, 'status', None)}"
    )


class TestPlanApprovalFlow:
    async def test_approve_plan_runs(self, monkeypatch: pytest.MonkeyPatch, demo_repo: Path) -> None:
        """批准计划 → 闭环进入写阶段 → done,报告非 plan_rejected。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, out))
        tid = _tid("pl-ok")
        manager.create(tid)
        runner = asyncio.create_task(api_app._run_task(tid, _request(demo_repo), _config()))
        await _wait_status(tid, "awaiting_plan_approval")
        handle = manager.get(tid)
        assert handle is not None and handle.pending
        aid = next(iter(handle.pending))
        assert handle.resolve_pending(aid, True)
        await asyncio.wait_for(runner, timeout=3.0)

        assert out["approved"] is True
        h = manager.get(tid)
        assert h is not None and h.status == "done"
        assert h.report is not None and h.report.get("plan_rejected") is False
        # plan_approval 事件已进历史,且**只此一条、带 approval_id**(无重复空 id 事件)
        plans = [e for e in h.history if e["event"] == "plan_approval"]
        assert len(plans) == 1
        assert plans[0]["data"]["approval_id"] == aid

    async def test_deny_plan_returns_early(
        self, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        """拒绝计划 → 闭环返回 plan_rejected 报告,零改动。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, out))
        tid = _tid("pl-deny")
        manager.create(tid)
        runner = asyncio.create_task(api_app._run_task(tid, _request(demo_repo), _config()))
        await _wait_status(tid, "awaiting_plan_approval")
        handle = manager.get(tid)
        assert handle is not None
        aid = next(iter(handle.pending))
        assert handle.resolve_pending(aid, False)
        await asyncio.wait_for(runner, timeout=3.0)

        assert out["approved"] is False
        h = manager.get(tid)
        assert h is not None and h.status == "done"
        assert h.report is not None and h.report.get("plan_rejected") is True
        assert h.report.get("changed_files") == []


class TestPlanApprovalEndpoint:
    @pytest.fixture()
    def client(self) -> Iterator[TestClient]:
        with TestClient(app) as c:
            yield c

    def _spawn_awaiting_plan(self, client: TestClient, demo_repo: Path) -> tuple[str, str]:
        body = client.post(
            "/api/tasks",
            json={"task": "修复 math_utils.py", "repo": str(demo_repo),
                  "strict": True, "auto_approve": False},
        ).json()
        tid = body["task_id"]
        # 等待上限:轮询条件(后台任务调度)依赖机器负载,2s 预算在全量跑时会偶发超时;
        # 这是失败守卫而非行为断言 —— 条件满足即 break,放宽零成本。
        for _ in range(500):
            h = manager.get(tid)
            if h is not None and h.pending:
                return tid, next(iter(h.pending))
            time.sleep(0.02)
        raise AssertionError(f"任务 {tid} 未进入 awaiting_plan_approval")

    def test_plan_approve_endpoint_resolves(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, out))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting_plan(client, demo_repo)

        resp = client.post(f"/api/tasks/{tid}/plan-approvals/{aid}", json={"approve": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"
        for _ in range(500):
            h = manager.get(tid)
            if h is not None and h.status == "done":
                break
            time.sleep(0.02)
        assert manager.get(tid).status == "done"  # type: ignore[union-attr]
        assert out["approved"] is True

    def test_plan_deny_endpoint_resolves(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, out))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting_plan(client, demo_repo)

        resp = client.post(f"/api/tasks/{tid}/plan-approvals/{aid}", json={"approve": False})
        assert resp.status_code == 200
        assert resp.json()["approved"] is False
        for _ in range(500):
            h = manager.get(tid)
            if h is not None and h.status == "done":
                break
            time.sleep(0.02)
        assert out["approved"] is False

    def test_unknown_plan_approval_404(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, {}))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, _aid = self._spawn_awaiting_plan(client, demo_repo)
        resp = client.post(f"/api/tasks/{tid}/plan-approvals/pl-wrong", json={"approve": True})
        assert resp.status_code == 404

    def test_unknown_task_404(self, client: TestClient) -> None:
        resp = client.post("/api/tasks/pl-none/plan-approvals/pl-1", json={"approve": True})
        assert resp.status_code == 404

    def test_resume_rejected_while_awaiting_plan(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        """等待计划审批时不可 resume(防双跑)。"""
        monkeypatch.setattr(api_app, "VerifiedLoop", _plan_loop_factory(demo_repo, {}))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, _aid = self._spawn_awaiting_plan(client, demo_repo)
        resp = client.post(f"/api/tasks/{tid}/resume")
        assert resp.json().get("error")
