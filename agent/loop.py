"""ReAct 主循环。

流程(每轮):
1. 收集流式增量 → AssistantMessage(经 StreamCollector 聚合)。
2. 若无工具调用 → 结束,返回最终回答。
3. 校验每个工具调用的参数;非法参数 → 生成错误 Observation(禁止执行)。
4. 合法工具调用 → partition + execute,按模型调用顺序回灌结果。
5. 重复直到:无工具调用 / 达到 max_steps / 工具调用全部非法(自动收敛)。

安全语义:
- 非法参数永不执行,回灌错误 Observation 让模型自行修正。
- max_steps 上限防止无限循环。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from agent.client import StreamingClient, build_stream_client
from agent.config import LLMConfig
from agent.stream_collector import ParamError, StreamCollector, param_error_observation
from agent.tool_calls import execute_partitions, partition_tool_calls
from agent.types import (
    AgentMessage,
    AgentResult,
    AssistantMessage,
    ToolCall,
)
from context.compression import (
    ContextWindow,
    compress_history,
    compress_to_budget,
    estimate_tokens,
)

StepKind = Literal[
    "model_call",
    "thinking",
    "assistant",
    "tool_exec_start",
    "tool_result",
    "invalid_params",
    "truncated",
    "finish",
    "all_invalid",
    "max_steps",
    "usage",
    "resume",
    "verify",
]


@dataclass
class StepEvent:
    """循环过程的回调事件(供 CLI/前端/评测消费)。kind 为受限字面量。"""

    kind: StepKind
    data: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    """ReAct Agent Loop。tools_spec: name -> {description, parameters(json schema)}。

    M4 持久化挂接:
    - session_id:会话标识(默认自动生成),供 store 定位记录文件。
    - record_event:可选的 StepEvent 回调(不传则 no-op;传则每次事件都回调)。
    - usage:ContextWindow 记账(total_messages 计数),供上下文压缩决策。
    - run 可重入:多次调用共用同一会话上下文,消息历史不重置(会话记录追加)。
    """

    def __init__(
        self,
        client: StreamingClient,
        tools_spec: dict[str, dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
        *,
        system_prompt: str,
        max_steps: int,
        max_concurrency: int = 1,
        session_id: str | None = None,
        record_event: Callable[[StepEvent], Awaitable[None] | None] | None = None,
        window_size: int = 200,
        token_budget: int | None = None,
    ) -> None:
        self._client = client
        self._tools_spec = tools_spec
        self._dispatch = dispatch
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        # 并发安全上限:默认 1(全串行,最保守)。>1 时同轮内可并发的工具数。
        self._max_concurrency = max_concurrency
        # M4 持久化挂接
        self.session_id = session_id or f"sess-{os.urandom(4).hex()}"
        # M9 W5:token_budget(可选)触发 token 水位压缩;消息数 window_size 兜底。
        self.usage = ContextWindow(window_size=window_size, token_budget=token_budget)
        self._record_event = record_event or (lambda e: None)
        # M4 工具调用落盘回调:(tool, result) → 持久化完整 ToolCallRecord
        self._record_call: Callable[[ToolCall, AgentResult], Awaitable[None] | None] | None = None

    async def _emit(self, kind: StepKind, **data: Any) -> None:
        callback = self._record_event
        result = callback(StepEvent(kind=kind, data=data))
        if result is not None:
            await result

    async def _call_model(
        self, messages: list[AgentMessage]
    ) -> tuple[AssistantMessage, list[ParamError]]:
        """一次模型调用:流式聚合为一回合完整输出 + 非法参数清单。

        调用前压缩早期历史(M4 上下文压缩挂接点):
        - M9 W5:token_budget 设置且估算超预算 → compress_to_budget(token 水位优先);
        - 兜底:消息数超 window_size → compress_history(历史语义,防回归)。
        system/user 永不剔除。估算基于当前 messages 列表(启发式),非 usage 累计值。

        校验在 StreamCollector 内完成(单一来源),本方法把结果一并返回 ——
        调用方不再自己解析 JSON 判断合法性。返回的 ParamError 必须转成错误
        Observation 回灌,禁止进入执行层。
        """
        if (
            self.usage.token_budget is not None
            and estimate_tokens(messages) > self.usage.token_budget
        ):
            messages[:] = compress_to_budget(messages, self.usage.token_budget)
        elif len(messages) > self.usage.window_size:
            messages[:] = compress_history(messages, self.usage.window_size)
        # 消息序列化按 provider 分派(OpenAI 直接给 messages;Anthropic 需把
        # system 提到顶层)。协议差异封在客户端,循环只拿 (extra, messages) 两段。
        extra_payload, wire_messages = self._client.serialize_messages(messages)
        tools = self._build_tools_payload()
        collector = StreamCollector()
        async for chunk in self._client.stream_chat(
            wire_messages, tools=tools, **extra_payload
        ):
            collector.feed(chunk)
        if collector.usage is not None:
            # M8:真实 token 记账(include_usage 的 usage-only 块 → ContextWindow 累计)
            self.usage.add_tokens(
                collector.usage.prompt_tokens, collector.usage.completion_tokens
            )
        return collector.finalize_checked()

    def _build_tools_payload(self) -> list[dict[str, object]]:
        """工具声明表 → 本 provider 的工具外形(实现见客户端,协议差异封在那里)。"""
        return self._client.tools_payload(self._tools_spec)

    def _parallel_safe(self, name: str) -> bool:
        """该工具是否声明为并发安全(真值来自 Tool.parallel_safe → tools_spec)。

        元数据缺失时保守返回 False(串行),不会把未知工具并发化。
        """
        return bool(self._tools_spec.get(name, {}).get("parallel_safe", False))

    async def run(
        self,
        user_task: str,
        events: Callable[[StepEvent], Awaitable[None]] | None = None,
    ) -> list[AgentMessage]:
        """运行整个 ReAct 循环,返回完整消息历史(含最终回答)。

        events 回调用于实时消费 StepEvent(工具开始/结束/错误等)。
        注意:record_event(构造时注入)负责持久化/记账;这里的 events 是面向
        调用方的一次性实时回调。两者独立,M4 起 record_event 覆盖 usage 记账。
        """
        messages: list[AgentMessage] = [AgentMessage(role="system", content=self._system_prompt)]
        messages.append(AgentMessage(role="user", content=user_task))
        return await self._run_reactive(messages, events=events)

    async def resume_with_seed(
        self,
        seed: list[AgentMessage],
        user_task: str,
        events: Callable[[StepEvent], Awaitable[None]] | None = None,
    ) -> list[AgentMessage]:
        """以持久化恢复上下文(seed)预热后继续运行——M4 会话恢复核心。

        seed 来自 build_resume_seed(store 记录):已完成历史 + overridden 剔除 +
        pending 只读重放 / 副作用跳过提示。模型不会重放已完成副作用操作。
        """
        if not any(m.role == "system" for m in seed):
            messages = [AgentMessage(role="system", content=self._system_prompt), *seed]
        else:
            messages = list(seed)
        messages.append(AgentMessage(role="user", content=user_task))
        return await self._run_reactive(messages, events=events)

    async def _run_reactive(
        self,
        messages: list[AgentMessage],
        events: Callable[[StepEvent], Awaitable[None]] | None = None,
    ) -> list[AgentMessage]:
        """ReAct 循环主体:从给定消息历史继续,直到结束或达 max_steps。"""
        async def _broadcast(kind: StepKind, **data: Any) -> None:
            # 实例级 record_event(持久化/记账)先于调用方实时回调
            await self._emit(kind, **data)
            if events is not None:
                await events(StepEvent(kind=kind, data=data))

        for step in range(self._max_steps):
            await _broadcast("model_call", step=step)
            # 校验与聚合**同源**(StreamCollector 内部完成),loop 只消费结论。
            assistant, param_errors = await self._call_model(messages)
            messages.append(
                AgentMessage(
                    role="assistant",
                    content=assistant.content,
                    reasoning_content=assistant.reasoning_content,
                    tool_calls=assistant.tool_calls,
                )
            )
            await _broadcast(
                "assistant",
                step=step,
                content=assistant.content,
                reasoning_content=assistant.reasoning_content,
                tool_calls=[tc.model_dump() for tc in assistant.tool_calls],
            )
            if assistant.reasoning_content:
                await _broadcast("thinking", step=step, content=assistant.reasoning_content)

            if not assistant.tool_calls:
                # 无工具调用 → 本回合结束。区分自然结束与 max_tokens 截断。
                if assistant.finish_reason == "length":
                    await _broadcast("truncated", step=step, content=assistant.content)
                await _broadcast("finish", step=step, content=assistant.content)
                await self._observe(messages, step)
                return messages

            # 校验工具调用:非法参数 → 错误 Observation,禁止执行。
            # 判定来自 StreamCollector.finalize_checked(与聚合同一处),此处只分发:
            # 合法调用进执行层;非法调用按 tool_call_id 记下,稍后统一回灌错误文案。
            error_by_id: dict[str, ParamError] = {e.tool_call_id: e for e in param_errors}
            valid_calls: list[ToolCall] = [
                tc for tc in assistant.tool_calls if tc.id not in error_by_id
            ]
            for e in param_errors:
                await _broadcast("invalid_params", step=step, tool=e.tool_name, error=e.reason)

            # 执行合法调用:分组并发;结果按模型调用顺序排列
            results_by_id: dict[str, AgentResult] = {}
            if valid_calls:
                groups = partition_tool_calls(
                    valid_calls,
                    max_concurrency=self._max_concurrency,
                    # M9 W4:并发安全谓词(tools_spec 里 Tool.parallel_safe 的携带值)
                    parallel_safe=self._parallel_safe,
                )
                await _broadcast(
                    "tool_exec_start",
                    step=step,
                    count=len(valid_calls),
                    tools=[tc.name for tc in valid_calls],  # 前端渲染工具名(SSE)
                )
                for r in await execute_partitions(groups, self._dispatch):
                    results_by_id[r.tool_call_id] = r
                    await _broadcast(
                        "tool_result",
                        step=step,
                        tool_call_id=r.tool_call_id,
                        is_error=r.is_error,
                        content=r.content,  # 前端渲染工具结果(SSE)
                    )

            # 按 assistant.tool_calls 原顺序统一回灌(非法→错误 Observation,合法→真实结果)
            ordered_results: list[AgentResult] = []
            for tc in assistant.tool_calls:
                if tc.id in error_by_id:
                    # tool_call_id 必须带上:tool 消息的 id 要与 assistant 的 tool_calls
                    # 精确匹配(provider 硬约束),且落盘/resume seed 依赖它配对。
                    ordered_results.append(
                        AgentResult(
                            tool_call_id=tc.id,
                            content=param_error_observation(error_by_id[tc.id]),
                            is_error=True,
                        )
                    )
                elif tc.id in results_by_id:
                    ordered_results.append(results_by_id[tc.id])

            for r in ordered_results:
                messages.append(
                    AgentMessage(
                        role="tool",
                        content=r.content,
                        tool_call_id=r.tool_call_id,
                        # is_error 随消息保留:Anthropic 的 tool_result block 有原生
                        # 错误标记,丢了这一位就只能从正文猜(OpenAI 协议无此字段,
                        # 故它此前没有消费方 —— 不是冗余)。
                        is_error=r.is_error,
                    )
                )
                # M4:完整工具调用落盘(工具名/参数/结果 → ToolCallRecord)
                if self._record_call is not None and r.tool_call_id:
                    for tc in assistant.tool_calls:
                        if tc.id == r.tool_call_id:
                            call_result = self._record_call(tc, r)
                            if call_result is not None:
                                await call_result
                            break

            await self._observe(messages, step)

            if not valid_calls:
                # 本轮全为非法调用:错误 Observation 已回灌,继续循环让模型修正
                await _broadcast("all_invalid", step=step, count=len(error_by_id))
                continue

        await _broadcast("max_steps", steps=self._max_steps)
        await self._observe(messages, self._max_steps)
        return messages

    async def _observe(self, messages: list[AgentMessage], step: int) -> None:
        """记账 + 触发 record_event(供持久化/SSE 消费)。"""
        self.usage.observe(len(messages))
        await self._emit(
            "usage",
            step=step,
            total_messages=self.usage.total_messages,
            prompt_tokens=self.usage.prompt_tokens,
            completion_tokens=self.usage.completion_tokens,
            total_tokens=self.usage.prompt_tokens + self.usage.completion_tokens,
        )


def build_agent(
    config: LLMConfig,
    tools_spec: dict[str, dict[str, Any]],
    dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
    *,
    system_prompt: str,
    transport: httpx.AsyncBaseTransport | None = None,
    max_concurrency: int = 1,
    session_id: str | None = None,
    record_event: Callable[[StepEvent], Awaitable[None] | None] | None = None,
    record_call: Callable[[ToolCall, AgentResult], Awaitable[None] | None] | None = None,
    window_size: int = 200,
    token_budget: int | None = None,
) -> AgentLoop:
    """便捷构造:根据配置创建 StreamingClient + AgentLoop。

    M4 起支持 session_id / record_event(持久化挂接)、record_call(工具调用落盘)
    与 window_size(上下文压缩)。M9 W5 起支持 token_budget(token 水位压缩)。
    """
    client = build_stream_client(config, transport=transport)
    loop = AgentLoop(
        client,
        tools_spec,
        dispatch,
        system_prompt=system_prompt,
        max_steps=config.max_steps,
        max_concurrency=max_concurrency,
        session_id=session_id,
        record_event=record_event,
        window_size=window_size,
        token_budget=token_budget,
    )
    loop._record_call = record_call
    return loop
