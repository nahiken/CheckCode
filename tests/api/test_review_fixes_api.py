"""M15 审计修复的回归测试(第二轮 code review):API 侧。

- B5:resume 失败后 /report /diff 不得继续返回上一轮的成果。
- B7:计划被拒 / 只读分析的任务不可回滚(force_branch 会凭空建出分支)。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

api_app = importlib.import_module("api.app")
from api.tasks import TaskManager  # noqa: E402
from workbench import rollback as workbench_rollback  # noqa: E402


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch) -> TaskManager:
    m = TaskManager()
    monkeypatch.setattr(api_app, "manager", m)
    return m


def test_set_error_clears_stale_report(manager: TaskManager) -> None:
    """审计 B5:任务先 done(有报告)后转 error → report 必须清空。

    否则 /report 与 /diff 会以 200 返回**上一次**的成果,把「本次失败」读成「本次通过」。
    """
    manager.create("t1")
    manager.set_done("t1", {"passed": True, "diff": "OLD-DIFF", "changed_files": ["a.py"]})
    assert manager.get("t1").report is not None  # type: ignore[union-attr]

    manager.set_error("t1", "resume exploded")

    h = manager.get("t1")
    assert h is not None
    assert h.status == "error"
    assert h.report is None  # 旧报告不残留


async def test_report_endpoint_409_after_error(manager: TaskManager) -> None:
    """行为面:error 态的 /report 必须是 409,而不是 200 + 旧报告。"""
    from fastapi import HTTPException

    manager.create("t2")
    manager.set_done("t2", {"passed": True, "diff": "OLD"})
    manager.set_error("t2", "boom")

    with pytest.raises(HTTPException) as ei:
        await api_app.task_report("t2")
    assert ei.value.status_code == 409


async def test_rollback_rejected_for_plan_rejected_task(
    manager: TaskManager, demo_repo: Path
) -> None:
    """审计 B7:计划被拒的任务未创建分支 → 回滚必须被拒(不能凭空 branch -f 建出来)。"""
    manager.create("t3")
    h = manager.get("t3")
    assert h is not None
    h.repo = str(demo_repo)
    h.base_commit = "deadbeef"
    manager.set_done("t3", {"plan_rejected": True, "changed_files": [], "diff": ""})

    out = await api_app.rollback_task("t3")

    assert "error" in out
    assert "计划被拒绝" in out["error"]


async def test_rollback_rejected_for_readonly_task(
    manager: TaskManager, demo_repo: Path
) -> None:
    """对照:只读分析任务同样无分支可回滚。"""
    manager.create("t4")
    h = manager.get("t4")
    assert h is not None
    h.repo = str(demo_repo)
    h.base_commit = "deadbeef"
    manager.set_done("t4", {"readonly": True, "changed_files": [], "diff": ""})

    out = await api_app.rollback_task("t4")

    assert "error" in out
    assert "只读" in out["error"]


async def test_rollback_still_allowed_for_real_changes(
    manager: TaskManager, demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照:真有改动的任务仍可回滚(修复不能一刀切禁掉回滚)。"""
    called: dict[str, Any] = {}

    class _Mgr:
        def __init__(self, repo: Path) -> None:
            pass

        def force_branch(self, branch: str, commit: str) -> None:
            called["branch"] = branch
            called["commit"] = commit

    # 回滚的**执行**已抽到 workbench.rollback(Web 与终端共用),故 patch 目标随之迁移:
    # 它用的是自己模块级的 WorktreeManager,打 api_app 上的引用已无效。
    # 这正是「实现搬家,接缝跟着搬」——测试打的是实现细节,不是公开契约。
    monkeypatch.setattr(workbench_rollback, "WorktreeManager", _Mgr)
    manager.create("t5")
    h = manager.get("t5")
    assert h is not None
    h.repo = str(demo_repo)
    h.base_commit = "cafe"
    manager.set_done("t5", {"passed": True, "changed_files": ["a.py"], "diff": "+x"})

    out = await api_app.rollback_task("t5")

    assert out.get("status") == "rolled_back"
    assert called["commit"] == "cafe"
