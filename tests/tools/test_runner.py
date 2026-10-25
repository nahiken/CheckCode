"""M2 集成测试:ToolRunner 统一安全入口。

覆盖:参数 schema 校验(空对象缺必填字段)、越权拦截、路径逃逸、
高危命令、Ask 审批(批准/拒绝/无审批器)、严格模式、规则覆盖、审计落盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hooks.audit import AuditLog
from policy.models import Plan, Rule
from tools.runner import ToolRunner


def _runner(tmp_path: Path, **kwargs: Any) -> ToolRunner:
    return ToolRunner(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"), **kwargs)


# === 参数 schema 校验(M1 遗留) ===

async def test_unknown_tool_rejected(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = await r.dispatch("nope", {})
    assert result.is_error
    assert "未知工具" in result.content


async def test_schema_validation_rejects_missing_required(tmp_path: Path) -> None:
    """M1 review 遗留:空对象缺必填字段必须被拒(此前只校验 JSON 可解析)。"""
    r = _runner(tmp_path)
    result = await r.dispatch("read_file", {})
    assert result.is_error
    assert "参数校验失败" in result.content
    assert "path" in result.content


async def test_schema_validation_rejects_wrong_type(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = await r.dispatch("write_file", {"path": "a.py", "content": 123})
    assert result.is_error
    assert "参数校验失败" in result.content


# === 越权拦截 / 路径逃逸 ===

async def test_unauthorized_write_denied_without_gate(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert "审批" in result.content or "越权" in result.content
    # 文件未被写入
    assert not (tmp_path / "a.py").exists()


async def test_path_escape_blocked_by_sandbox(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    r = _runner(tmp_path)
    result = await r.dispatch("write_file", {"path": "../outside.txt", "content": "x"})
    assert result.is_error
    assert "越界" in result.content
    assert not outside.exists()


async def test_high_risk_command_blocked(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = await r.dispatch("run_command", {"command": "rm -rf /"})
    assert result.is_error
    assert "黑名单" in result.content


# === Ask 审批 ===

async def test_ask_approved(tmp_path: Path) -> None:
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        return True

    r = _runner(tmp_path, approval_gate=gate)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "hello"})
    assert not result.is_error
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "hello"
    rec = r.audit.records()[-1]
    assert rec.decision == "ask"
    assert rec.approved is True
    assert rec.granted is True


async def test_ask_denied(tmp_path: Path) -> None:
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        return False

    r = _runner(tmp_path, approval_gate=gate)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert "拒绝" in result.content
    assert not (tmp_path / "a.py").exists()
    rec = r.audit.records()[-1]
    assert rec.approved is False
    assert rec.granted is False


async def test_approval_gate_exception_treated_as_deny(tmp_path: Path) -> None:
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        raise RuntimeError("gate broken")

    r = _runner(tmp_path, approval_gate=gate)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert not (tmp_path / "a.py").exists()


# === 计划白名单 + 严格模式 ===

async def test_plan_permit_without_gate(tmp_path: Path) -> None:
    """normal 模式:命中计划 → 直接放行,无需审批。"""
    r = _runner(tmp_path, plan=Plan(files=["src/a.py"]))
    result = await r.dispatch("write_file", {"path": "src/a.py", "content": "x"})
    assert not result.is_error
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "x"


async def test_strict_mode_plan_write_still_asks(tmp_path: Path) -> None:
    """严格模式:命中计划仍需审批。"""
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        return True

    r = _runner(tmp_path, mode="strict", plan=Plan(files=["a.py"]), approval_gate=gate)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "y"})
    assert not result.is_error
    rec = r.audit.records()[-1]
    assert rec.mode == "strict"
    assert rec.approved is True


async def test_strict_mode_plan_write_without_gate_denied(tmp_path: Path) -> None:
    r = _runner(tmp_path, mode="strict", plan=Plan(files=["a.py"]))
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert not (tmp_path / "a.py").exists()


# === 规则覆盖 ===

async def test_rule_deny_overrides_plan_permit(tmp_path: Path) -> None:
    r = _runner(
        tmp_path,
        plan=Plan(files=["a.py"]),
        rules=[Rule(tool="write_file", effect="deny")],
    )
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})
    assert result.is_error
    assert "规则拒绝" in result.content
    assert not (tmp_path / "a.py").exists()


async def test_rule_permit_bypasses_ask(tmp_path: Path) -> None:
    """规则允许 run_command(如 pytest)→ 无需审批直接执行。"""
    r = _runner(tmp_path, rules=[Rule(tool="run_command", pattern="echo", effect="permit")])
    result = await r.dispatch("run_command", {"command": "echo hi"})
    assert not result.is_error
    assert "hi" in result.content


# === 审计落盘 ===

async def test_audit_records_full_flow(tmp_path: Path) -> None:
    """两段式 Hook:每次调用前有 before 现场、收尾有 after 结论(放行与被拒都是两段)。"""
    r = _runner(tmp_path)
    await r.dispatch("read_file", {"path": "missing.py"})  # 只读放行 → 执行 → before+after
    await r.dispatch("write_file", {"path": "a.py", "content": "x"})  # denied(ask, 无 gate)
    await r.dispatch("run_command", {"command": "rm -rf /"})  # denied(blacklist)

    records = r.audit.records()
    assert len(records) == 6
    # 已执行的 read_file:before(现场,result 未定)+ after(最终结果)
    assert records[0].tool_name == "read_file"
    assert records[0].decision == "permit"
    assert records[0].asked is False
    assert records[0].granted is True  # 权限放行(≠ 已执行)
    assert records[0].executed is False  # before:还没执行
    assert records[0].duration_ms is None  # before:耗时未定
    assert records[0].is_error is None  # before:结果未定
    assert records[1].tool_name == "read_file"
    assert records[1].decision == "permit"
    assert records[1].granted is True
    assert records[1].executed is True  # after:执行路径置位
    assert records[1].duration_ms is not None
    assert records[1].is_error is True  # 文件不存在
    assert records[1].result
    # 被拒的 write_file:before(判定现场)+ after(结论)。两段都在,靠 executed 区分未执行。
    assert records[2].decision == "ask"
    assert records[2].layer == "mode"
    assert records[2].asked is False  # before:尚未进入审批
    assert records[2].granted is False
    assert records[3].decision == "ask"
    assert records[3].asked is True  # after:已问过(无 gate → 默认拒)
    assert records[3].executed is False
    # 被拒的 run_command:同样两段(blacklist 是硬边界,判定即拒)
    assert records[4].decision == "deny"
    assert records[4].executed is False
    assert records[5].decision == "deny"
    assert records[5].layer == "blacklist"


async def test_audit_granted_write_two_phase(tmp_path: Path) -> None:
    """审批放行的写操作:两次 before(判定现场 + 审批现场)+ 一次 after(最终结果)。

    三次记录是「before 在审批前触发 + 审批后补记」的结果:第一个 before 让**被拒**
    路径也有现场,第二个 before 把 `asked/approved` 落盘 —— 否则审计看不出这次调用
    问过、批过。
    """
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        return True

    r = _runner(tmp_path, approval_gate=gate)
    result = await r.dispatch("write_file", {"path": "a.py", "content": "hello"})
    assert not result.is_error

    records = r.audit.records()
    assert len(records) == 3
    first, second, after = records
    # 第一次 before:判定现场(尚未审批)
    assert first.decision == "ask"
    assert first.asked is False
    assert first.executed is False
    # 第二次 before:审批现场(问过、批过,结果未定)
    assert second.asked is True and second.approved is True
    assert second.granted is True
    assert second.executed is False
    assert second.duration_ms is None
    # after:最终结果与耗时
    assert after.granted is True
    assert after.executed is True
    assert after.duration_ms is not None
    assert after.is_error is False
    assert after.result


# === list_dir 默认参数(P3:校验后参数应用 Pydantic 默认值) ===

async def test_list_dir_default_path_permitted(tmp_path: Path) -> None:
    """list_dir 省略 path → 用默认 "."(校验后参数),不再被沙箱误判为越界。"""
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    r = _runner(tmp_path)
    result = await r.dispatch("list_dir", {})
    assert not result.is_error
    assert "a.py" in result.content
    rec = r.audit.records()[-1]
    assert rec.decision == "permit"  # 只读放行,而非 sandbox deny


# === 工具执行异常转 Observation ===

async def test_tool_exception_becomes_error_observation(tmp_path: Path) -> None:
    """自定义工具抛异常 → runner 捕获转错误结果,不冒泡。

    声明 read_only=True 以直达执行(工具分类是集成方责任,此处专注验证异常兜底)。
    """
    from tools.base import Tool
    from tools.runner import ToolRunner

    class BoomTool(Tool):
        read_only: bool = True

        def run(self, args: dict[str, Any]) -> Any:
            raise RuntimeError("boom")

    r = ToolRunner(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"), tools={"boom": BoomTool(workdir=tmp_path)})
    result = await r.dispatch("boom", {})
    assert result.is_error
    assert "执行异常" in result.content
