"""assembly/build.default_user_dir 单测:用户级技能根的推导。

背景(C 包接线):三级加载的「用户级」此前在生产路径恒为 None —— 只有测试覆盖。
接线后由生产调用点显式传 `default_user_dir()`,本文件锁住它的推导规则。
"""

from __future__ import annotations

from pathlib import Path

from assembly.build import USER_DIR_ENV, default_user_dir


def test_default_is_home_dot_whycode(monkeypatch) -> None:
    """无环境变量 → ~/.whycode(与 skills.loader 的 <root>/skills/<name> 约定配套)。"""
    monkeypatch.delenv(USER_DIR_ENV, raising=False)
    assert default_user_dir() == Path.home() / ".whycode"


def test_env_override_wins(monkeypatch, tmp_path: Path) -> None:
    """WHYCODE_USER_DIR 存在 → 用它(测试/多用户隔离的显式旋钮)。"""
    monkeypatch.setenv(USER_DIR_ENV, str(tmp_path / "custom"))
    assert default_user_dir() == tmp_path / "custom"


def test_blank_env_falls_back_to_home(monkeypatch) -> None:
    """空白环境变量视为未设置 —— 不能让 `WHYCODE_USER_DIR=""` 把根变成当前目录。

    Path("") 是 Path("."),那会把仓库根当成用户级技能根,静默改变装配结果。
    """
    monkeypatch.setenv(USER_DIR_ENV, "   ")
    assert default_user_dir() == Path.home() / ".whycode"
