"""工具调用分组执行与按序回灌。

- partition_tool_calls:把一批工具调用划分成若干「并发安全」的组。
  组内调用可安全并发执行;组间串行(按模型调用顺序)。
- execute_partitions:执行各组,返回按模型调用顺序排列的 ToolResult 列表。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent.stream_collector import parse_tool_arguments
from agent.types import AgentResult, ToolCall


async def _run_single(
    tool: ToolCall,
    dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
) -> AgentResult:
    """执行单个工具调用。

    无论 dispatch 返回什么,`tool_call_id` 都强制使用请求的 tool.id——
    这是协议硬约束:tool 消息的 id 必须与 assistant 的 tool_calls id 精确匹配,
    DeepSeek 等 provider 会拒绝不匹配的请求。这也防止 dispatch 返回错误 id。
    """
    try:
        args = parse_tool_arguments(tool.arguments)  # 与 loop 复用同一套参数校验
    except ValueError as e:
        return AgentResult(
            tool_call_id=tool.id,
            content=f"参数校验失败,拒绝执行: {e}",
            is_error=True,
        )
    try:
        result = await dispatch(tool.name, args)
        # 强制用请求的 id(忽略 dispatch 填写的 id);审批现场(audit)透传
        return AgentResult(
            tool_call_id=tool.id,
            content=result.content,
            is_error=result.is_error,
            audit=result.audit,
        )
    except Exception as e:  # 工具执行异常必须转成 Observation,不能冒泡
        return AgentResult(tool_call_id=tool.id, content=f"工具执行异常: {e}", is_error=True)


def partition_tool_calls(
    tool_calls: list[ToolCall],
    max_concurrency: int = 1,
    parallel_safe: Callable[[str], bool] | None = None,
) -> list[list[ToolCall]]:
    """把工具调用划分成若干可并发执行的组。

    - max_concurrency=1 → 全串行(最保守;AgentLoop 库默认)。
    - max_concurrency>1 且未提供 parallel_safe → 按模型顺序取块,组内并发数
      不超过上限(旧的纯切片语义,测试保留)。
    - 提供 parallel_safe → 按「并发安全」分组:安全调用在模型顺序相邻区间内
      攒批,组满 flush;不安全调用各自独立成组(串行屏障),并阻断安全攒批。
      组间保持模型调用顺序;组内并发执行,结果仍按模型顺序回灌。

    parallel_safe(name) 谓词由调用方按工具元数据构造(见 agent/loop.py),
    真值来自 Tool.parallel_safe(工具层单一来源)。
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须 >= 1")

    groups: list[list[ToolCall]] = []
    if max_concurrency == 1 or parallel_safe is None:
        # 全串行,或按块分组(旧的纯切片语义)
        for i in range(0, len(tool_calls), max_concurrency):
            groups.append(tool_calls[i : i + max_concurrency])
        return groups

    batch: list[ToolCall] = []  # 当前已攒的安全调用(尚未 flush)

    def flush() -> None:
        if batch:
            groups.append(list(batch))
            batch.clear()

    for tc in tool_calls:
        if parallel_safe(tc.name):
            batch.append(tc)
            if len(batch) >= max_concurrency:
                flush()
        else:
            flush()  # 先落盘已攒的安全批(保持模型顺序)
            groups.append([tc])  # 不安全调用独立成组(串行屏障)
    flush()
    return groups


async def execute_partitions(
    groups: list[list[ToolCall]],
    dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
) -> list[AgentResult]:
    """逐组执行,返回按模型调用顺序排列的结果列表。"""
    results: list[AgentResult] = []
    for group in groups:
        group_results = await asyncio.gather(*(_run_single(t, dispatch) for t in group))
        results.extend(group_results)
    return results
