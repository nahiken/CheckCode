"""tui 内核的测试(纯 asyncio,**不需要 Textual**)。

这是分层约定(`tui/gates.py` 等不导入 Textual)的直接红利:审批桥与任务编排的
全部逻辑可以在毫秒级覆盖,不必启动 Textual 的 pilot。
"""

from __future__ import annotations

import asyncio

import pytest

from api.tasks import TaskManager
from policy.models import PermissionDecision, Plan
from tui.gates import TERMINAL, ApprovalBridge


def _ask(reason: str = "越权写操作") -> PermissionDecision:
    return PermissionDecision(decision="ask", layer="mode", reason=reason)


async def _wait_status(manager: TaskManager, tid: str, status: str) -> None:
    """轮询等任务进入指定状态(后台协程调度,不用固定 sleep)。"""
    for _ in range(200):
        handle = manager.get(tid)
        if handle is not None and handle.status == status:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"任务未进入 {status}(当前 {manager.get(tid).status if manager.get(tid) else None})")  # type: ignore[union-attr]


# === 审批桥:登记 → resolve 解阻塞 ===

async def test_tool_gate_registers_then_resolve_unblocks() -> None:
    """gate 登记 pending 并挂起;resolve_pending 唤醒它并返回审批结果。"""
    manager = TaskManager()
    manager.create("t1")
    bridge = ApprovalBridge(manager, "t1", auto_approve=False)

    job = asyncio.create_task(bridge.tool_gate("write_file", {"path": "x"}, _ask()))
    await _wait_status(manager, "t1", "awaiting_approval")

    handle = manager.get("t1")
    assert handle is not None
    aid = next(iter(handle.pending))
    # 摘要含工具名与理由(list() / 前端据此渲染审批面板)
    summary = handle.pending_summary
    assert summary[0]["kind"] == "tool"
    assert summary[0]["tool_name"] == "write_file"

    assert handle.resolve_pending(aid, True) is True
    assert await asyncio.wait_for(job, timeout=1.0) is True
    assert handle.pending == {}  # 无残留


async def test_deny_returns_false_and_clears_pending() -> None:
    manager = TaskManager()
    manager.create("t1")
    bridge = ApprovalBridge(manager, "t1", auto_approve=False)

    job = asyncio.create_task(bridge.tool_gate("write_file", {}, _ask()))
    await _wait_status(manager, "t1", "awaiting_approval")
    handle = manager.get("t1")
    assert handle is not None
    aid = next(iter(handle.pending))

    assert handle.resolve_pending(aid, False) is True
    assert await asyncio.wait_for(job, timeout=1.0) is False
    assert handle.pending == {}


async def test_auto_approve_short_circuits_without_pending() -> None:
    """auto_approve=True → 直接返回 True,**不**登记 pending、不置 awaiting 状态。"""
    manager = TaskManager()
    manager.create("t1")
    manager.set_running("t1")
    bridge = ApprovalBridge(manager, "t1", auto_approve=True)

    assert await bridge.tool_gate("write_file", {}, _ask()) is True
    handle = manager.get("t1")
    assert handle is not None
    assert handle.pending == {}
    assert handle.status == "running"  # 未被审批状态覆盖


async def test_missing_task_denies_without_hanging() -> None:
    """任务已消失 → 拒绝(不悬挂)。审批是安全边界,不默认放行。"""
    manager = TaskManager()
    bridge = ApprovalBridge(manager, "gone", auto_approve=False)

    assert await asyncio.wait_for(bridge.tool_gate("write_file", {}, _ask()), 1.0) is False


async def test_plan_gate_uses_plan_registration() -> None:
    """计划审批走 set_awaiting_plan_approval —— 状态与摘要都标 kind=plan。"""
    manager = TaskManager()
    manager.create("t1")
    bridge = ApprovalBridge(manager, "t1", auto_approve=False)
    plan = Plan(description="改 math_utils", files=["math_utils.py"], commands=["pytest"])

    job = asyncio.create_task(bridge.plan_gate(plan))
    await _wait_status(manager, "t1", "awaiting_plan_approval")
    handle = manager.get("t1")
    assert handle is not None
    aid = next(iter(handle.pending))
    summary = handle.pending_summary
    assert summary[0]["kind"] == "plan"
    # 计划对象原样带出(前端/终端渲染同一个 Plan,而不是拆成字段再拼回去)
    assert summary[0]["plan"]["files"] == ["math_utils.py"]

    handle.resolve_pending(aid, True)
    assert await asyncio.wait_for(job, timeout=1.0) is True


async def test_on_request_callback_fires_with_id_and_kind() -> None:
    """UI 通知回调收到 (approval_id, kind) —— 终端据此弹出对应模态框。"""
    manager = TaskManager()
    manager.create("t1")
    seen: list[tuple[str, str]] = []
    bridge = ApprovalBridge(
        manager, "t1", auto_approve=False, on_request=lambda aid, kind: seen.append((aid, kind))
    )

    job = asyncio.create_task(bridge.tool_gate("read_file", {}, _ask()))
    await _wait_status(manager, "t1", "awaiting_approval")
    handle = manager.get("t1")
    assert handle is not None
    aid = next(iter(handle.pending))
    assert seen == [(aid, "tool")]
    handle.resolve_pending(aid, True)
    await asyncio.wait_for(job, timeout=1.0)


# === 终止事件集合(消费协程据此停止) ===

def test_terminal_events_match_web_semantics() -> None:
    """终止事件集合必须与 Web 侧一致(report/error/rollback)。

    `approval` / `plan_approval` **不在其中** —— 审批是过程内暂停,不是流终点。
    若把 approval 误加进来,审批一发生消费协程就停止,后续事件全部丢失。
    """
    assert TERMINAL == ("report", "error", "rollback")
    assert "approval" not in TERMINAL
    assert "plan_approval" not in TERMINAL


# === actions:回滚守卫(与 Web 共用 workbench.rollback) ===

async def test_rollback_guard_rejects_plan_rejected_task() -> None:
    """计划被拒的任务不可回滚 —— 它从未创建分支,force_branch 会凭空建出来。"""
    from tui.actions import rollback_task

    manager = TaskManager()
    manager.create("t1")
    handle = manager.get("t1")
    assert handle is not None
    handle.repo = "/tmp/whatever"
    handle.base_commit = "abc"
    manager.set_done("t1", {"plan_rejected": True})

    err = rollback_task(manager, "t1")
    assert err is not None and "计划被拒绝" in err


async def test_rollback_guard_rejects_readonly_task() -> None:
    from tui.actions import rollback_task

    manager = TaskManager()
    manager.create("t1")
    handle = manager.get("t1")
    assert handle is not None
    handle.repo = "/tmp/whatever"
    handle.base_commit = "abc"
    manager.set_done("t1", {"readonly": True})

    err = rollback_task(manager, "t1")
    assert err is not None and "只读" in err


async def test_rollback_guard_rejects_running_task() -> None:
    from tui.actions import rollback_task

    manager = TaskManager()
    manager.create("t1")
    handle = manager.get("t1")
    assert handle is not None
    handle.repo = "/tmp/whatever"
    handle.base_commit = "abc"
    manager.set_running("t1")

    err = rollback_task(manager, "t1")
    assert err is not None and "不可回滚" in err


async def test_rollback_guard_rejects_unknown_task() -> None:
    from tui.actions import rollback_task

    assert rollback_task(TaskManager(), "nope") == "task not found"


# === actions:resolve_approval 是唯一写入点 ===

async def test_resolve_approval_unblocks_and_returns_to_running() -> None:
    """resolve 成功后回到 running —— 模态框与列表行按键共用这一个入口。"""
    from tui.actions import resolve_approval

    manager = TaskManager()
    manager.create("t1")
    bridge = ApprovalBridge(manager, "t1", auto_approve=False)
    job = asyncio.create_task(bridge.tool_gate("write_file", {}, _ask()))
    await _wait_status(manager, "t1", "awaiting_approval")
    handle = manager.get("t1")
    assert handle is not None
    aid = next(iter(handle.pending))

    assert resolve_approval(manager, "t1", aid, True) is True
    assert await asyncio.wait_for(job, timeout=1.0) is True
    assert manager.get("t1").status == "running"  # type: ignore[union-attr]


def test_resolve_approval_unknown_ids_return_false() -> None:
    from tui.actions import resolve_approval

    manager = TaskManager()
    assert resolve_approval(manager, "nope", "aid", True) is False
    manager.create("t1")
    assert resolve_approval(manager, "t1", "no-such-aid", True) is False


# === actions:create_task 的前置校验 ===

async def test_create_task_rejects_blank_task() -> None:
    from tui.actions import create_task

    with pytest.raises(ValueError, match="不能为空"):
        create_task(TaskManager(), task="   ", emit=lambda _e: None)


async def test_create_task_without_api_key_sets_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 API key → 任务直接 error(**不启动**),与 Web 的 create_task 同款语义。"""
    from agent.config import LLMConfig
    from tui import actions

    monkeypatch.setattr(
        actions, "get_llm_config", lambda: LLMConfig(base_url="", api_key="", model="")
    )
    manager = TaskManager()
    session = actions.create_task(manager, task="修复", emit=lambda _e: None)
    handle = manager.get(session.task_id)
    assert handle is not None
    assert handle.status == "error"
    assert handle.error is not None and "API key" in handle.error


# === resolve_repo:失败不退出进程(不污染全屏渲染) ===

def test_resolve_repo_rejects_missing_path_without_exit(tmp_path) -> None:
    """坏路径 → ValueError(**不是 SystemExit**);prepare_repo 的 print 也不会发生。"""
    from tui.session import resolve_repo

    with pytest.raises(ValueError, match="不存在"):
        resolve_repo(str(tmp_path / "nope"))


def test_resolve_repo_rejects_non_git_dir(tmp_path) -> None:
    from tui.session import resolve_repo

    (tmp_path / "plain").mkdir()
    with pytest.raises(ValueError, match="不是 git 仓库"):
        resolve_repo(str(tmp_path / "plain"))


def test_resolve_repo_accepts_valid_git_repo(tmp_path) -> None:
    from tui.session import resolve_repo

    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    assert resolve_repo(str(repo)) == repo.resolve()
