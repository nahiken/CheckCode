"""OpenAI 兼容流式客户端。

- 使用 httpx.AsyncClient 流式请求 /chat/completions。
- SSE 逐行解析(`data: {...}` 行,`[DONE]` 结束)。
- 请求携带 `stream_options: {"include_usage": true}`,在最后一块返回 usage。
- 支持 mock transport(httpx.MockTransport),便于确定性测试。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agent.config import LLMConfig
from agent.types import (
    AgentMessage,
    Delta,
    StreamChunk,
    ToolCallChunk,
    Usage,
    to_openai_messages,
)


def parse_sse_line(line: str) -> StreamChunk | None:
    """解析一行 SSE `data: <json>`。跳过注释/空行/`[DONE]`。"""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        raise ValueError(f"非 SSE data 行: {line!r}")
    payload = line[len("data:") :].strip()
    if payload == "[DONE]":
        return None
    if not payload:
        return None  # 空 data: 行(部分 provider 会发心跳)
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        # 畸形 chunk 不应中断整条流;带上下文抛出便于定位
        raise ValueError(f"SSE 数据非合法 JSON: {e};行内容: {payload[:120]!r}") from e
    choices = obj.get("choices") or []
    if not choices:
        # include_usage:流末尾发一个带 usage 的 usage-only 块(token 用量,评测/前端消费)
        usage_raw = obj.get("usage")
        if usage_raw is not None:
            prompt = int(usage_raw.get("prompt_tokens", 0))
            completion = int(usage_raw.get("completion_tokens", 0))
            return StreamChunk(
                index=0,
                delta=Delta(),
                usage=Usage(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=int(usage_raw.get("total_tokens") or (prompt + completion)),
                ),
            )
        return None  # 空 choices 且无 usage(心跳/其它)
    choice = choices[0]
    raw_delta = choice.get("delta", {}) or {}
    tool_calls_raw = raw_delta.get("tool_calls")
    tool_calls = None
    if tool_calls_raw:
        tool_calls = [
            ToolCallChunk(
                id=tc.get("id"),
                name=(tc.get("function") or {}).get("name"),
                arguments=(tc.get("function") or {}).get("arguments", ""),
                # 透传 index:并行工具调用靠 index 区分,丢失会导致坍缩合并
                index=tc.get("index"),
            )
            for tc in tool_calls_raw
        ]
    return StreamChunk(
        index=choice.get("index", 0),
        delta=Delta(
            role=raw_delta.get("role"),
            content=raw_delta.get("content"),
            reasoning_content=raw_delta.get("reasoning_content"),
            tool_calls=tool_calls,
        ),
        finish_reason=choice.get("finish_reason"),
    )


class StreamingClient:
    """OpenAI 兼容流式客户端。transport 可注入 mock(测试用)。"""

    def __init__(
        self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not config.api_key:
            raise ValueError(
                "LLM api_key 未配置:请在 .env 中设置 LLM_API_KEY 或 DEEPSEEK_API_KEY"
            )
        self._config = config
        self._transport = transport

    def tools_payload(self, tools_spec: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        """工具声明表 → OpenAI function-calling 外形。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description", ""),
                    "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for name, spec in tools_spec.items()
        ]

    def serialize_messages(
        self, messages: list[AgentMessage]
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """AgentMessage 列表 → `(extra_payload, messages)`。

        OpenAI 没有顶层 system 参数,故 extra 恒为空 —— 保持与 Anthropic 客户端
        同一个返回形状,让调用方不必分支。
        """
        return {}, to_openai_messages(messages)

    async def stream_chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """发起流式请求,逐块产出解析后的 StreamChunk。"""
        url = self._config.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": self._config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # M10:推理模型(输出 reasoning_content)通常不接受 temperature → 省略该字段,
        # 避免部分 provider 直接 400。非推理模型保持原行为(显式发送 temperature)。
        if not self._config.supports_reasoning:
            payload["temperature"] = self._config.temperature
        if tools:
            payload["tools"] = tools
            # 未显式指定 tool_choice 时,交由模型自主决定(auto)
            payload["tool_choice"] = "auto"

        timeout = httpx.Timeout(self._config.request_timeout)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=timeout,
        ) as client:
            headers = {"Authorization": f"Bearer {self._config.api_key}"}
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"LLM API 返回 {resp.status_code}: {body[:500]}",
                        request=resp.request,
                        response=resp,
                    )
                async for line in resp.aiter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is not None:
                        yield chunk


def build_stream_client(
    config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None
) -> Any:
    """按 `config.provider` 造流式客户端(**生产的唯一构造点**)。

    两个实现遵守同一接口:`stream_chat(...) -> AsyncIterator[StreamChunk]`、
    `tools_payload(...)`、`serialize_messages(...)`。上层(`AgentLoop`)只依赖接口,
    因此新增协议族不需要动循环、聚合、并发、审批或落盘。

    anthropic SDK 的路径由 `config.provider` 选择客户端的**实现**,而不是在这里
    分支 —— 见 `agent/client_anthropic.AnthropicClient` 的模块 docstring(它内部
    优先 SDK、缺失时用 httpx 手写)。这样构造点只有一处,测试注入 `transport=`
    的既有模式对两个 provider 都成立。
    """
    if config.provider == "anthropic":
        # 延迟导入:未配 anthropic 的进程不该为它付导入成本(与 assembly.mcp 同一条纪律)
        from agent.client_anthropic import AnthropicClient

        return AnthropicClient(config, transport=transport)
    return StreamingClient(config, transport=transport)
