"""M0 冒烟测试:项目骨架可导入、CLI 存在、demo_repo 模板可初始化为 git 仓库。

- demo_repo 是独立样例仓库,其测试由 Agent/评测触发,不纳入本 pytest 套件。
- `tests/fixtures/demo_repo/` 是只读模板(入库不含 .git);运行期在 tmp_path 副本上
  初始化,见 `tests/conftest.py` 的 `demo_repo` fixture。
"""

from __future__ import annotations

import subprocess
import sys


def test_cli_help() -> None:
    """mewcode CLI 可运行并打印 help。"""
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        capture_output=True,
        text=False,  # 原始字节:子进程按 UTF-8 输出,父进程解码不可靠(Windows GBK)
        check=False,
    )
    assert result.returncode == 0
    assert b"mewcode" in result.stdout


def test_packages_importable() -> None:
    """核心包目录存在且可导入(空包不报错)。"""
    import agent
    import cli
    import tools

    assert agent.__name__ == "agent"
    assert cli.__name__ == "cli"
    assert tools.__name__ == "tools"
