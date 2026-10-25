"""M1 单元测试:StreamCollector 聚合 + 非法参数保护。"""

from __future__ import annotations

import pytest

from agent.stream_collector import (
    ParamError,
    StreamCollector,
    param_error_observation,
    parse_tool_arguments,
)
from agent.types import Delta, StreamChunk, ToolCallChunk, Usage


def _chunk(content: str | None = None, tool_calls: list[ToolCallChunk] | None = None) -> StreamChunk:
    return StreamChunk(delta=Delta(content=content, tool_calls=tool_calls))


# === finalize_checked:聚合与校验同源 ===

def test_finalize_checked_reports_invalid_args() -> None:
    """非法参数在**聚合器内**定型为 ParamError,调用方无需自己解析 JSON。

    这是模块自述能力边界的落实:此前 `finalize` 聚合、`loop` 另做一次 JSON 解析,
    两处各写一套判定;现在非法调用由 collector 直接给出结论。
    """
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="c1", name="read_file",
                                            arguments='{"path": "a.py')]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=1, id="c2", name="write_file",
                                            arguments='{"path": "b.py"}')]))
    msg, errors = c.finalize_checked()

    assert len(msg.tool_calls) == 2  # 聚合不受影响(非法调用依然出现在消息里)
    assert [e.tool_call_id for e in errors] == ["c1"]  # 只有 c1 非法
    assert errors[0].tool_name == "read_file"
    assert errors[0].reason  # 有可读原因
    assert errors[0].arguments == '{"path": "a.py'  # 保留原始串(便于排查)


def test_finalize_checked_clean_when_all_valid() -> None:
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="c1", name="read_file",
                                            arguments='{"path": "a.py"}')]))
    _, errors = c.finalize_checked()
    assert errors == []


def test_finalize_is_aggregate_only() -> None:
    """`finalize()` 仍是不校验的兼容入口(等价于 finalize_checked 的消息部分)。"""
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="c1", name="read_file",
                                            arguments="not json")]))
    msg = c.finalize()
    assert len(msg.tool_calls) == 1  # 非法也在消息里(不校验)


def test_param_error_observation_is_single_source() -> None:
    """错误 Observation 文案单一来源 —— loop 主路径与执行层兜底共用,措辞不会漂移。"""
    err = ParamError(tool_call_id="c1", tool_name="t", reason="工具参数为空", arguments="")
    assert param_error_observation(err) == "参数校验失败,拒绝执行: 工具参数为空"


def test_aggregate_text_deltas() -> None:
    c = StreamCollector()
    c.feed(_chunk(content="Hello "))
    c.feed(_chunk(content="world"))
    c.feed(_chunk())
    msg = c.finalize()
    assert msg.content == "Hello world"
    assert msg.tool_calls == []


def test_aggregate_tool_call_arguments() -> None:
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(id="call_1", name="read_file", arguments="")]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(arguments='{"path":')]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(arguments='"a.py"}')]))
    msg = c.finalize()
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == '{"path":"a.py"}'


def test_aggregate_multiple_tool_calls_in_order() -> None:
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=1, id="b", name="write_file")]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="a", name="read_file")]))
    msg = c.finalize()
    # finalize 按 index 排序,保证回灌顺序与模型调用顺序一致
    assert [tc.id for tc in msg.tool_calls] == ["a", "b"]


def test_empty_arguments_rejected() -> None:
    with pytest.raises(ValueError, match="空"):
        parse_tool_arguments("")


def test_invalid_json_rejected() -> None:
    with pytest.raises(ValueError):
        parse_tool_arguments('{"path": "unclosed')


def test_non_object_json_rejected() -> None:
    with pytest.raises(ValueError):
        parse_tool_arguments("[1, 2, 3]")


def test_valid_object_parsed() -> None:
    assert parse_tool_arguments('{"path":"a.py","line":3}') == {"path": "a.py", "line": 3}


# === parse_tool_arguments 边界(新增) ===

def test_whitespace_only_rejected() -> None:
    with pytest.raises(ValueError, match="空"):
        parse_tool_arguments("   \n\t  ")


def test_whitespace_wrapped_valid_json_parsed() -> None:
    assert parse_tool_arguments('  {"path": "a.py"}  ') == {"path": "a.py"}


def test_empty_object_accepted() -> None:
    assert parse_tool_arguments("{}") == {}


def test_typed_values_preserved() -> None:
    raw = '{"flag": true, "count": 3, "ratio": 0.5, "note": null, "nested": {"k": "v"}}'
    parsed = parse_tool_arguments(raw)
    assert parsed == {
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "note": None,
        "nested": {"k": "v"},
    }


# === StreamCollector 多工具乱序聚合(新增) ===

def test_interleaved_multi_tool_arguments_out_of_order() -> None:
    """两个工具的参数分块乱序到达,仍按 index 各自聚合,不回灌错乱。"""
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="call_a", name="read_file")]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=1, id="call_b", name="write_file")]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=1, arguments='{"path":')]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, arguments='{"path":')]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, arguments='"a.py"}')]))
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=1, arguments='"b.py"}')]))
    msg = c.finalize()
    assert [tc.id for tc in msg.tool_calls] == ["call_a", "call_b"]
    assert [tc.arguments for tc in msg.tool_calls] == ['{"path":"a.py"}', '{"path":"b.py"}']
    # 回灌顺序按 index 递增(与模型调用顺序一致)
    assert [tc.index for tc in msg.tool_calls] == [0, 1]


def test_missing_id_falls_back_to_call_index() -> None:
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=2, name="read_file")]))
    msg = c.finalize()
    assert msg.tool_calls[0].id == "call_2"
    assert msg.tool_calls[0].name == "read_file"


def test_missing_name_keeps_empty_string() -> None:
    c = StreamCollector()
    c.feed(_chunk(tool_calls=[ToolCallChunk(index=0, id="call_x")]))
    msg = c.finalize()
    assert msg.tool_calls[0].name == ""


def test_finish_reason_captured() -> None:
    c = StreamCollector()
    c.feed(StreamChunk(delta=Delta(), finish_reason="tool_calls"))
    msg = c.finalize()
    assert msg.finish_reason == "tool_calls"


def test_content_and_tool_calls_in_same_chunk() -> None:
    c = StreamCollector()
    c.feed(StreamChunk(delta=Delta(content="思考", tool_calls=[ToolCallChunk(index=0, id="c1")])))
    msg = c.finalize()
    assert msg.content == "思考"
    assert len(msg.tool_calls) == 1


def test_reasoning_content_aggregated() -> None:
    """DeepSeek 思考增量(reasoning_content)被聚合到 reasoning_content 字段。"""
    c = StreamCollector()
    c.feed(StreamChunk(delta=Delta(reasoning_content="让我想想")))
    c.feed(StreamChunk(delta=Delta(reasoning_content=", 先读文件")))
    c.feed(StreamChunk(delta=Delta(content="实际回答")))
    msg = c.finalize()
    assert msg.reasoning_content == "让我想想, 先读文件"
    assert msg.content == "实际回答"


def test_usage_only_chunk_captured() -> None:
    """include_usage 的 usage-only 块 → collector.usage 记录 token 用量。"""
    c = StreamCollector()
    c.feed(StreamChunk(delta=Delta(content="回答")))
    c.feed(StreamChunk(delta=Delta(), usage=Usage(prompt_tokens=12, completion_tokens=3)))
    c.feed(StreamChunk(delta=Delta(content="尾")))  # usage 之后继续 content 正常聚合
    msg = c.finalize()
    assert msg.content == "回答尾"
    assert c.usage is not None
    assert c.usage.prompt_tokens == 12
    assert c.usage.completion_tokens == 3


def test_parallel_tool_indices_preserved_through_parse() -> None:
    """【回归】真实流式路径(parse_sse_line)透传 tool_call 的 index。

    修复前:index 丢失 → 并行工具调用坍缩合并、参数拼接成非法 JSON、id 后写覆盖。
    修复后:index 透传 → 各自独立聚合,参数/顺序正确。
    """
    from agent.client import parse_sse_line

    line = (
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"call_a","function":{"name":"read","arguments":"{\\"p\\":1}"}},'
        '{"index":1,"id":"call_b","function":{"name":"write","arguments":"{\\"p\\":2}"}}'
        ']},"finish_reason":null}]}'
    )
    chunk = parse_sse_line(line)
    assert chunk is not None
    assert chunk.delta.tool_calls is not None
    # index 已透传
    assert [tc.index for tc in chunk.delta.tool_calls] == [0, 1]

    c = StreamCollector()
    c.feed(chunk)
    msg = c.finalize()
    # 两个调用各自独立聚合,参数完整
    assert len(msg.tool_calls) == 2
    assert msg.tool_calls[0].id == "call_a"
    assert msg.tool_calls[0].arguments == '{"p":1}'
    assert msg.tool_calls[1].id == "call_b"
    assert msg.tool_calls[1].arguments == '{"p":2}'
