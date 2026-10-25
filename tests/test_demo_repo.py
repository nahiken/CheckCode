"""M0 demo_repo 层测试:fixture 幂等 + math_utils 当前行为(记录缺陷状态)。

demo_repo 是独立样例仓库,由 conftest 的 `demo_repo` fixture 在 tmp_path 副本上初始化。
本文件断言「当前实现」的实际行为,并把与目标不一致之处作为已知缺陷记录下来:

- divide(1, 0):当前抛 ZeroDivisionError,目标应抛 ValueError。
- fib(-5):当前返回 -5,目标应返回 -1。

这两处是 M0 故意遗留、供未来 Agent 修复的缺陷,因此这里断言的是"现状",
而不是目标。一旦 Agent 修复,这些测试应更新为断言目标行为。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture()
def repo(demo_repo: Path) -> Path:
    return demo_repo


def _load_math_utils(repo: Path) -> ModuleType:
    """从副本直接加载 math_utils 模块(不依赖包安装)。"""
    src = repo / "math_utils.py"
    spec = importlib.util.spec_from_file_location("_demo_math_utils", src)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_demo_repo_is_git_repo(repo: Path) -> None:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout.strip() == "true"


def test_demo_repo_expected_files_present(repo: Path) -> None:
    for rel in ("math_utils.py", "tests/test_math_utils.py", "README.md"):
        assert (repo / rel).exists(), rel


def test_math_utils_add_subtract_ok(repo: Path) -> None:
    m = _load_math_utils(repo)
    assert m.add(1, 2) == 3
    assert m.subtract(5, 3) == 2


def test_math_utils_divide_normal_ok(repo: Path) -> None:
    m = _load_math_utils(repo)
    assert m.divide(10, 2) == 5.0


def test_math_utils_divide_by_zero_current_bug(repo: Path) -> None:
    """缺陷记录:当前抛 ZeroDivisionError;目标行为为抛 ValueError。"""
    m = _load_math_utils(repo)
    with pytest.raises(ZeroDivisionError):
        m.divide(1, 0)


def test_math_utils_fib_normal_ok(repo: Path) -> None:
    m = _load_math_utils(repo)
    assert m.fib(10) == 55


def test_math_utils_fib_negative_current_bug(repo: Path) -> None:
    """缺陷记录:当前对负数返回负数本身;目标行为为返回 -1。"""
    m = _load_math_utils(repo)
    assert m.fib(-5) == -5
