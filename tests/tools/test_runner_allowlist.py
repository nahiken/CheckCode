"""M15 单元测试:ToolRunner 工具白名单(端到端到「文件未落盘」)。

承重断言:`tools_allow` 收窄到只读工具时,write_file 不仅被判 deny,
而且**文件真的没被写**(证明拒绝发生在执行之前)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hooks.audit import AuditLog
from policy.evaluator import PolicyEvaluator
from policy.models import Plan
from tools.registry import build_tools, tools_spec_from
from tools.runner import ToolRunner


def _runner(tmp_path: Path, **kwargs: Any) -> ToolRunner:
    """按生产形态构造:tools 走 build_tools,evaluator 由 runner 自建。"""
    tools = kwargs.pop("tools", None) or build_tools(tmp_path)
    return ToolRunner(
        tmp_path,
        tools=tools,
        evaluator=PolicyEvaluator(
            tmp_path, tools_spec=tools_spec_from(tools), **kwargs
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )


async def test_allowed_tool_still_works(tmp_path: Path) -> None:
    """白名单内的工具正常执行(收窄不等于禁用)。"""
    (tmp_path / "a.py").write_text("hello", encoding="utf-8")
    r = _runner(tmp_path, tools_allow={"read_file"})
    result = await r.dispatch("read_file", {"path": "a.py"})
    assert not result.is_error
    assert "hello" in result.content


async def test_denied_tool_writes_nothing(tmp_path: Path) -> None:
    """白名单外的写工具:deny 且**文件未落盘**(拒绝先于执行)。"""
    r = _runner(tmp_path, tools_allow={"read_file"})
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert "skill" in result.content
    assert not (tmp_path / "a.py").exists()


async def test_audit_records_skill_layer(tmp_path: Path) -> None:
    """审计里 layer 落在 skill 层(可定位「被哪一层拦的」)。"""
    r = _runner(tmp_path, tools_allow={"read_file"})
    await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    records = r.audit.records()
    assert records[-1].layer == "skill"
    assert records[-1].decision == "deny"
    assert records[-1].granted is False


async def test_denied_result_carries_skill_layer_to_store(tmp_path: Path) -> None:
    """拒绝现场随 AgentResult.audit 回传(M4 落盘用),layer=skill。"""
    r = _runner(tmp_path, tools_allow={"read_file"})
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.audit is not None
    assert result.audit["layer"] == "skill"


async def test_sandbox_boundary_unaffected_by_allowlist(tmp_path: Path) -> None:
    """白名单内的工具仍受沙箱约束(层序:skill 在沙箱之后,收窄不可绕过硬边界)。"""
    r = _runner(tmp_path, tools_allow={"write_file"}, plan=Plan(files=["../outside.txt"]))
    result = await r.dispatch("write_file", {"path": "../outside.txt", "content": "x"})
    assert result.is_error
    assert result.audit is not None
    assert result.audit["layer"] == "sandbox"


async def test_none_leaves_behavior_unchanged(tmp_path: Path) -> None:
    """tools_allow=None → 与不传该参数完全同构(兼容纪律)。"""
    r_none = _runner(tmp_path, tools_allow=None)
    r_base = _runner(tmp_path)

    def _audit_without_timing(result: Any) -> dict[str, Any]:
        # duration_ms 是墙钟测量,不参与同构比较
        audit = dict(result.audit or {})
        audit.pop("duration_ms", None)
        return audit

    for name, args in (
        ("read_file", {"path": "a.py"}),
        ("write_file", {"path": "a.py", "content": "x"}),
    ):
        a = await r_none.dispatch(name, args)
        b = await r_base.dispatch(name, args)
        assert a.is_error == b.is_error
        assert _audit_without_timing(a) == _audit_without_timing(b)
