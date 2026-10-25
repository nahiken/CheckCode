"""workbench/session_id.py —— 任务 ↔ 会话/分支 id 的单一来源。"""

from __future__ import annotations


def new_session_id(task_id: str) -> str:
    """任务绑定会话 id(单一来源):run 与 resume 用同一 session 自动续接。

    这个 id 同时是 **Git 分支名**(`WorktreeManager.create(branch=...)`)与
    **会话文件名**(`session_path(...)`)。三处(Web 创建、Web 回滚、终端创建)
    必须算出同一个值,否则回滚会打到一条不存在的分支 —— 故收敛到此处。
    """
    return f"sess-{task_id[:8]}"
