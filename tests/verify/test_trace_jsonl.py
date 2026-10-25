"""M12 单元测试:Trace JSONL 导出 + 运行摘要 + run_id 过滤。

覆盖:to_jsonl 每行可独立解析、run_summary 计数与 fixture 一致、build_trace(run_id=)
只保留该次运行(header 除外)、旧文件(无 run_id)过滤不误伤。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.types import ToolCall
from store.models import HeaderRecord, MessageRecord, PhaseRecord, ToolCallRecord
from store.session import SessionStore
from verify.trace import build_trace, load_session_trace, run_summary, to_jsonl

RUN1 = "run-aaaa1111"
RUN2 = "run-bbbb2222"


def _records() -> list[HeaderRecord | MessageRecord | ToolCallRecord | PhaseRecord]:
    """两次运行(run-1 / run-2)共用同一会话文件,各带若干记录。"""
    return [
        HeaderRecord(
            session_id="sess-tr", repo="/tmp/r", task="修 divide", branch="sess-tr", ts=1000.0
        ),
        # --- 第一次运行 ---
        MessageRecord(role="user", content="修 divide", ts=1000.1, run_id=RUN1),
        MessageRecord(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path":"m.py"}')],
            ts=1000.5,
            run_id=RUN1,
        ),
        ToolCallRecord(
            tool_call_id="c1",
            tool_name="read_file",
            args={"path": "m.py"},
            decision="permit",
            layer="mode",
            granted=True,
            is_error=False,
            result="def divide...",
            ts=1001.0,
            run_id=RUN1,
        ),
        MessageRecord(role="tool", content="def divide...", tool_call_id="c1", ts=1001.0, run_id=RUN1),
        MessageRecord(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c2", name="write_file", arguments='{"path":"m.py"}')],
            ts=1001.2,
            run_id=RUN1,
        ),
        ToolCallRecord(
            tool_call_id="c2",
            tool_name="write_file",
            args={"path": "m.py"},
            decision="ask",
            layer="mode",
            asked=True,
            approved=False,
            granted=False,
            is_error=True,
            result="操作被用户拒绝",
            ts=1001.3,
            run_id=RUN1,
        ),
        PhaseRecord(phase="run", data={"run_id": RUN1}, ts=1001.0, run_id=RUN1),
        PhaseRecord(
            phase="verify", data={"round": 1, "passed": True}, ts=1002.0, run_id=RUN1
        ),
        PhaseRecord(phase="done", data={"passed": True}, ts=1002.5, run_id=RUN1),
        # --- 第二次运行(同一会话 resume)---
        PhaseRecord(phase="run", data={"run_id": RUN2}, ts=2000.0, run_id=RUN2),
        MessageRecord(role="assistant", content="完成", ts=2001.0, run_id=RUN2),
        PhaseRecord(
            phase="verify", data={"round": 1, "passed": False}, ts=2002.0, run_id=RUN2
        ),
        PhaseRecord(phase="done", data={"passed": False}, ts=2002.5, run_id=RUN2),
    ]


# === to_jsonl ===

def test_to_jsonl_each_line_parsable() -> None:
    entries = build_trace(_records())
    text = to_jsonl(entries)
    lines = [ln for ln in text.splitlines() if ln]
    assert lines, "应产出非空 JSONL"
    parsed = [json.loads(ln) for ln in lines]
    assert all("kind" in p for p in parsed)
    # 每行独立可解析 + 行数 == 条目数
    assert len(parsed) == len(entries)


def test_to_jsonl_empty() -> None:
    assert to_jsonl([]) == ""


def test_to_jsonl_ends_with_newline() -> None:
    text = to_jsonl(build_trace(_records()))
    assert text.endswith("\n")


# === run_summary ===

def test_run_summary_counts_match_fixture() -> None:
    """摘要计数与 fixture 一致(仅 RUN1:1 read + 1 write)。"""
    entries = build_trace(_records(), run_id=RUN1)
    s = run_summary(entries)
    assert s["run_id"] == RUN1
    assert s["session_id"] == "sess-tr"
    assert s["tool_calls"] == {"read_file": 1, "write_file": 1}
    assert s["total_tool_calls"] == 2
    assert s["granted"] == 1
    assert s["denied"] == 1
    assert s["asked"] == 1
    assert s["messages"] == 4  # user + assistant(tool_calls) + tool + assistant(tool_calls)
    assert "verify" in s["phases"] and "done" in s["phases"]
    assert s["passed"] is True
    assert s["duration_s"] is not None


def test_run_summary_second_run_not_passed() -> None:
    """第二次运行 verify 失败 → passed=False(保守判定,看 verify detail)。"""
    s = run_summary(build_trace(_records(), run_id=RUN2))
    assert s["run_id"] == RUN2
    assert s["total_tool_calls"] == 0
    assert s["passed"] is False


def test_run_summary_whole_session_includes_both_runs() -> None:
    """不过滤 → 全会话累计(两次 run 的工具调用相加)。"""
    s = run_summary(build_trace(_records()))
    assert s["total_tool_calls"] == 2
    assert s["passed"] is False  # 含 RUN2 的失败 verify → 保守判为未通过


# === run_id 过滤 ===

def test_build_trace_run_id_filter_keeps_header() -> None:
    """过滤后:仅该次运行的记录 + header(会话级上下文总保留)。"""
    entries = build_trace(_records(), run_id=RUN1)
    tool_entries = [e for e in entries if e.kind == "tool_call" and e.data.get("tool_name")]
    assert {e.data["tool_name"] for e in tool_entries} == {"read_file", "write_file"}
    # header 行仍在(且带 run_id)
    header = next(e for e in entries if e.kind == "header")
    assert header.data["session_id"] == "sess-tr"


def test_header_run_id_reflects_filtered_run() -> None:
    """B1 回归:按第二次 run 过滤时,header 行的 run_id 必须是**该次**,
    而不是存储时被写死的首个 run_id。

    构造真实落盘顺序:header 由 run-1 的 store 写入(故记录里 header.run_id == RUN1),
    再用 run_id=RUN2 过滤 —— 权威来源是 phase="run" 的 detail,不是 header 字段。
    """
    records = _records()
    # 模拟生产:header 的 run_id 被首次运行的 store 写死为 RUN1
    for r in records:
        if isinstance(r, HeaderRecord):
            r.run_id = RUN1
            r.ts = 1000.0
    entries = build_trace(records, run_id=RUN2)
    header = next(e for e in entries if e.kind == "header")
    assert header.data["run_id"] == RUN2
    assert run_summary(entries)["run_id"] == RUN2


def test_run_summary_run_id_without_filter_is_latest_run() -> None:
    """不过滤时,摘要 run_id 取**最近一次**运行(而非 header 里写死的首个)。"""
    records = _records()
    for r in records:
        if isinstance(r, HeaderRecord):
            r.run_id = RUN1
            r.ts = 1000.0
    assert run_summary(build_trace(records))["run_id"] == RUN2


def test_run_summary_passed_uses_done_phase_authority() -> None:
    """M2 回归:resume 预验证失败、无后续 verify phase 时不得误判为通过。

    done phase 的 detail.passed 是 loop 落盘的权威结论,摘要必须采信它,
    而不是靠「有 done 且无失败 verify」的启发式。
    """
    records = [
        HeaderRecord(session_id="s", repo="/r", task="t", branch="b", ts=1.0),
        PhaseRecord(phase="resume", data={"task": "t"}, ts=1.1),
        PhaseRecord(phase="run", data={"run_id": "run-x"}, ts=1.2),
        PhaseRecord(phase="resume_verify", data={"passed": False, "exit_code": 1}, ts=1.3),
        PhaseRecord(phase="done", data={"passed": False}, ts=1.4),
    ]
    assert run_summary(build_trace(records))["passed"] is False


def test_run_summary_absent_done_phase_is_not_passed() -> None:
    """无 done phase(运行中/被拒)→ 不算通过。"""
    records = [
        HeaderRecord(session_id="s", repo="/r", task="t", branch="b", ts=1.0),
        PhaseRecord(phase="run", data={"run_id": "run-x"}, ts=1.1),
    ]
    assert run_summary(build_trace(records))["passed"] is False


def test_build_trace_no_filter_keeps_all() -> None:
    entries = build_trace(_records())
    tool_entries = [e for e in entries if e.kind == "tool_call" and e.data.get("tool_name")]
    assert len(tool_entries) == 2  # RUN1 的两个


def test_build_trace_run_id_filter_excludes_other_run() -> None:
    """RUN2 没有工具调用 → 过滤后无 tool_call 行。"""
    entries = build_trace(_records(), run_id=RUN2)
    tool_entries = [e for e in entries if e.kind == "tool_call" and e.data.get("tool_name")]
    assert tool_entries == []


def test_load_session_trace_run_id_filter(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    store = SessionStore(path)
    for r in _records():
        store._append(r)
    entries = load_session_trace(path, run_id=RUN1)
    assert any(e.kind == "header" for e in entries)
    assert not any(
        e.kind == "message" and "完成" in e.text for e in entries
    )  # RUN2 的完成消息被滤掉


def test_legacy_records_without_run_id_not_filtered_out_when_no_filter() -> None:
    """旧文件(全 run_id="")不过滤时照常出全量。"""
    legacy = [
        HeaderRecord(session_id="s", repo="/r", task="t", branch="b", ts=1.0),
        MessageRecord(role="user", content="hi", ts=2.0),
    ]
    entries = build_trace(legacy)
    assert any(e.kind == "message" for e in entries)
