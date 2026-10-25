"""M0 配置层测试:pyproject.toml 可通过 tomllib 解析,关键字段完整。

覆盖:
- 元数据(name / version / requires-python / description)
- 运行时依赖(pydantic / httpx)
- dev 依赖(pytest / ruff / mypy 等)
- console script 入口(mewcode -> cli.main:main)
- pytest / ruff / mypy 工具配置
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _load() -> dict[str, Any]:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def test_pyproject_parseable() -> None:
    """pyproject.toml 是合法的 TOML 且包含 project 段。"""
    data = _load()
    assert "project" in data
    assert data["project"]["name"] == "whycode"


def test_pyproject_metadata_fields() -> None:
    p = _load()["project"]
    assert p["version"] == "0.6.0"  # 0.6.0:双协议(Anthropic) + Textual 终端工作台
    assert p["requires-python"].startswith(">=")
    assert p["description"].strip()
    assert p["readme"] == "README.md"


def test_pyproject_runtime_dependencies() -> None:
    deps = _load()["project"]["dependencies"]
    assert any(d.startswith("pydantic") for d in deps)
    assert any(d.startswith("httpx") for d in deps)


def test_pyproject_dev_dependencies() -> None:
    dev = _load()["project"]["optional-dependencies"]["dev"]
    for tool in ("pytest", "pytest-asyncio", "pytest-cov", "ruff", "mypy"):
        assert any(d.startswith(tool) for d in dev), tool


def test_pyproject_console_script() -> None:
    scripts = _load()["project"]["scripts"]
    assert scripts["mewcode"] == "cli.main:main"


def test_pyproject_pytest_testpaths() -> None:
    ini = _load()["tool"]["pytest"]["ini_options"]
    assert "tests" in ini["testpaths"]
    # demo_repo 是独立仓库,不纳入主套件
    assert "tests/fixtures" in ini["norecursedirs"]


def test_pyproject_mypy_strict() -> None:
    assert _load()["tool"]["mypy"]["strict"] is True


def test_pyproject_ruff_config() -> None:
    cfg = _load()["tool"]["ruff"]
    assert cfg["target-version"] == "py311"
    for code in ("E", "F", "I", "UP", "B", "SIM"):
        assert code in cfg["lint"]["select"], code
