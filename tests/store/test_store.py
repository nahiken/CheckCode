"""store/ 单元测试:JSONL 落盘/读回/损坏容忍/恢复推导。

纯存储层,不依赖 git/LLM。恢复重放边界由 store/effect.py 的 is_read_only 定义。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.types import AgentMessage, ToolCall

# 副作用判定器(恢复边界唯一来源)
from store.effect import is_read_only
from store.models import (
    HeaderRecord,
    MessageRecord,
    StoreRecord,
    ToolCallRecord,
)
from store.session import SessionStore, recover_events


def test_s1_append_and_read_roundtrip(tmp_path: Path) -> None:
    """header + message 追加 → 逐行读回,顺序保持。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_header(
        HeaderRecord(session_id="s1", repo="/tmp/repo", task="修复 bug", branch="feat/abc", mode="normal")
    )
    store.record_message(MessageRecord(role="user", content="修复 divide"))

    assert store.path.exists()
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 两行
    parsed = json.loads(lines[0])
    assert parsed["type"] == "header"
    assert parsed["session_id"] == "s1"
    assert parsed["task"] == "修复 bug"
    parsed1 = json.loads(lines[1])
    assert parsed1["type"] == "message"
    assert parsed1["content"] == "修复 divide"


def test_s2_readback_returns_typed_records(tmp_path: Path) -> None:
    """读回返回类型化记录列表。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_header(
        HeaderRecord(session_id="s2", repo="/r", task="t", branch="feat/x", mode="strict")
    )
    store.record_message(MessageRecord(role="assistant", content="ok", reasoning_content="想一下"))

    records = store.records()
    assert [type(r) for r in records] == [HeaderRecord, MessageRecord]
    header, msg = records
    assert isinstance(header, HeaderRecord) and isinstance(msg, MessageRecord)
    assert header.session_id == "s2"
    assert header.mode == "strict"
    assert msg.reasoning_content == "想一下"


def test_s3_continue_session_appends_after_readback(tmp_path: Path) -> None:
    """模拟跨进程恢复:先写一部分,读回,继续追加(不覆盖旧记录)。"""
    path = tmp_path / "s.jsonl"
    store1 = SessionStore(path)
    store1.record_header(HeaderRecord(session_id="s3", repo="/r", task="t", branch="feat/y", mode="normal"))
    store1.record_message(MessageRecord(role="user", content="第一段"))

    store2 = SessionStore(path)
    store2.continue_session()  # 复用既有文件(进程重启后)
    store2.record_message(MessageRecord(role="assistant", content="第二段"))

    records = store2.records()
    assert [r.role for r in records if isinstance(r, MessageRecord)] == ["user", "assistant"]


def test_s3b_read_path_creates_no_directory(tmp_path: Path) -> None:
    """M10:目录**延迟创建** —— 只读路径(构造/continue_session/records)不产生任何文件。

    这是「计划被拒绝 → 零 session 文件」的底层保证:若读取就 mkdir,拒绝路径
    仍会留下空目录(此前实现如此,审计判为与规格不符)。
    """
    path = tmp_path / "nested" / "sess-x.jsonl"
    store = SessionStore(path)
    store.continue_session()
    assert store.records() == []  # 文件不存在 → 空

    assert not path.parent.exists(), "只读路径不应创建 session 目录"
    assert not path.exists()

    # 首次真正写入才创建目录 + 文件
    store.record_message(MessageRecord(role="user", content="hi"))
    assert path.exists()


def test_s4_corrupt_line_tolerated(tmp_path: Path) -> None:
    """损坏行(非 JSON)跳过并计数。"""
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type":"header","session_id":"s4","repo":"/r","task":"t","branch":"b"}\n'
        "{broken\n"
        '{"type":"message","role":"user","content":"x"}\n',
        encoding="utf-8",
    )
    store = SessionStore(path)
    records, skipped = store.read_with_skips()
    assert [r.type for r in records] == ["header", "message"]
    assert skipped == 1


def test_s5_tool_call_record_roundtrip(tmp_path: Path) -> None:
    """工具调用记录:参数 + 判定层 + 审批边界可追溯。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_tool_call(
        ToolCallRecord(
            tool_call_id="call_1",
            tool_name="write_file",
            args={"path": "a.py", "content": "x"},
            mode="normal",
            decision="permit",
            layer="plan",
            asked=True,
            approved=True,
            granted=True,
            overridden=False,
            result="ok",
            is_error=False,
        )
    )
    (rec,) = store.records()
    assert isinstance(rec, ToolCallRecord)
    assert rec.tool_name == "write_file"
    assert rec.decision == "permit"
    assert rec.asked and rec.approved and rec.granted
    assert rec.args == {"path": "a.py", "content": "x"}


def test_s6_empty_or_missing(tmp_path: Path) -> None:
    """空 store / 不存在文件 → 空记录。"""
    store = SessionStore(tmp_path / "none.jsonl")
    assert store.records() == []
    assert store.read_with_skips() == ([], 0)

    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert SessionStore(tmp_path / "empty.jsonl").records() == []


def test_s7_overridden_flag_roundtrip(tmp_path: Path) -> None:
    """覆写标记可解析(恢复语义:对应 assistant tool_calls 需从模型上下文排除)。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_tool_call(
        ToolCallRecord(
            tool_call_id="call_x", tool_name="write_file", args={}, overridden=True
        )
    )
    (rec,) = store.records()
    assert isinstance(rec, ToolCallRecord)
    assert rec.overridden is True


def test_s8_truncated_output_landed(tmp_path: Path) -> None:
    """超长工具输出落盘裁剪:内容 ≤ 上限 + 截断标记。"""
    store = SessionStore(tmp_path / "s.jsonl", max_output_chars=100)
    store.record_tool_call(
        ToolCallRecord(
            tool_call_id="call_t",
            tool_name="run_command",
            args={},
            result="x" * 5000,
        )
    )
    (rec,) = store.records()
    assert isinstance(rec, ToolCallRecord)
    assert len(rec.result) <= 100  # 含截断后缀仍不超上限
    assert rec.result.startswith("x" * 10)
    assert rec.truncated is True


def test_e1_read_tools_are_read_only() -> None:
    assert is_read_only("read_file") is True
    assert is_read_only("list_dir") is True


def test_e2_write_tools_not_read_only() -> None:
    assert is_read_only("write_file") is False


def test_e3_run_command_not_read_only() -> None:
    assert is_read_only("run_command") is False


def test_r1_all_resumed_no_replay() -> None:
    """全部已完成 → 不需要恢复,干净重放。"""
    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        ToolCallRecord(tool_call_id="c1", tool_name="read_file", args={"path": "a"}, result="内容", is_error=False),
        MessageRecord(role="tool", content="内容", tool_call_id="c1"),
        ToolCallRecord(tool_call_id="c2", tool_name="write_file", args={}, result="ok", is_error=False),
        MessageRecord(role="tool", content="ok", tool_call_id="c2"),
    ]
    assert recover_events(events) == []


def test_r2_pending_read_recovered() -> None:
    """pending 读:tool result 未回灌 → 作为消息喂给模型。"""
    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        ToolCallRecord(tool_call_id="c1", tool_name="read_file", args={"path": "a"}, result="内容"),
    ]
    recovered = recover_events(events)
    assert len(recovered) == 1
    (rec,) = recovered
    assert rec.kind == "message"
    assert rec.message is not None
    assert rec.message.role == "tool"
    assert rec.message.tool_call_id == "c1"
    assert rec.message.content == "内容"


def test_r3_pending_write_skipped() -> None:
    """pending 写:副作用已落盘但未回灌 → 跳过,回灌合成跳过提示。"""
    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        ToolCallRecord(tool_call_id="c1", tool_name="write_file", args={"path": "a", "content": "x"}, result="ok"),
    ]
    recovered = recover_events(events)
    assert len(recovered) == 1
    (rec,) = recovered
    assert rec.kind == "skip"
    assert rec.message is not None
    assert rec.message.role == "tool"
    assert rec.message.tool_call_id == "c1"
    assert "已跳过" in rec.message.content


def test_r4_overridden_excluded_from_replay() -> None:
    """overridden=True 记录:模型上下文中对应 assistant tool_calls 被排除(防重复回灌)。"""
    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        MessageRecord(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="write_file", arguments="{}")],
        ),
        ToolCallRecord(tool_call_id="c1", tool_name="write_file", args={}, overridden=True),
    ]
    assert recover_events(events) == []


def test_r5_composite_restore_without_replay() -> None:
    """复合:已完成写(读+写+读完整)恢复时模型上下文不含重放,后续正常继续。"""
    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        MessageRecord(role="assistant", content="", tool_calls=[ToolCall(id="r1", name="read_file", arguments="{}")]),
        ToolCallRecord(tool_call_id="r1", tool_name="read_file", args={}, result="内容"),
        MessageRecord(role="tool", content="内容", tool_call_id="r1"),
        MessageRecord(role="assistant", content="", tool_calls=[ToolCall(id="w1", name="write_file", arguments="{}")]),
        ToolCallRecord(tool_call_id="w1", tool_name="write_file", args={}, result="ok"),
        MessageRecord(role="tool", content="ok", tool_call_id="w1"),
    ]
    assert recover_events(events) == []


def test_recover_events_returns_replayable_history() -> None:
    """从已持久化事件还原完整可重放历史(含已完成写,供模型上下文)。"""
    from store.session import build_resume_seed

    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        MessageRecord(role="assistant", content="", tool_calls=[ToolCall(id="w1", name="write_file", arguments="{}")]),
        ToolCallRecord(tool_call_id="w1", tool_name="write_file", args={}, result="ok", is_error=False),
        MessageRecord(role="tool", content="ok", tool_call_id="w1"),
    ]
    history = build_resume_seed(events)
    assert [m.role for m in history] == ["user", "assistant", "tool"]
    assert isinstance(history[1], AgentMessage)
    assert history[1].tool_calls[0].name == "write_file"
    # 可序列化为 OpenAI 消息格式
    from agent.types import to_openai_messages

    oai = to_openai_messages(history)
    assert oai[1]["role"] == "assistant"
    assert oai[2]["role"] == "tool"


def test_build_resume_seed_overridden_excluded() -> None:
    """恢复上下文剔除 overridden 的 assistant tool_calls(防悬空 tool_calls)。"""
    from store.session import build_resume_seed

    events: list[StoreRecord] = [
        MessageRecord(role="user", content="任务"),
        MessageRecord(role="assistant", content="", tool_calls=[ToolCall(id="w1", name="write_file", arguments="{}")]),
        ToolCallRecord(tool_call_id="w1", tool_name="write_file", args={}, result="ok", overridden=True),
    ]
    history = build_resume_seed(events)
    # assistant 的 w1 被剔除(overridden),不再出现在模型上下文
    assert [m.role for m in history] == ["user"]
