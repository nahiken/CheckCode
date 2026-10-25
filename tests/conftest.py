"""pytest 共享 fixture:demo_repo 的只读模板 + tmp_path 可写副本。

设计要点(应对 M0 review 发现的问题):
- `tests/fixtures/demo_repo/` 是**只读模板**,入库时不含 .git、不参与运行期写操作。
- 每个测试在 `tmp_path` 的**副本**上 `git init`,消除:原位污染、并发 TOCTOU、
  worktree/分支残留破坏幂等、CI worker 并行竞态。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

DEMO_REPO = Path(__file__).parent / "fixtures" / "demo_repo"


def _git(workdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture()
def demo_repo(tmp_path: Path) -> Path:
    """复制模板到 tmp_path,初始化为独立 git 仓库,返回其路径。

    模板保持只读;本仓库作为样例仓库,可被测试/Agent 反复修改、回滚。
    """
    repo = tmp_path / "demo_repo"
    shutil.copytree(DEMO_REPO, repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.name=demo",
        "-c", "user.email=demo@example.com",
        "commit", "-q", "-m", "init",
    )
    return repo


@pytest.fixture()
def worktree(demo_repo: Path, tmp_path: Path) -> Iterator[Path]:
    """在 demo_repo 上创建隔离 Worktree,返回其路径。自带清理。

    使用唯一分支名,避免并行测试冲突;结束后 best-effort 清理(不抛错),
    保证残留不会破坏后续测试的幂等性。
    """
    branch = f"feat/{tmp_path.name}"
    wt = tmp_path / "worktree"
    _git(demo_repo, "worktree", "add", "-q", "-b", branch, str(wt))
    yield wt
    subprocess.run(
        ["git", "-C", str(demo_repo), "worktree", "remove", "-f", str(wt)],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(demo_repo), "branch", "-D", branch],
        capture_output=True,
        text=True,
        check=False,
    )
