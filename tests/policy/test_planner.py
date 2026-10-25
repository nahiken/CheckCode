"""M2 单元测试:只读 Planner(严格模式第一步)+ 计划解析。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent.config import LLMConfig
from policy.planner import build_plan, parse_plan_from_text
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block

# === parse_plan_from_text ===

def test_parse_plan_files_and_commands() -> None:
    text = "[计划]\nfile: math_utils.py\nfile: tests/test_math_utils.py\ncmd: python -m pytest\n[/计划]"
    plan = parse_plan_from_text(text)
    assert plan.files == ["math_utils.py", "tests/test_math_utils.py"]
    assert plan.commands == ["python -m pytest"]
    assert plan.description


def test_parse_plan_without_marker_returns_empty() -> None:
    plan = parse_plan_from_text("没有标记的计划文本")
    assert plan.files == []
    assert plan.commands == []


def test_parse_plan_ignores_garbage_lines() -> None:
    text = "[计划]\nfile: a.py\n随便一行\ncmd: git status\n[/计划]"
    plan = parse_plan_from_text(text)
    assert plan.files == ["a.py"]
    assert plan.commands == ["git status"]


def test_plan_allows_command_prefix_match() -> None:
    from policy.models import Plan

    plan = Plan(files=["a.py"], commands=["python -m pytest"])
    assert plan.allows_command("python -m pytest -q") is True
    assert plan.allows_command("git status") is False
    assert plan.allows_command("") is False


# === build_plan 端到端(只读 Planner) ===

def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


async def test_build_plan_reads_then_outputs_plan(demo_repo: Path) -> None:
    """Planner 先读文件再输出 [计划] → 解析为 Plan,且不写任何文件。"""
    r1 = tool_delta(0, id_="c1", name="read_file", args='{"path":"math_utils.py"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("[计划]\nfile: math_utils.py\ncmd: python -m pytest\n[/计划]") + finish() + usage_block()

    request_iter = iter([r1, r2])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    plan = await build_plan(
        _config(),
        demo_repo,
        "修复 math_utils 的缺陷",
        audit_path=demo_repo / ".whycode" / "planner-audit.jsonl",
        transport=httpx.MockTransport(handler),
    )

    assert plan.files == ["math_utils.py"]
    assert plan.commands == ["python -m pytest"]
    # 文件未被修改(Planner 只读)
    content = (demo_repo / "math_utils.py").read_text(encoding="utf-8")
    assert "def add" in content and "ZeroDivisionError" in content
    # 计划审计落盘
    records = (demo_repo / ".whycode" / "planner-audit.jsonl").read_text(encoding="utf-8")
    assert "read_file" in records


async def test_build_plan_write_attempt_denied(demo_repo: Path) -> None:
    """Planner 若尝试写文件 → 被权限层拒绝(只读调度)。"""
    r1 = tool_delta(0, id_="c1", name="write_file", args='{"path":"math_utils.py","content":"evil"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("[计划]\nfile: math_utils.py\n[/计划]") + finish() + usage_block()

    request_iter = iter([r1, r2])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    plan = await build_plan(
        _config(),
        demo_repo,
        "改文件",
        audit_path=demo_repo / ".whycode" / "planner-audit.jsonl",
        transport=httpx.MockTransport(handler),
    )
    # 写被拒 → 文件未变
    content = (demo_repo / "math_utils.py").read_text(encoding="utf-8")
    assert "evil" not in content
    assert plan.files == ["math_utils.py"]


async def test_build_plan_empty_output_raises(demo_repo: Path) -> None:
    """Planner 输出空计划 → 报错(不允许静默通过)。"""
    r1 = text_delta("我没有计划") + finish() + usage_block()

    request_iter = iter([r1])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    with pytest.raises(ValueError, match="Planner"):
        await build_plan(
            _config(),
            demo_repo,
            "随便",
            audit_path=demo_repo / ".whycode" / "planner-audit.jsonl",
            transport=httpx.MockTransport(handler),
        )
