"""Anthropic Messages API 流式客户端(与 OpenAI 客户端同接口)。

**双实现**:优先用官方 `anthropic` SDK(安装即用);未安装时回退到本模块的
httpx 手写实现。两条路径产出**同样的 `StreamChunk` 流**,故 `StreamCollector`、
`AgentLoop`、并发分区、审批、落盘全部零改动。

Anthropic 的 SSE 与 OpenAI 有三处结构性差异,每处都必须在此**归一化**,否则
上层要么丢数据、要么静默出错:

1. **`stop_reason` 语义不同** —— Anthropic 用 `max_tokens` / `end_turn` /
   `tool_use`。上层靠 `finish_reason == "length"` 判定截断并广播 `truncated` 事件,
   故必须映射 `max_tokens → "length"`、`tool_use → "tool_calls"`。
2. **usage 分两次到达** —— `message_start` 给 `input_tokens`,`message_delta` 给
   `output_tokens`。而 `StreamCollector.feed` 对每个带 usage 的 chunk 是**直接覆盖**
   (`self.usage = chunk.usage`)。若照搬两个 usage 块,`prompt_tokens` 会被第二次
   覆盖清成 0。故这里**内部累加,末尾一次性产出**一个 usage chunk。
3. **工具参数是 JSON 增量** —— `input_json_delta.partial_json` 需要像 OpenAI 的
   `arguments` 一样按 index 累加拼接,由 `StreamCollector` 按 index 分桶。

另:SEC 事件流每行都有 `event:` 与 `data:` 两类行,故**不能复用** `agent.client` 的
`parse_sse_line`(它对非 `data:` 行直接抛错)。
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
    to_anthropic_messages,
)

# Anthropic 的 stop_reason → 上层的 finish_reason 口径(见模块 docstring 差异 1)
_STOP_REASON_MAP = {
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "end_turn": "stop",
    "stop_sequence": "stop",
}


def _sdk_available() -> bool:
    """官方 SDK 是否可用(未安装 → 走内置 httpx 实现)。"""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def parse_anthropic_event(event: str, data: str) -> dict[str, Any] | None:
    """解析一条 Anthropic SSE 事件。

    与 OpenAI 不同,Anthropic 的每个事件都有 `event:` 行给出类型,`data:` 行给 JSON。
    返回 `{"type": ..., "data": {...}}`;非 JSON / 空 / `[DONE]` → None。
    """
    payload = data.strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(f"Anthropic SSE 数据非合法 JSON: {e};内容: {payload[:120]!r}") from e
    # 事件类型优先取 `event:` 行;缺失时回退到 payload 的 type 字段(流式协议两种都有)
    kind = event.strip() or str(obj.get("type", ""))
    if not kind:
        return None
    return {"type": kind, "data": obj}


def _sdk_event_to_dict(ev: Any) -> tuple[str, dict[str, Any]]:
    """官方 SDK 的事件对象 → `(type, dict)`,与 HTTP 路径的解析结果**同形**。

    这样两条实现共用同一套翻译器(`AnthropicClient._translate` / `_absorb_usage`),
    协议语义只有一份 —— 否则 SDK 路径与 httpx 路径会随时间漂移,而漂移表现为
    「装没装 SDK 行为不一样」,极难排查。
    """
    dump = ev.model_dump()
    return str(dump.get("type", "")), dump


class AnthropicClient:
    """Anthropic Messages API 流式客户端。transport 可注入 mock(测试用)。

    **双实现**由 `_use_sdk` 选择:
    - 装了官方 `anthropic` 包且未注入 `transport` → 走 SDK(它自带重试/超时/连接池);
    - 未装(或注入了 mock transport,见下)→ 走内置 httpx 实现。

    注入 `transport` 时**强制走 httpx**:MockTransport 是 httpx 的概念,SDK 无法接受它。
    这也让 CI 的离线测试只覆盖 httpx 路径 —— 故 SDK 路径的语义正确性由
    「两条路径共用翻译器」这个结构性事实保证,而不是靠双份测试。
    """

    def __init__(
        self, config: LLMConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not config.api_key:
            raise ValueError(
                "LLM api_key 未配置:请在 .env 中设置 LLM_API_KEY / ANTHROPIC_API_KEY"
            )
        self._config = config
        self._transport = transport
        self._use_sdk = transport is None and _sdk_available()
        # usage 累加状态(跨 `_translate_events` 的多次调用保留,见其 docstring)
        self._prompt_tokens = 0
        self._completion_tokens = 0

    def _payload(
        self, system: str, messages: list[dict[str, object]], tools: list[dict[str, object]] | None
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            # Anthropic 要求 max_tokens 必填(不像 OpenAI 可省略)
            "max_tokens": self._config.max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system:
            payload["system"] = system
        # 扩展思考/推理模型不接受显式 temperature(与 OpenAI 侧同一纪律)
        if not self._config.supports_reasoning:
            payload["temperature"] = self._config.temperature
        if tools:
            payload["tools"] = tools
        return payload

    def tools_payload(self, tools_spec: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        """工具声明表 → Anthropic tools 外形(`name` / `description` / `input_schema`)。

        与 OpenAI 的 `{"type":"function","function":{...}}` 不同,Anthropic 是扁平的。
        两者都从**同一份** `tools_spec` 派生(见 `tools/registry.py::tools_spec_from`),
        故工具定义只有一处来源,不存在两套 schema 漂移。
        """
        return [
            {
                "name": name,
                "description": spec.get("description", ""),
                "input_schema": spec.get("parameters", {"type": "object", "properties": {}}),
            }
            for name, spec in tools_spec.items()
        ]

    def serialize_messages(
        self, messages: list[AgentMessage]
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """AgentMessage 列表 → `(extra_payload, messages)`。

        Anthropic 的 `system` 是**顶层参数**(见 `to_anthropic_messages`),
        故这里把它放进 extra,由 `stream_chat` 并入请求体。
        """
        system, converted = to_anthropic_messages(messages)
        return ({"system": system} if system else {}), converted

    async def stream_chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        system: str = "",
    ) -> AsyncIterator[StreamChunk]:
        """发起流式请求,逐块产出**与 OpenAI 客户端同形**的 StreamChunk。

        messages 需已由 `to_anthropic_messages` 转换(它返回 (system, messages));
        `system` 单独传入。两条实现产出同一形状的事件流。
        """
        # 重置 usage 累加(同一客户端实例可能被复用;每次调用必须从零计)
        self._prompt_tokens = 0
        self._completion_tokens = 0
        if self._use_sdk:
            async for chunk in self._stream_via_sdk(messages, tools, system):
                yield chunk
        else:
            async for chunk in self._stream_via_httpx(messages, tools, system):
                yield chunk
        # 差异 2(收尾):整条流结束后产出一个 usage chunk —— 两条实现共用。
        # 放在这里(而非各自内部)保证 SDK 与 httpx 路径的 token 口径完全一致。
        usage = self._usage_chunk()
        if usage is not None:
            yield usage

    async def _stream_via_sdk(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None,
        system: str,
    ) -> AsyncIterator[StreamChunk]:
        """官方 SDK 路径。事件对象经 `_sdk_event_to_dict` 归一后走同一套翻译器。"""
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=self._config.api_key,
            base_url=self._config.base_url,
            default_headers={"anthropic-version": self._config.api_version},
            timeout=self._config.request_timeout,
        )
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if not self._config.supports_reasoning:
            kwargs["temperature"] = self._config.temperature
        if tools:
            kwargs["tools"] = tools
        async with client.messages.stream(**kwargs) as stream:
            async for ev in stream:
                async for chunk in self._translate_events(
                    [_sdk_event_to_dict(ev)]
                ):
                    yield chunk

    async def _stream_via_httpx(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None,
        system: str,
    ) -> AsyncIterator[StreamChunk]:
        """内置 httpx 实现(未装 SDK,或测试注入了 mock transport)。"""
        url = self._config.base_url.rstrip("/") + "/v1/messages"
        payload = self._payload(system, messages, tools)
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": self._config.api_version,
            "content-type": "application/json",
        }
        timeout = httpx.Timeout(self._config.request_timeout)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout
        ) as client, client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    f"Anthropic API 返回 {resp.status_code}: {body[:500]}",
                    request=resp.request,
                    response=resp,
                )
            event = ""
            events: list[tuple[str, dict[str, Any]]] = []
            async for line in resp.aiter_lines():
                stripped = line.strip()
                if stripped.startswith("event:"):
                    event = stripped[len("event:") :].strip()
                    continue
                if not stripped.startswith("data:"):
                    continue  # 空行 / 注释 / 其它字段行
                parsed = parse_anthropic_event(event, stripped[len("data:") :])
                event = ""  # 一个事件类型只作用于紧随其后的 data 行
                if parsed is not None:
                    events.append((parsed["type"], parsed["data"]))
                # 逐条翻译并产出,保持真流式(不等整条流结束)
                async for chunk in self._translate_events(events):
                    yield chunk
                events.clear()
            async for chunk in self._translate_events(events):
                yield chunk

    async def _translate_events(
        self, events: list[tuple[str, dict[str, Any]]]
    ) -> AsyncIterator[StreamChunk]:
        """把归一后的事件翻译成 StreamChunk(两条实现共用,含 usage 累加)。

        usage 累加状态挂在实例上(`_prompt_tokens` / `_completion_tokens`),因为
        `_translate_events` 会被逐批调用 —— 局部变量无法跨批保留。用量在流开始时重置。
        """
        for kind, data in events:
            # ⚠️ 顺序要紧:**先吸收 usage,再翻译**。`message_delta` 同时携带
            # stop_reason 与 output_tokens;若先翻译,它产出的 chunk 会 yield 出去,
            # usage 那一步被跳过,completion_tokens 永远是 0。
            self._absorb_usage(kind, data)
            chunk = self._translate(kind, data)
            if chunk is not None:
                yield chunk

    def _absorb_usage(self, kind: str, data: dict[str, Any]) -> None:
        """把带 usage 的控制事件累加进计数(Anthropic 分 message_start / message_delta)。

        ⚠️ 用 `is not None` 判存在,**不能用 `or`**:`message_start` 里
        `output_tokens` 正常就是 **0**,`0 or old` 会把它当成缺失而保留旧值;
        而 `message_delta` 才是 `output_tokens` 的真正来源 —— 一旦被吞掉,
        completion_tokens 永远是 0(静默失真,不报错)。
        """
        if kind == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            value = usage.get("input_tokens")
            if value is not None:
                self._prompt_tokens = int(value)
        elif kind == "message_delta":
            usage = data.get("usage") or {}
            value = usage.get("output_tokens")
            if value is not None:
                self._completion_tokens = int(value)

    def _usage_chunk(self) -> StreamChunk | None:
        """整条流结束后产出的**唯一** usage chunk(总量一次给全)。无则 None。"""
        if not self._prompt_tokens and not self._completion_tokens:
            return None
        return StreamChunk(
            index=0,
            delta=Delta(),
            usage=Usage(
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                total_tokens=self._prompt_tokens + self._completion_tokens,
            ),
        )


    @staticmethod
    def _translate(kind: str, data: dict[str, Any]) -> StreamChunk | None:
        """把一条 Anthropic 事件翻译成 StreamChunk;纯控制事件 → None。"""
        if kind == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                # 工具调用的开头:给 id / name,参数稍后由 input_json_delta 累加
                return StreamChunk(
                    index=int(data.get("index", 0)),
                    delta=Delta(
                        tool_calls=[
                            ToolCallChunk(
                                id=block.get("id"),
                                name=block.get("name"),
                                arguments="",
                                # index 承载 content_block 的下标:StreamCollector 据此分桶,
                                # 与 OpenAI 的 tool_calls[].index 同义(并行调用靠它区分)
                                index=int(data.get("index", 0)),
                            )
                        ]
                    ),
                )
            return None
        if kind == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return StreamChunk(
                    index=int(data.get("index", 0)),
                    delta=Delta(content=delta.get("text", "")),
                )
            if dtype == "input_json_delta":
                # 差异 3:参数是增量 JSON 片段,交回 collector 按 index 累加拼接
                return StreamChunk(
                    index=int(data.get("index", 0)),
                    delta=Delta(
                        tool_calls=[
                            ToolCallChunk(
                                arguments=delta.get("partial_json", ""),
                                index=int(data.get("index", 0)),
                            )
                        ]
                    ),
                )
            if dtype == "thinking_delta":
                return StreamChunk(
                    index=int(data.get("index", 0)),
                    delta=Delta(reasoning_content=delta.get("thinking", "")),
                )
            return None
        if kind == "message_delta":
            # 差异 1:stop_reason 归一,否则上层的截断判定永远不触发
            raw = data.get("delta", {}).get("stop_reason")
            if raw is None:
                return None
            return StreamChunk(
                index=0, delta=Delta(), finish_reason=_STOP_REASON_MAP.get(str(raw), str(raw))
            )
        return None
