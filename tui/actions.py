"""tui/actions.py —— 工作台动作(创建/回滚/恢复/读 Trace),**不导入 Textual**。

这些是 Web 端点(`api/app.py`)做过的事,去掉 HTTP 外壳后的纯逻辑。与 Web 共享:
- 回滚守卫与执行 → `workbench.rollback`(三条安全守卫单一来源)
- 报告序列化 → `workbench.report`
- 会话 id → `workbench.session_id`

**为什么不让 TUI 直接调 Web 端点**:那要求 TUI 起一个 HTTP 客户端并假设服务端在跑 ——
而 TUI 是独立进程,它就是「服务端」本身(进程内直接持有 TaskManager)。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from agent.config import get_llm_config
from api.tasks import TaskManager
from tui.session import EmitFn, TaskSession
from workbench.rollback import rollback_branch, rollback_guard


def new_task_id() -> str:
    """12 位十六进制任务 id(与 Web 侧 `uuid.uuid4().hex[:12]` 同款)。"""
    return uuid.uuid4().hex[:12]


def create_task(
    manager: TaskManager,
    *,
    task: str,
    emit: EmitFn,
    repo: str | None = None,
    strict: bool = False,
    auto_approve: bool = False,
    skills: list[str] | None = None,
    config_path: str | None = None,
    transport: Any = None,
    test_command: str | None = None,
    max_fix_rounds: int | None = None,
    on_request_approval: Any = None,
) -> TaskSession:
    """登记并启动一个任务,返回可等待的 TaskSession。

    前置校验与 Web 的 `_TaskRequest` 等价(它本身是 pydantic,但这里只需两条):
    任务非空、API key 已配 —— 缺 key 时**直接置 error 而不启动**,与 Web 一致。
    """
    if not task.strip():
        raise ValueError("task 不能为空")

    task_id = new_task_id()
    handle = manager.create(task_id)
    config = get_llm_config()
    if not config.api_key:
        manager.set_error(
            task_id,
            "未配置 API key(.env 中 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 或 LLM_API_KEY)",
        )
        # 返回一个已结束的 session(不 start),让调用方走统一的展示路径
        return TaskSession(
            manager, task_id, handle, {"task": task}, emit=emit,
            transport=transport, test_command=test_command,
        )

    request: dict[str, Any] = {
        "task": task,
        "repo": repo,
        "strict": strict,
        "auto_approve": auto_approve,
        "skills": skills,
        "config": config_path,
        "_config": config,
    }
    session = TaskSession(
        manager,
        task_id,
        handle,
        request,
        emit=emit,
        transport=transport,
        test_command=test_command,
        max_fix_rounds=max_fix_rounds,
        on_request_approval=on_request_approval,
    )
    session.start()
    return session


def resume_task(
    manager: TaskManager,
    task_id: str,
    *,
    emit: EmitFn,
    transport: Any = None,
    test_command: str | None = None,
    max_fix_rounds: int | None = None,
    on_request_approval: Any = None,
) -> tuple[TaskSession | None, str | None]:
    """恢复任务:复用原仓库 + 同 session 续跑(不重放已完成副作用)。

    返回 `(session, error)` —— 二选一非空。守卫与 Web 的 `/resume` 端点一致:
    运行中/等待审批的任务不恢复(它还在跑)。
    """
    handle = manager.get(task_id)
    if handle is None:
        return None, "任务不存在"
    if handle.status in ("running", "awaiting_approval", "awaiting_plan_approval"):
        return None, f"任务 {handle.status} 运行中,无需恢复"
    if handle.repo is None:
        return None, "任务尚未绑定仓库,无法恢复(请先运行一次)"

    config = get_llm_config()
    if not config.api_key:
        return None, "未配置 API key(.env 中 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 或 LLM_API_KEY)"

    # 恢复现场 = 原请求快照 + resume 标志(复用 repo,不新拷 demo)
    request: dict[str, Any] = dict(handle.request or {"task": "resume"})
    request["_config"] = config
    request["_resume"] = True
    session = TaskSession(
        manager,
        task_id,
        handle,
        request,
        emit=emit,
        transport=transport,
        test_command=test_command,
        max_fix_rounds=max_fix_rounds,
        on_request_approval=on_request_approval,
    )
    session.start()
    return session, None


def rollback_task(manager: TaskManager, task_id: str) -> str | None:
    """回滚任务修改,返回错误信息(成功 → None)。

    守卫与执行都来自 `workbench.rollback` —— **与 Web 端点共用同一份**,不重写。
    """
    handle = manager.get(task_id)
    reason = rollback_guard(handle)
    if reason is not None:
        return reason
    # rollback_guard 已保证这两个字段非空
    assert handle is not None and handle.repo and handle.base_commit
    try:
        manager.set_rollback(task_id, rollback_branch(handle.repo, handle.base_commit, task_id))
        return None
    except Exception as exc:  # noqa: BLE001 - 回滚失败转任务错误,不冒泡
        manager.set_error(task_id, f"回滚失败: {exc}")
        return str(exc)


def resolve_approval(manager: TaskManager, task_id: str, approval_id: str, approved: bool) -> bool:
    """resolve 一个进行中的审批(pending 中的 Future)。**两类审批共用此入口。**

    Web 侧由两个端点调用同一个 `_resolve_approval`;TUI 侧由模态框与列表行按键
    调用同一个本函数 —— 单一写入点是「多触发点不分叉」的关键(见 `tui/gates.py`)。
    """
    handle = manager.get(task_id)
    if handle is None:
        return False
    if not handle.resolve_pending(approval_id, approved):
        return False
    manager.set_running(task_id)  # 审批结束 → 回到运行中
    return True


def session_trace(session_file: str | None, run_id: str | None = None) -> str:
    """读会话文件渲染 Trace markdown(离线,不调模型)。无文件 → 提示文本。"""
    if not session_file:
        return "(会话文件尚未生成)"
    path = Path(session_file)
    if not path.exists():
        return f"(会话文件不存在: {path})"
    from verify.trace import load_session_trace, render_markdown

    return render_markdown(load_session_trace(path, run_id=run_id))


__all__ = [
    "create_task",
    "new_task_id",
    "resolve_approval",
    "resume_task",
    "rollback_task",
    "session_trace",
]
