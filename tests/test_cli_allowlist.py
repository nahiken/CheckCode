"""M15 CLI 接线测试:装配层白名单必须真的传到闭环(接线点纪律)。

M13 审计教训:算法写对但**接线点漏了**(Planner 直接 build_agent),能力等于没启用。
这里对 `_run_loop` 做同类守卫:构造 kwargs 必须含 asm 解析出的 allowed_local_tools。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.config import LLMConfig
from cli.main import _build_parser, _run_loop


class _FakeReport:
    branch = "sess-fake"

    def render(self) -> str:
        return "(fake report)"


class _FakeLoop:
    """替身 VerifiedLoop:只记录构造 kwargs,run 返回假报告。"""

    captured: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _FakeLoop.captured = kwargs

    async def run(self, repo: Path, task: str) -> _FakeReport:
        return _FakeReport()


@pytest.fixture()
def _patch_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 whycode.toml 装配出的 skill 白名单固定为 {read_file},并替换 VerifiedLoop。"""
    import assembly.build as build_mod
    import assembly.skills as skills_mod
    import cli.main as cli_mod

    def fake_assemble(repo: Path, **kwargs: Any) -> skills_mod.SkillSelection:
        return skills_mod.SkillSelection(
            skills=[], system_prompt_extra="", allowed_local_tools={"read_file"}
        )

    monkeypatch.setattr(build_mod, "assemble_skills", fake_assemble)
    # cli.main 在模块级 `from verify.loop import VerifiedLoop`,故补丁要打在 cli.main 上
    monkeypatch.setattr(cli_mod, "VerifiedLoop", _FakeLoop)


async def test_run_loop_threads_allowlist_to_verified_loop(
    demo_repo: Path, _patch_loop: None
) -> None:
    await _run_loop(
        LLMConfig(base_url="http://test", api_key="k", model="m"),
        demo_repo,
        "读一下",
        strict=False,
        auto_approve=True,
        max_fix_rounds=1,
        test_command="python -c pass",
        config_path=None,
    )
    assert _FakeLoop.captured["allowed_local_tools"] == {"read_file"}


def test_parser_accepts_skills_allowlist() -> None:
    """--skills 可重复给定,汇集为列表(未给 → None = 不限制)。"""
    parser = _build_parser()
    args = parser.parse_args(["run", "--task", "x", "--skills", "editor", "--skills", "audit"])
    assert args.skills == ["editor", "audit"]
    assert parser.parse_args(["run", "--task", "x"]).skills is None
