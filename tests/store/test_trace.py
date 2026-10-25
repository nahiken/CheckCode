"""store/trace 单元测试:会话 Trace 导出(M9 W2)。

覆盖:
- SessionStore._append 单一写入点落 ts(新记录自动带时间戳)。
- 旧文件(无 ts)仍可解析,缺 ts 记录字段为 None。
- build_trace:header/message/tool_call/phase/gap/summary 各类行的结构。
- 缺 ts 时 gap 不产生、summary 标注「无时间戳」。
- markdown / json 两种渲染可序列化。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.types import ToolCall
from store.models import HeaderRecord, MessageRecord, PhaseRecord, ToolCallRecord
from store.session import SessionStore
from verify.trace import build_trace, load_session_trace, render_markdown, to_summary_dict


def _records() -> list[HeaderRecord | MessageRecord | ToolCallRecord | PhaseRecord]:
    """构造一次完整会话的样例记录(带显式 ts,供断言确定性)。"""
    return [
        HeaderRecord(
            session_id="sess-tr1", repo="/tmp/repo", task="修复 divide",
            branch="sess-tr1", mode="normal",
            ts=1000.0,
        ),
        MessageRecord(role="user", content="修复 divide 除零", ts=1000.1),
        MessageRecord(
            role="assistant", content="", tool_calls=[ToolCall(id="c1", name="read_file", arguments='{"path":"m.py"}')],
            ts=1000.5,
        ),
        ToolCallRecord(
            tool_call_id="c1", tool_name="read_file", args={"path": "m.py"},
            decision="permit", layer="mode", granted=True, is_error=False, result="def add...",
            ts=1001.0,
        ),
        MessageRecord(role="tool", content="def add...", tool_call_id="c1", ts=1001.0),
        MessageRecord(role="assistant", content="完成修复", ts=1002.0),
        PhaseRecord(phase="done", data={"passed": True}, ts=1002.5),
    ]


def test_ts_lands_on_append(tmp_path: Path) -> None:
    """M9 W2:_append 单一写入点自动补 ts(记录 model 里 ts 默认 None)。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_message(MessageRecord(role="user", content="任务"))
    (rec,) = store.records()
    assert isinstance(rec, MessageRecord)
    assert rec.ts is not None  # 写入时补了时间戳
    assert rec.ts > 0
    # 落盘 JSON 里确实带 ts 字段
    raw = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8").strip())
    assert "ts" in raw


def test_ts_preserved_when_explicit(tmp_path: Path) -> None:
    """显式 ts 不被 _append 覆盖(记录可被测试/工具构造复现)。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_message(MessageRecord(role="user", content="x", ts=123.0))
    (rec,) = store.records()
    assert rec.ts == 123.0


def test_legacy_no_ts_records_parse(tmp_path: Path) -> None:
    """旧文件(无 ts 字段)仍可解析:ts=None、记录不丢。"""
    path = tmp_path / "legacy.jsonl"
    path.write_text(
        '{"type":"header","session_id":"s","repo":"/r","task":"t","branch":"b"}\n'
        '{"type":"message","role":"user","content":"旧任务"}\n',
        encoding="utf-8",
    )
    store = SessionStore(path)
    records = store.records()
    assert [r.type for r in records] == ["header", "message"]
    assert all(getattr(r, "ts", object()) is None for r in records)  # 缺 ts → None


def test_build_trace_structure_with_ts() -> None:
    """带 ts 记录 → Trace 含 header/message/tool_call/phase + 间隙行 + summary。"""
    entries = build_trace(_records(), session_id="sess-tr1")
    kinds = [e.kind for e in entries]
    # header 任务行
    assert kinds[0] == "header"
    assert "修复 divide" in entries[0].text
    # 工具发起(assistant tool_calls)
    assert any(e.kind == "message" and "read_file" in e.text for e in entries)
    # 工具调用行(含权限结论/审批边界)
    tools = [e for e in entries if e.kind == "tool_call"]
    assert len(tools) >= 1
    assert tools[0].data["granted"] is True
    # 阶段行
    assert any(e.kind == "phase" and e.data.get("phase") == "done" for e in entries)
    # 间隙行(相邻有 ts)→ 会话有历时
    assert any(e.kind == "gap" for e in entries)
    # summary 带会话 id + 历时
    summary = [e for e in entries if e.kind == "summary"][0]
    assert "sess-tr1" in summary.text
    assert "历时" in summary.text


def test_build_trace_legacy_no_ts_no_gaps() -> None:
    """无 ts 记录 → 无间隙行、summary 标注无时间戳、消息仍完整呈现。"""
    records = [
        HeaderRecord(session_id="old", repo="/r", task="t", branch="old"),
        MessageRecord(role="user", content="任务"),
        ToolCallRecord(tool_call_id="c1", tool_name="read_file", args={}, decision="permit", granted=True),
        MessageRecord(role="tool", content="结果", tool_call_id="c1"),
    ]
    entries = build_trace(records)
    kinds = [e.kind for e in entries]
    assert "gap" not in kinds  # 无 ts → 不产间隙
    assert kinds[0] == "header"
    # tool_call 与 tool 结果消息都呈现
    assert any(e.kind == "tool_call" for e in entries)
    assert any(e.kind == "message" and "结果" in e.text for e in entries)
    summary = [e for e in entries if e.kind == "summary"][0]
    assert "无时间戳" in summary.text


def test_tool_error_and_denied_marked() -> None:
    """错误工具调用 / 被拒工具在 Trace 中标出。"""
    records = [
        HeaderRecord(session_id="s", repo="/r", task="t", branch="s"),
        MessageRecord(role="user", content="任务"),
        ToolCallRecord(
            tool_call_id="c1", tool_name="write_file", args={"path": "a.py"},
            decision="deny", layer="plan", granted=False,
            result="权限拒绝(plan): 严格模式", is_error=True,
        ),
    ]
    entries = build_trace(records)
    tool_entry = [e for e in entries if e.kind == "tool_call"][0]
    assert tool_entry.data["granted"] is False
    assert tool_entry.data["decision"] == "deny"
    # 错误子行被加入
    assert any("错误" in e.text for e in entries)


def test_render_markdown_and_json() -> None:
    """markdown / json 两种渲染都可用且可序列化。"""
    entries = build_trace(_records())
    md = render_markdown(entries)
    assert "# 会话 Trace" in md
    assert "任务" in md
    # json 可 roundtrip
    summary = to_summary_dict(entries)
    dumped = json.dumps(summary, ensure_ascii=False)
    assert isinstance(dumped, str)
    assert summary["count"] == len(entries)


def test_load_session_trace_missing_file(tmp_path: Path) -> None:
    """session 文件不存在 → 空 Trace(不抛异常)。"""
    assert load_session_trace(tmp_path / "none.jsonl") == []
