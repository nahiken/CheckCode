"""M2 单元测试:审计日志 JSONL 落盘 + 读回。"""

from __future__ import annotations

from pathlib import Path

from hooks.audit import AuditLog
from hooks.base import ToolAuditEvent


def test_audit_append_and_records(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    ev = ToolAuditEvent(
        tool_name="write_file",
        args={"path": "a.py"},
        decision="permit",
        layer="plan",
        approved=False,
        granted=True,
        duration_ms=1.2,
        is_error=False,
    )
    log.append(ev)
    log.append(ToolAuditEvent(tool_name="run_command", args={"command": "git status"}, decision="ask"))

    records = log.records()
    assert len(records) == 2
    assert records[0].tool_name == "write_file"
    assert records[0].decision == "permit"
    assert records[0].approved is False
    assert records[0].granted is True
    assert records[1].tool_name == "run_command"
    assert records[1].decision == "ask"


def test_audit_missing_file_returns_empty(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "nope.jsonl")
    assert log.records() == []


def test_audit_creates_parent_dirs(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "deep" / "nested" / "audit.jsonl")
    log.append(ToolAuditEvent(tool_name="read_file"))
    assert log.path.exists()
    assert log.path.parent.is_dir()
