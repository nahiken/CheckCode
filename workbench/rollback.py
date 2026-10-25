"""workbench/rollback.py —— 回滚的安全守卫与执行(Web 与终端共用)。

**这个模块的存在理由就是"守卫不能抄两遍"**:下面三条前置拒绝各自对应一次真实的
审计发现,任何一条抄漏,回滚就会做出用户没要求、且与既有承诺矛盾的事。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.session_id import new_session_id
from worktree.manager import WorktreeManager


def rollback_guard(handle: Any) -> str | None:
    """回滚前置检查:**可回滚返回 None,否则返回拒绝原因**(给用户看)。

    三条拒绝,顺序即优先级(先报最根本的原因):

    1. **无基线**(`handle.repo` / `base_commit` 为空)—— 任务还没跑完过,
       没有可回退的目标。
    2. **状态不符**:仅 `done` / `error` / `rolled_back` 可回滚。运行中/等待审批的
       任务拒绝(`running` 时回滚会与正在进行的写入竞争)。
    3. **零改动任务不可回滚**:计划被拒(`plan_rejected`)或只读分析(`readonly`)
       从未创建分支 —— `force_branch` 的 `git branch -f` 会**凭空建出**那条
       M10 承诺「拒绝即不存在」的分支(审计 M10–M15 B7)。提前拒绝比事后建分支诚实。
    """
    if handle is None:
        return "task not found"
    if handle.repo is None or handle.base_commit is None:
        return "任务尚未完成,无可回滚基线"
    if handle.status not in ("done", "error", "rolled_back"):
        return f"任务状态 {handle.status} 不可回滚"
    report = handle.report or {}
    if report.get("plan_rejected"):
        return "计划被拒绝,未做任何修改,无需回滚"
    if report.get("readonly"):
        return "只读分析任务无文件改动,无需回滚"
    return None


def rollback_branch(repo: str, base_commit: str, task_id: str) -> str:
    """把任务分支强制移回基线提交,返回给用户看的结果消息。

    回滚语义:任务分支已含 Agent commit(`commit_all` 落分支)→ 强制分支回到基线,
    丢弃全部 Agent 改动。Agent 的改动**从未进入主工作树**(worktree 隔离),
    因此只移动分支指针即可,不得 reset 主仓(会摧毁无关工作)。

    调用方**必须**先过 `rollback_guard` —— 本函数不做守卫判断(单一职责),
    它假设调用方已确认这条分支该存在。
    """
    branch = new_session_id(task_id)
    WorktreeManager(Path(repo)).force_branch(branch, base_commit)
    return f"已回滚到 {base_commit}"
