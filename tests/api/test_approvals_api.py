"""M9 W1 集成测试:浏览器交互审批 + resume 恢复编排(api/app.py 层)。

审批核心语义(不给 loop 加 park 通道;Web 专属 approval_gate 用 Future 挂起):
- auto_approve=False + strict → policy decision="ask" 时 gate 在 TaskHandle 登记
  approval_id + Future、推非终止 approval 事件、状态转 awaiting_approval、await Future。
- 浏览器经审批端点 resolve(approve=True 放行 / deny=False 拒绝回灌)。
- SSE 流在 approval 事件间保持打开(approval 非终止),report 才结束流。

测试方式(CI 全离线,不触真实 LLM / 不依赖真实仓库测试):
- monkeypatch api.app.VerifiedLoop 为假类(触发 gate 或直接返回报告),monkeypatch
  get_llm_config 给假 key,不触网。repo 传 demo_repo fixture(已是 git 仓库,免拷贝)。
- 审批流在**同一事件循环**内完成:async 测试用 resolver task 并发 resolve;端点测试
  经 TestClient(portal 单循环)+ 轮询等待 awaiting 再 approve/deny——与生产同循环契约。

M4 已覆盖「真实 VerifiedLoop resume 不重放副作用」,本文件只验 Web 编排层。
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.config import LLMConfig
from api.app import _TaskRequest, app, manager
from policy.models import Plan
from verify.report import RiskReport

# 注意:不能 `import api.app as api_app`——api/__init__.py 的 `from api.app import app`
# 把父包同名属性 app 设成 FastAPI 实例,遮蔽子模块(import-as 经 getattr 绑定)。
# importlib.import_module 直接查 sys.modules 返回真模块,供 monkeypatch 定位。
api_app = importlib.import_module("api.app")

# 每测试唯一 id 前缀,避免模块级 manager 跨测试污染
_ID = [0]


def _tid(prefix: str) -> str:
    _ID[0] += 1
    return f"{prefix}-{_ID[0]}"


def _config() -> LLMConfig:
    """假配置:api_key 有值(不触网,VerifiedLoop 已被 monkeypatch)。"""
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _request(demo_repo: Path, *, strict: bool = True, auto_approve: bool = False) -> _TaskRequest:
    """严格 + 手动审批的创建请求(浏览器交互审批路径)。"""
    return _TaskRequest(
        task="修复 math_utils.py", repo=str(demo_repo), strict=strict, auto_approve=auto_approve
    )


def _ask_decision() -> Any:
    """policy decision="ask" 的鸭子对象(gate 只读 reason/decision)。"""
    return SimpleNamespace(decision="ask", layer="rule", reason="越权写文件需人工确认")


def _report(demo_repo: Path, *, changed: bool) -> RiskReport:
    """通过报告(passed=True):给一轮通过的验证(rounds 末轮 passed 驱动 property)。"""
    from verify.runner import TestResult

    return RiskReport(
        repo=demo_repo,
        base_commit="abc",
        branch="sess-fake",
        changed_files=["math_utils.py"] if changed else [],
        test_command="python -m pytest tests/ -q",
        rounds=[
            TestResult(command="python -m pytest tests/ -q", passed=True, exit_code=0,
                       output="ok", duration_ms=10)
        ],
        rounds_used=1,
        remaining_risk="低:所有验证命令通过",
        diff="+1" if changed else "",
    )


def _gate_loop_factory(demo_repo: Path, out: dict[str, Any]) -> type:
    """构造一个触发 gate 一次的假 VerifiedLoop(结果写入 out['approved'])。"""

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            self._gate = kw["approval_gate"]

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            out["approved"] = await self._gate(
                "write_file", {"path": "math_utils.py"}, _ask_decision()
            )
            return _report(demo_repo, changed=out["approved"])

    return _FakeLoop


async def _wait_status(task_id: str, status: str, timeout: float = 3.0) -> Any:
    """轮询 manager 直到任务状态为指定值(同 loop 内让出给后台协程/resolve)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        h = manager.get(task_id)
        if h is not None and h.status == status:
            return h
        await asyncio.sleep(0.01)
    h = manager.get(task_id)
    raise AssertionError(
        f"任务 {task_id} 未在 {timeout}s 内进入 {status},当前 status={getattr(h, 'status', None)}"
    )


async def _resolve_and_finish(
    runner: asyncio.Task[None], task_id: str, approved: bool, timeout: float = 3.0
) -> None:
    """等任务进入 awaiting → resolve 该审批 → 等后台跑完(单循环并发契约)。"""
    await _wait_status(task_id, "awaiting_approval")
    handle = manager.get(task_id)
    assert handle is not None and handle.pending
    aid = next(iter(handle.pending))
    assert handle.resolve_pending(aid, approved)
    await asyncio.wait_for(runner, timeout=timeout)


class TestInteractiveApproval:
    """审批核心流(gate 登记 → awaiting → resolve 放行/拒绝),单事件循环内。"""

    async def test_approve_resolves_and_runs(self, monkeypatch: pytest.MonkeyPatch,
                                             demo_repo: Path) -> None:
        """approve(True)→ gate 返回 True(工具放行)→ 任务继续到 done。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, out))
        tid = _tid("ap-ok")
        manager.create(tid)
        runner = asyncio.create_task(api_app._run_task(tid, _request(demo_repo), _config()))
        await _resolve_and_finish(runner, tid, approved=True)

        assert out["approved"] is True
        handle = manager.get(tid)
        assert handle is not None
        assert handle.status == "done"
        assert handle.report is not None and handle.report.get("passed") is True
        # approval 事件已进历史(SSE 前端面板据此渲染)
        assert any(e["event"] == "approval" for e in handle.history)
        # awaiting 后自动回 running 再 done(无残留 pending)
        assert handle.pending == {}

    async def test_deny_rejects_tool(self, monkeypatch: pytest.MonkeyPatch, demo_repo: Path) -> None:
        """deny(False)→ gate 返回 False(工具被拒,不执行)→ 报告正常产生。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, out))
        tid = _tid("ap-deny")
        manager.create(tid)
        runner = asyncio.create_task(api_app._run_task(tid, _request(demo_repo), _config()))
        await _resolve_and_finish(runner, tid, approved=False)

        assert out["approved"] is False  # deny → 不执行
        handle = manager.get(tid)
        assert handle is not None
        assert handle.status == "done"
        # 未被越权执行:报告 changed_files 为空
        assert handle.report is not None and handle.report.get("changed_files") == []
        assert handle.pending == {}

    async def test_auto_approve_skips_interaction(self, monkeypatch: pytest.MonkeyPatch,
                                                  demo_repo: Path) -> None:
        """auto_approve=True → gate 恒放行,无 approval 事件、无 awaiting。"""
        fired: list[bool] = []

        class _FakeLoop:
            def __init__(self, config: Any, **kw: Any) -> None:
                self._gate = kw["approval_gate"]

            async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
                fired.append(await self._gate("write_file", {"path": "x"}, _ask_decision()))
                return _report(demo_repo, changed=True)

        monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)
        tid = _tid("ap-auto")
        manager.create(tid)
        await api_app._run_task(tid, _request(demo_repo, auto_approve=True), _config())

        assert fired == [True]
        handle = manager.get(tid)
        assert handle is not None
        assert handle.status == "done"
        assert not any(e["event"] == "approval" for e in handle.history)

    async def test_resolve_unknown_approval_returns_false(self, demo_repo: Path) -> None:
        """resolve 未知 approval_id → False(端点据此回 404),不悬挂。"""
        handle = manager.create(_tid("ap-miss"))
        handle.request = {"task": "x", "repo": str(demo_repo), "strict": True,
                          "auto_approve": False}
        fut = handle.register_pending("ap-1", tool_name="write_file", reason="越权")
        assert handle.resolve_pending("ap-nope", True) is False
        assert not fut.done()  # 真实审批未受影响
        assert handle.resolve_pending("ap-1", True) is True
        assert fut.done() and fut.result() is True


class TestApprovalEndpoint:
    """审批 HTTP 端点(经真实 create → 轮询 awaiting → resolve),portal 单循环。"""

    @pytest.fixture()
    def client(self) -> Iterator[TestClient]:
        with TestClient(app) as c:
            yield c

    def _spawn_awaiting(self, client: TestClient, demo_repo: Path) -> tuple[str, str]:
        """POST create(strict+手动)→ 轮询到 awaiting_approval → 返回 (task_id, approval_id)。

        后台 create_task 在 portal 循环调度;每次请求返回让出、sleep 放后台推进。
        """
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
        raise AssertionError(f"任务 {tid} 未进入 awaiting_approval")

    def test_approve_endpoint_resolves(self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                       demo_repo: Path) -> None:
        """浏览器点「允许」:POST approvals/{aid} approve=True → 200 + 后台完成。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, out))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting(client, demo_repo)

        resp = client.post(f"/api/tasks/{tid}/approvals/{aid}", json={"approve": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "resolved" and body["approved"] is True
        # 后台跑完(portal 循环在后续请求/等待中推进)
        for _ in range(500):
            h = manager.get(tid)
            if h is not None and h.status == "done":
                break
            time.sleep(0.02)
        h = manager.get(tid)
        assert h is not None and h.status == "done"
        assert h.report is not None and h.report.get("passed") is True
        assert out["approved"] is True

    def test_deny_endpoint_resolves(self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                    demo_repo: Path) -> None:
        """浏览器点「拒绝」:POST approvals/{aid} approve=False → 200,工具未执行。"""
        out: dict[str, Any] = {}
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, out))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting(client, demo_repo)

        resp = client.post(f"/api/tasks/{tid}/approvals/{aid}", json={"approve": False})
        assert resp.status_code == 200
        assert resp.json()["approved"] is False
        for _ in range(500):
            h = manager.get(tid)
            if h is not None and h.status == "done":
                break
            time.sleep(0.02)
        assert out["approved"] is False

    def test_unknown_approval_404(self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
                                  demo_repo: Path) -> None:
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, {}))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting(client, demo_repo)
        resp = client.post(f"/api/tasks/{tid}/approvals/ap-wrong", json={"approve": True})
        assert resp.status_code == 404

    def test_unknown_task_404(self, client: TestClient) -> None:
        resp = client.post("/api/tasks/ap-none/approvals/ap-1", json={"approve": True})
        assert resp.status_code == 404

    def test_body_missing_approve_422(self, client: TestClient,
                                      monkeypatch: pytest.MonkeyPatch, demo_repo: Path) -> None:
        """缺 approve 字段 → 422(Pydantic 校验先于 handler)。"""
        monkeypatch.setattr(api_app, "VerifiedLoop", _gate_loop_factory(demo_repo, {}))
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid, aid = self._spawn_awaiting(client, demo_repo)
        resp = client.post(f"/api/tasks/{tid}/approvals/{aid}", json={})
        assert resp.status_code == 422


class TestResumeEndpoint:
    """resume 端点:复用原任务仓库 + 请求现场,后台续跑(不重拷 demo/不重放)。"""

    @pytest.fixture()
    def client(self) -> Iterator[TestClient]:
        with TestClient(app) as c:
            yield c

    def _seed_error_task(self, demo_repo: Path) -> str:
        """sync 种一个 error 任务(无 Future 创建,可脱离事件循环)。"""
        tid = _tid("rs")
        handle = manager.create(tid)
        handle.status = "error"
        handle.error = "上轮失败"
        handle.repo = str(demo_repo)
        handle.request = _request(demo_repo).model_dump()
        return tid

    def test_resume_runs_again_reusing_repo(self, client: TestClient,
                                            monkeypatch: pytest.MonkeyPatch,
                                            demo_repo: Path) -> None:
        """error 任务 → resume → 复用原 repo/session 后台续跑(假 loop 秒完成)→ done。"""
        constructed: list[dict[str, Any]] = []
        ran_repos: list[Path] = []

        class _FakeLoop:
            def __init__(self, config: Any, **kw: Any) -> None:
                constructed.append(kw)

            async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
                ran_repos.append(repo)
                return _report(demo_repo, changed=True)

        monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid = self._seed_error_task(demo_repo)

        resp = client.post(f"/api/tasks/{tid}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "resumed"
        # 先等后台任务跑到终态:POST 只 create_task(不 await),TestClient 返回时
        # 后台协程可能尚未被调度 —— 此时断言构造次数是竞态(负载下会偶发)。
        # 注意必须等 "done" 而非 "error":任务**本来**就是 error(种下的失败任务),
        # 等 ("done","error") 会立刻命中并误判。
        for _ in range(250):
            h = manager.get(tid)
            if h is not None and h.status == "done":
                break
            time.sleep(0.02)
        h = manager.get(tid)
        assert h is not None and h.status == "done"
        # 假 loop 被构造一次(带 gate)+ run 在同一 repo 上(复用,不新拷)
        assert len(constructed) == 1
        assert constructed[0]["approval_gate"] is not None
        assert ran_repos == [Path(str(demo_repo))]

    def test_resume_running_rejected(self, client: TestClient,
                                     monkeypatch: pytest.MonkeyPatch, demo_repo: Path) -> None:
        """运行中/等待审批的任务不可 resume(避免双跑)。"""
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        tid = self._seed_error_task(demo_repo)
        manager.get(tid).status = "awaiting_approval"  # type: ignore[union-attr]
        resp = client.post(f"/api/tasks/{tid}/resume")
        assert resp.json()["error"]  # 明确拒绝,不重启

    def test_resume_unknown_task_404(self, client: TestClient) -> None:
        resp = client.post("/api/tasks/rs-none/resume")
        assert resp.status_code == 404

    def test_resume_no_repo_rejected(self, client: TestClient,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
        """从未绑定仓库的任务不可恢复(无会话续接点)。"""
        monkeypatch.setattr(api_app, "get_llm_config", lambda: _config())
        handle = manager.create(_tid("rs-norepo"))
        handle.status = "error"
        resp = client.post(f"/api/tasks/{handle.task_id}/resume")
        assert resp.json()["error"]


class TestPendingSummary:
    """任务列表端点暴露 awaiting 任务 + 审批概要(resume 按钮据此渲染)。"""

    @pytest.fixture()
    def client(self) -> Iterator[TestClient]:
        with TestClient(app) as c:
            yield c

    async def test_list_contains_awaiting_and_pending(self, demo_repo: Path) -> None:
        """awaiting 任务在列表里带 request 快照与 pending_approvals 摘要。"""
        tid = _tid("sum")
        handle = manager.create(tid)
        handle.request = _request(demo_repo).model_dump()
        handle.repo = str(demo_repo)
        handle.register_pending("sum-aid", tool_name="write_file", reason="越权写文件")

        mine = next(t for t in manager.list() if t["task_id"] == tid)
        assert mine["status"] == "awaiting_approval"
        assert mine["task"] == "修复 math_utils.py"  # 请求快照随任务带出
        assert mine["pending_approvals"] == [
            {
                "approval_id": "sum-aid",
                "kind": "tool",
                "tool_name": "write_file",
                "reason": "越权写文件",
                "plan": None,  # 逐工具审批无计划对象
            }
        ]
        # M10:非计划审批 → pending_plan_approvals 为空
        assert mine["pending_plan_approvals"] == []

        # 计划审批摘要:plan 字段承载被审对象,tool_name 为 None(不以 "plan" 冒充工具名)
        handle2 = manager.create(_tid("sum-plan"))
        handle2.register_pending(
            "sum-pl", kind="plan", reason="严格模式", plan=Plan(files=["a.py"], commands=["pytest"])
        )
        mine2 = next(t for t in manager.list() if t["task_id"] == handle2.task_id)
        assert mine2["status"] == "awaiting_plan_approval"
        assert mine2["pending_plan_approvals"] == [
            {
                "approval_id": "sum-pl",
                "kind": "plan",
                "tool_name": None,
                "reason": "严格模式",
                "plan": {"description": "", "files": ["a.py"], "commands": ["pytest"]},
            }
        ]
        assert mine2["pending_approvals"] == []

