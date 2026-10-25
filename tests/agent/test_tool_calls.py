"""M1 单元测试:工具调用分组与执行回灌。"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.tool_calls import execute_partitions, partition_tool_calls
from agent.types import AgentResult, ToolCall


def _tc(index: int, name: str = "read_file", args: str = "{}") -> ToolCall:
    return ToolCall(id=f"call_{index}", name=name, arguments=args, index=index)


# M9 W4:并发安全谓词(测试模拟 Tool.parallel_safe 元数据的携带值)
def _parallel_safe(name: str) -> bool:
    return name in {"read_file", "list_dir"}


def test_partition_serial_by_default() -> None:
    calls = [_tc(0), _tc(1), _tc(2)]
    groups = partition_tool_calls(calls)
    assert [len(g) for g in groups] == [1, 1, 1]
    # 顺序保持模型调用顺序
    assert [g[0].id for g in groups] == ["call_0", "call_1", "call_2"]


def test_partition_concurrency_groups() -> None:
    """无谓词 → 纯切片块分组(旧的并发上限语义,顺序保持)。"""
    calls = [_tc(0), _tc(1), _tc(2), _tc(3), _tc(4)]
    groups = partition_tool_calls(calls, max_concurrency=2)
    assert [len(g) for g in groups] == [2, 2, 1]


def test_partition_invalid_concurrency_raises() -> None:
    with pytest.raises(ValueError):
        partition_tool_calls([_tc(0)], max_concurrency=0)


# === M9 W4:parallel_safe 谓词分组(安全攒批并发/不安全独立串行) ===

def test_partition_parallel_safe_mixed_groups() -> None:
    """mixed [read, write, read, read] @2 → 安全读攒批/写串行屏障,顺序保持。"""
    calls = [_tc(0, "read_file"), _tc(1, "write_file"), _tc(2, "read_file"), _tc(3, "read_file")]
    groups = partition_tool_calls(calls, max_concurrency=2, parallel_safe=_parallel_safe)
    assert [[tc.name for tc in g] for g in groups] == [
        ["read_file"],  # 写屏障前的孤立读(未攒满批容量)独立成组
        ["write_file"],  # 不安全调用独立成组(串行屏障)
        ["read_file", "read_file"],  # 写后相邻只读攒批满额,组内并发
    ]
    # 组间顺序保持模型调用顺序
    flat = [tc.id for g in groups for tc in g]
    assert flat == ["call_0", "call_1", "call_2", "call_3"]


def test_partition_parallel_safe_trailing_batch_flushed() -> None:
    """尾部不足一批的安全调用也 flush(不成组遗留)。"""
    calls = [_tc(0, "read_file"), _tc(1, "read_file")]
    groups = partition_tool_calls(calls, max_concurrency=4, parallel_safe=_parallel_safe)
    # 批容量 4 > 调用数 2 → 尾部 flush 成一整组(仍可并发)
    assert [[tc.name for tc in g] for g in groups] == [["read_file", "read_file"]]


def test_partition_parallel_safe_all_unsafe_serial() -> None:
    """全部不安全 → 各自独立成组(全串行),不被并发化。"""
    calls = [_tc(0, "write_file"), _tc(1, "run_command"), _tc(2, "write_file")]
    groups = partition_tool_calls(calls, max_concurrency=4, parallel_safe=_parallel_safe)
    assert [len(g) for g in groups] == [1, 1, 1]
    assert [g[0].name for g in groups] == ["write_file", "run_command", "write_file"]


def test_partition_parallel_safe_all_safe_single_group() -> None:
    """全部安全且未超上限 → 合并为一整组并发执行。"""
    calls = [_tc(0, "read_file"), _tc(1, "read_file"), _tc(2, "read_file")]
    groups = partition_tool_calls(calls, max_concurrency=5, parallel_safe=_parallel_safe)
    assert [len(g) for g in groups] == [3]


def test_partition_parallel_safe_empty() -> None:
    """空调用 → 空组(无异常)。"""
    assert partition_tool_calls([], max_concurrency=2, parallel_safe=_parallel_safe) == []


async def _dispatch(name: str, args: dict[str, object]) -> AgentResult:
    return AgentResult(content=f"{name}:{json.dumps(args)}")


def test_execute_partitions_serial_order() -> None:
    calls = [_tc(0, "a", '{"n":1}'), _tc(1, "b", '{"n":2}')]
    results = asyncio.run(execute_partitions(partition_tool_calls(calls), _dispatch))
    assert [r.content for r in results] == ["a:{\"n\": 1}", "b:{\"n\": 2}"]


def test_invalid_json_argument_becomes_error_result() -> None:
    calls = [_tc(0, "bad", '{"n":') ]
    results = asyncio.run(execute_partitions(partition_tool_calls(calls), _dispatch))
    assert results[0].is_error is True
    assert "合法 JSON" in results[0].content


def test_dispatch_exception_becomes_error_result() -> None:
    async def boom(name: str, args: dict[str, object]) -> AgentResult:
        raise RuntimeError("boom")

    calls = [_tc(0, "crash", "{}")]
    results = asyncio.run(execute_partitions(partition_tool_calls(calls), boom))
    assert results[0].is_error is True
    assert "boom" in results[0].content


def test_tool_call_id_forced_from_request() -> None:
    """协议硬约束:tool 消息 id 必须用请求的 tool.id。

    AgentResult.tool_call_id 由 _run_single 强制绑定请求 id(工具层不填写)。
    这是真实 DeepSeek 调用暴露的 bug 的回归保障:provider 会拒绝 id 不匹配的请求。
    """

    async def ok_dispatch(name: str, args: dict[str, object]) -> AgentResult:
        return AgentResult(content="ok")

    calls = [_tc(0, "read_file", "{}")]
    results = asyncio.run(execute_partitions(partition_tool_calls(calls), ok_dispatch))
    assert results[0].tool_call_id == "call_0"  # 强制用请求 id
    assert results[0].content == "ok"
