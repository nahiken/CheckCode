"""M1 单元测试:SSE 行解析与流式分块。"""

from __future__ import annotations

import pytest

from agent.client import parse_sse_line
from agent.types import StreamChunk


def test_parse_text_delta() -> None:
    chunk = parse_sse_line('data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}')
    assert chunk is not None
    assert chunk.delta.content == "hello"
    assert chunk.finish_reason is None


def test_parse_tool_call_delta() -> None:
    line = (
        'data: {"choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"call_1","function":{"name":"read_file","arguments":""}}'
        ']},"finish_reason":null}]}'
    )
    chunk = parse_sse_line(line)
    assert chunk is not None
    tc = chunk.delta.tool_calls
    assert tc is not None and len(tc) == 1
    assert tc[0].id == "call_1"
    assert tc[0].name == "read_file"


def test_parse_done_line_returns_none() -> None:
    assert parse_sse_line("data: [DONE]") is None


def test_parse_comment_and_empty_lines_return_none() -> None:
    assert parse_sse_line("") is None
    assert parse_sse_line(": keep-alive") is None


def test_parse_usage_only_block_captures_usage() -> None:
    # include_usage 会在最后发一个带 usage 但 choices 为空的块
    line = 'data: {"usage":{"prompt_tokens":10,"completion_tokens":5},"choices":[]}'
    chunk = parse_sse_line(line)
    assert chunk is not None
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 10
    assert chunk.usage.completion_tokens == 5
    assert chunk.usage.total_tokens == 15  # total 缺省时 = prompt + completion


def test_parse_empty_choices_without_usage_returns_none() -> None:
    # 空 choices 且无 usage(心跳/其它)→ 忽略
    assert parse_sse_line('data: {"choices":[]}') is None


def test_parse_non_sse_line_raises() -> None:
    with pytest.raises(ValueError):
        parse_sse_line("not a data line")


def test_parse_stream_chunk_is_pydantic() -> None:
    chunk = parse_sse_line('data: {"choices":[{"delta":{"role":"assistant"}}]}')
    assert isinstance(chunk, StreamChunk)
