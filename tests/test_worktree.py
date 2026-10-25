"""M0 端到端测试:Git Worktree add / list / remove 生命周期与隔离性。

使用 conftest 的 `worktree` fixture:在 tmp_path 副本上创建,自带 finally 清理,
避免失败残留破坏后续运行(见 M0 review P0 问题)。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def repo(demo_repo: Path) -> Path:
    return demo_repo


def _git_in(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _worktree_is_listed(repo: Path, wt: Path) -> bool:
    """git worktree list 中是否包含该 worktree(规范化路径比较,兼容 Windows)。"""
    listing = _git_in(repo, "worktree", "list").stdout
    expected = wt.resolve().as_posix()  # 统一为 / 分隔
    return any(expected in line for line in listing.splitlines())


def test_worktree_add_list_remove_lifecycle(repo: Path, worktree: Path) -> None:
    assert _worktree_is_listed(repo, worktree)
    assert (worktree / "math_utils.py").exists()
    # fixture 结束时会 remove;此处不重复断言移除后状态


def test_worktree_isolation_untracked_file(repo: Path, worktree: Path) -> None:
    """worktree 内新增未跟踪文件不污染主仓库状态。"""
    (worktree / "note.txt").write_text("change", encoding="utf-8")
    status = _git_in(repo, "status", "--short").stdout
    assert "note.txt" not in status


def test_worktree_commit_not_seen_by_main(repo: Path, worktree: Path) -> None:
    """worktree 内的提交不影响主仓库当前分支的 HEAD。"""
    main_head = _git_in(repo, "rev-parse", "HEAD").stdout.strip()
    (worktree / "committed.txt").write_text("y", encoding="utf-8")
    _git_in(worktree, "add", "-A")
    _git_in(
        worktree,
        "-c", "user.name=t",
        "-c", "user.email=t@example.com",
        "commit", "-q", "-m", "wt commit",
    )
    assert _git_in(repo, "rev-parse", "HEAD").stdout.strip() == main_head


def test_worktree_parallel_worktrees(repo: Path, worktree: Path, tmp_path: Path) -> None:
    """同一仓库可同时存在多个独立 worktree。"""
    wt2 = tmp_path / "wt-p2"
    branch2 = "feat/p2"
    _git_in(repo, "worktree", "add", "-q", "-b", branch2, str(wt2))
    try:
        assert _worktree_is_listed(repo, worktree)
        assert _worktree_is_listed(repo, wt2)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "-f", str(wt2)],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch2],
            capture_output=True,
            text=True,
            check=False,
        )
