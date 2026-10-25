"""目标仓库准备:显式路径校验或复制 demo_repo 到临时目录并 git init。

从 cli/main.py 抽出(CLI 模块只该管 argv;repo 准备被 CLI 与脚本共用)。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# demo_repo 样例仓库(未指定 --repo 时的默认目标)
_DEMO_REPO = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "demo_repo"


def prepare_repo(repo_arg: str | None) -> Path:
    """确定目标仓库:显式指定或复制 demo_repo 到临时目录并 git init。"""
    if repo_arg is not None:
        repo = Path(repo_arg).resolve()
        if not repo.exists():
            print(f"错误:仓库路径不存在: {repo}")
            sys.exit(1)
        if not (repo / ".git").exists():
            print(f"错误:不是 git 仓库: {repo}")
            sys.exit(1)
        return repo
    workdir = Path(tempfile.mkdtemp()) / "demo_repo"
    shutil.copytree(_DEMO_REPO, workdir)
    subprocess.run(["git", "-C", str(workdir), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(workdir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(workdir), "-c", "user.name=demo", "-c", "user.email=demo@example.com",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return workdir
