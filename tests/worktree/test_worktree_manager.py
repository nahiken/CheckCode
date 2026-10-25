"""M3 单元/集成测试:Git Worktree 生命周期 + Diff/变更文件清单。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from worktree.manager import WorktreeError, WorktreeManager


def _git_in(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _worktree_is_listed(repo: Path, wt: Path) -> bool:
    listing = _git_in(repo, "worktree", "list").stdout
    expected = wt.resolve().as_posix()
    return any(expected in line for line in listing.splitlines())


def _manager(repo: Path) -> WorktreeManager:
    return WorktreeManager(repo)


# === 生命周期 ===

def test_create_worktree_registered(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    wt = m.create()
    try:
        assert _worktree_is_listed(demo_repo, wt.path)
        assert (wt.path / "math_utils.py").exists()  # 内容来自主仓
        assert wt.branch.startswith("sess-")  # M4:默认分支即 session_id 前缀
    finally:
        m.remove(wt)


def test_remove_cleans_worktree_and_branch(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    wt = m.create()
    m.remove(wt)
    assert not _worktree_is_listed(demo_repo, wt.path)
    assert not wt.path.exists()
    # 分支已删除:checkout 该分支应失败
    branches = _git_in(demo_repo, "branch").stdout
    assert wt.branch not in branches


def test_remove_idempotent(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    wt = m.create()
    m.remove(wt)
    m.remove(wt)  # 再次移除不应抛错


def test_create_custom_branch_and_path(demo_repo: Path, tmp_path: Path) -> None:
    m = _manager(demo_repo)
    target = tmp_path / "custom-wt"
    wt = m.create(branch="feat/custom", path=target)
    try:
        assert wt.branch == "feat/custom"
        assert wt.path == target.resolve()
        assert _worktree_is_listed(demo_repo, wt.path)
    finally:
        m.remove(wt)


def test_create_recovers_from_stale_leftover(demo_repo: Path) -> None:
    """崩溃残留(孤儿分支)被自动清理并重试成功。

    场景:worktree 被移除但分支未删 → 再 create 同名分支应清理后成功。
    """
    m = _manager(demo_repo)
    # 制造残留:创建 worktree、commit,然后只拆 worktree 不删分支
    wt = m.create(branch="feat/orphan")
    (wt.path / "stale.txt").write_text("x", encoding="utf-8")
    _git_in(wt.path, "add", "-A")
    _git_in(
        wt.path,
        "-c", "user.name=t",
        "-c", "user.email=t@example.com",
        "commit", "-q", "-m", "stale",
    )
    _git_in(demo_repo, "worktree", "remove", "-f", str(wt.path))  # 分支遗留

    wt2 = m.create(branch="feat/orphan")  # 自动清理孤儿分支后成功
    try:
        assert _worktree_is_listed(demo_repo, wt2.path)
        assert wt2.branch == "feat/orphan"
    finally:
        m.remove(wt2)


def test_create_live_conflict_raises(demo_repo: Path) -> None:
    """同名分支被活动 worktree 占用 → 报错(真冲突,不静默覆盖)。"""
    m = _manager(demo_repo)
    wt1 = m.create(branch="feat/live")
    try:
        with pytest.raises(WorktreeError, match="worktree add 失败"):
            m.create(branch="feat/live")
    finally:
        m.remove(wt1)


def test_manager_requires_git_repo(tmp_path: Path) -> None:
    with pytest.raises(WorktreeError, match="不是 git 仓库"):
        WorktreeManager(tmp_path)


# === 隔离性 ===

def test_worktree_changes_do_not_touch_main(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    base = m.base_commit()
    wt = m.create()
    try:
        # worktree 内新增文件 + 提交
        (wt.path / "note.txt").write_text("change", encoding="utf-8")
        _git_in(wt.path, "add", "-A")
        _git_in(
            wt.path,
            "-c", "user.name=t",
            "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "wt change",
        )
        # 主仓库工作树无变化、HEAD 未动
        assert _git_in(demo_repo, "status", "--short").stdout == ""
        assert m.base_commit() == base
    finally:
        m.remove(wt)


# === Diff / 变更文件 ===

def test_diff_and_changed_files(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    base = m.base_commit()
    wt = m.create()
    try:
        (wt.path / "math_utils.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        _git_in(wt.path, "add", "-A")
        _git_in(
            wt.path,
            "-c", "user.name=t",
            "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "fix math_utils",
        )
        assert m.changed_files(base, wt) == ["math_utils.py"]
        diff = m.diff(base, wt)
        assert "math_utils.py" in diff
        assert "def add" in diff
    finally:
        m.remove(wt)


def test_no_changes_empty_diff(demo_repo: Path) -> None:
    m = _manager(demo_repo)
    base = m.base_commit()
    wt = m.create()
    try:
        assert m.changed_files(base, wt) == []
        assert m.diff(base, wt) == ""
    finally:
        m.remove(wt)


# === 回滚(M10:force_branch 覆盖;闭环产出可回滚分支,回滚由 API/CLI 触发)===

def test_force_branch_resets_to_base_discarding_changes(demo_repo: Path) -> None:
    """force_branch 把分支指回基线:Agent 改动被丢弃,diff 清空,主工作树不受影响。

    契约(与生产 rollback 一致):worktree 已拆除、分支保留(keep_branch=True)后调
    force_branch——git 拒绝对「仍被活动 worktree 检出的分支」force update。
    """
    m = _manager(demo_repo)
    base = m.base_commit()
    wt = m.create(branch="feat/rollback")
    (wt.path / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    _git_in(wt.path, "add", "-A")
    _git_in(
        wt.path,
        "-c", "user.name=t",
        "-c", "user.email=t@example.com",
        "commit", "-q", "-m", "agent change",
    )
    assert m.changed_files(base, wt) == ["math_utils.py"]  # 分支含 Agent 改动
    # 拆除 worktree 但保留分支(生产 rollback 的前置状态)
    m.remove(wt, keep_branch=True)

    m.force_branch("feat/rollback", base)  # 回滚

    # 分支已被重置到基线 → 相对基线无改动
    assert m.changed_files(base, wt) == []
    assert m.diff(base, wt) == ""
    # 主工作树未被触碰
    assert _git_in(demo_repo, "status", "--short").stdout == ""
    assert m.base_commit() == base


def test_force_branch_active_worktree_raises(demo_repo: Path) -> None:
    """分支仍被活动 worktree 检出 → force_branch 报错(不静默破坏)。"""
    m = _manager(demo_repo)
    base = m.base_commit()
    wt = m.create(branch="feat/active")
    try:
        with pytest.raises(WorktreeError, match="git branch -f 失败"):
            m.force_branch("feat/active", base)
    finally:
        m.remove(wt)


