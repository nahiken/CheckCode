"""Anthropic Messages API 客户端测试(CI 全离线,用 httpx.MockTransport)。

重点覆盖三处**必须归一化**的协议差异(见 agent/client_anthropic.py 模块 docstring):
每一处若不处理,上层要么丢数据、要么静默出错 —— 故各有一条承重测试。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.client import build_stream_client
from agent.client_anthropic import AnthropicClient, parse_anthropic_event
from agent.config import LLMConfig
from agent.stream_collector import StreamCollector
from agent.types import AgentMessage, ToolCall, to_anthropic_messages


def _cfg(**kw: object) -> LLMConfig:
    base: dict[str, object] = {
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-test",
        "model": "claude-sonnet-4-6",
        "provider": "anthropic",
    }
    base.update(kw)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _ev(kind: str, obj: dict) -> str:
    """构造一条 Anthropic SSE 事件(每种事件有 event: 行 + data: 行)。"""
    return f"event: {kind}\ndata: {json.dumps(obj)}\n\n"


def _text_stream() -> str:
    return (
        _ev("message_start", {"type": "message_start", "message": {
            "usage": {"input_tokens": 11, "output_tokens": 0}}})
        + _ev("content_block_start", {"index": 0, "content_block": {"type": "text"}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "text_delta", "text": "你好"}})
        + _ev("content_block_stop", {"index": 0})
        + _ev("message_delta", {"delta": {"stop_reason": "end_turn"},
                                "usage": {"output_tokens": 7}})
        + _ev("message_stop", {"type": "message_stop"})
    )


def _client(stream: str, cfg: LLMConfig | None = None) -> AnthropicClient:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=stream))
    return AnthropicClient(cfg or _cfg(), transport=transport)


async def _collect(client: AnthropicClient, messages: list[dict]) -> object:
    collector = StreamCollector()
    async for chunk in client.stream_chat(messages, tools=None):
        collector.feed(chunk)
    return collector.finalize_checked()


# === 差异 1:stop_reason 归一 ===

async def test_stop_reason_max_tokens_maps_to_length() -> None:
    """⚠️ 承重:`max_tokens → "length"` —— 上层靠它判定截断并广播 truncated 事件。

    若不映射,`finish_reason` 会是 `"max_tokens"`,`agent/loop.py` 的
    `== "length"` 判定永远不成立 → **输出被截断时用户完全看不到提示**。
    """
    stream = _ev("message_delta", {"delta": {"stop_reason": "max_tokens"},
                                   "usage": {"output_tokens": 3}})
    msg, _ = await _collect(_client(stream), [])
    assert msg.finish_reason == "length"

    stream = _ev("message_delta", {"delta": {"stop_reason": "end_turn"},
                                   "usage": {"output_tokens": 3}})
    msg, _ = await _collect(_client(stream), [])
    assert msg.finish_reason == "stop"


async def test_stop_reason_tool_use_maps_to_tool_calls() -> None:
    stream = _ev("message_delta", {"delta": {"stop_reason": "tool_use"},
                                   "usage": {"output_tokens": 3}})
    msg, _ = await _collect(_client(stream), [])
    assert msg.finish_reason == "tool_calls"


# === 差异 2:usage 分两次到,必须累加 ===

async def test_usage_accumulated_not_clobbered() -> None:
    """⚠️ 承重:usage 分两次到达(`message_start` 给 input、`message_delta` 给 output)。

    `StreamCollector.feed` 对每个带 usage 的 chunk 是**直接覆盖**。若客户端照搬
    两个 usage 块,第二个会把 `prompt_tokens` **清成 0** —— token 统计静默失真,
    且不会有任何报错(这正是「指标看着有、实际不可信」的形态)。
    """
    msg, _ = await _collect(_client(_text_stream()), [])
    assert msg.content == "你好"
    # 客户端在末尾一次性产出完整 usage(不是两个互相覆盖的块)
    collector = StreamCollector()
    async for chunk in _client(_text_stream()).stream_chat([]):
        collector.feed(chunk)
    assert collector.usage is not None
    assert collector.usage.prompt_tokens == 11  # 未被 output 块清零
    assert collector.usage.completion_tokens == 7
    assert collector.usage.total_tokens == 18


async def test_usage_absent_when_no_counters() -> None:
    """流里没有 usage 时,不应产出一个全 0 的 usage chunk(那会污染存量统计)。"""
    collector = StreamCollector()
    async for chunk in _client(_text_stream()).stream_chat([]):
        collector.feed(chunk)
    # _text_stream 有 usage → 有;这里换一个完全没有 usage 的流
    plain = _ev("content_block_delta", {"index": 0, "delta": {
        "type": "text_delta", "text": "x"}})
    collector2 = StreamCollector()
    async for chunk in _client(plain).stream_chat([]):
        collector2.feed(chunk)
    assert collector2.usage is None


# === 差异 3:工具参数是 JSON 增量 ===

async def test_tool_use_input_json_delta_accumulated() -> None:
    """⚠️ 承重:`input_json_delta.partial_json` 分片必须拼成完整 arguments。

    参数是**跨分片**到达的(与 OpenAI 的 arguments 同形),StreamCollector 按
    content_block 的 index 分桶累加。若不透传 index,并行工具调用会坍缩成一个。
    """
    stream = (
        _ev("content_block_start", {"index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "read_file"}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"path": "a'}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '.py"}'}})
        + _ev("message_delta", {"delta": {"stop_reason": "tool_use"},
                                "usage": {"output_tokens": 5}})
    )
    msg, errors = await _collect(_client(stream), [])
    assert errors == []
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].id == "toolu_1"
    assert msg.tool_calls[0].name == "read_file"
    assert msg.tool_calls[0].arguments == '{"path": "a.py"}'


async def test_parallel_tool_use_kept_separate_by_index() -> None:
    """两个并行工具调用(不同 content_block index)→ 不得坍缩成一个。"""
    stream = (
        _ev("content_block_start", {"index": 0, "content_block": {
            "type": "tool_use", "id": "t1", "name": "read_file"}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"path":"a"}'}})
        + _ev("content_block_start", {"index": 1, "content_block": {
            "type": "tool_use", "id": "t2", "name": "list_dir"}})
        + _ev("content_block_delta", {"index": 1, "delta": {
            "type": "input_json_delta", "partial_json": '{"path":"b"}'}})
    )
    msg, _ = await _collect(_client(stream), [])
    assert [tc.name for tc in msg.tool_calls] == ["read_file", "list_dir"]
    assert [tc.arguments for tc in msg.tool_calls] == ['{"path":"a"}', '{"path":"b"}']


async def test_thinking_delta_maps_to_reasoning_content() -> None:
    stream = (
        _ev("content_block_start", {"index": 0, "content_block": {"type": "thinking"}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "thinking_delta", "thinking": "让我想想"}})
        + _ev("content_block_delta", {"index": 0, "delta": {
            "type": "text_delta", "text": "答案"}})
    )
    msg, _ = await _collect(_client(stream), [])
    assert msg.reasoning_content == "让我想想"
    assert msg.content == "答案"


# === 认证与请求外形 ===

async def test_request_headers_and_shape() -> None:
    """Anthropic 用 x-api-key + anthropic-version,URL 是 /v1/messages。"""
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.content.decode())
        return httpx.Response(200, text=_ev("message_stop", {}))

    client = AnthropicClient(_cfg(), transport=httpx.MockTransport(handler))
    async for _ in client.stream_chat([{"role": "user", "content": "hi"}], system="SYS"):
        pass
    assert str(seen["url"]).endswith("/v1/messages")
    headers = seen["headers"]  # type: ignore[assignment]
    assert headers["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in headers
    assert "authorization" not in headers  # 不用 Bearer
    body = seen["body"]  # type: ignore[assignment]
    assert body["system"] == "SYS"
    assert body["max_tokens"] == 4096  # Anthropic 必填


async def test_tools_payload_shape() -> None:
    """工具声明是扁平的 name/description/input_schema(非 OpenAI 的嵌套外形)。"""
    client = AnthropicClient(_cfg(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="")))
    out = client.tools_payload(
        {"read_file": {"description": "读文件",
                       "parameters": {"type": "object", "properties": {}}}}
    )
    assert out == [{"name": "read_file", "description": "读文件",
                    "input_schema": {"type": "object", "properties": {}}}]


# === provider 分派 ===

def test_build_stream_client_dispatches_by_provider() -> None:
    assert isinstance(build_stream_client(_cfg()), AnthropicClient)
    assert not isinstance(
        build_stream_client(_cfg(provider="openai", base_url="https://x", model="m")),
        AnthropicClient,
    )


# === 双实现:SDK 路径与 httpx 路径 ===

async def test_sdk_path_selected_when_available_and_no_transport() -> None:
    """未注入 transport 且 SDK 可用 → 走 SDK 路径。

    CI 若未装 anthropic 则整条跳过(不静默通过 —— 断言写在 skip 之外会误导)。
    """
    pytest.importorskip("anthropic")
    client = AnthropicClient(_cfg())  # 不传 transport
    assert client._use_sdk is True  # noqa: SLF001 - 白盒确认路径选择


async def test_mock_transport_forces_httpx_path() -> None:
    """⚠️ 承重:注入 transport 时**强制走 httpx**。

    MockTransport 是 httpx 的概念,SDK 无法接受。若这里不强制,CI 的离线测试会
    悄悄走 SDK 并对真实网络发请求(或直接报错)—— 离线保证就破了。
    """
    client = _client(_text_stream())  # 传了 transport
    assert client._use_sdk is False  # noqa: SLF001


async def test_sdk_and_httpx_produce_same_chunks(monkeypatch) -> None:
    """⚠️ 承重:两条实现产出**同一形状**的事件流(共用翻译器)。

    这是「装没装 SDK 行为一致」的结构性保证。做法:把 SDK 路径的事件源换成
    与 httpx 路径同样的字典序列,断言两者产出的 StreamChunk 完全相等 ——
    若哪天有人给 SDK 路径单独写一套翻译,这条会红。
    """
    stream = _text_stream()
    httpx_client = _client(stream)

    # 手工解析出 httpx 路径会看到的事件序列,喂给 SDK 路径的翻译器
    events: list[tuple[str, dict]] = []
    event = ""
    for line in stream.splitlines():
        s = line.strip()
        if s.startswith("event:"):
            event = s[len("event:") :].strip()
        elif s.startswith("data:"):
            parsed = parse_anthropic_event(event, s[len("data:") :])
            if parsed:
                events.append((parsed["type"], parsed["data"]))
            event = ""

    sdk_client = _client(stream)

    async def _via_sdk() -> list[object]:
        sdk_client._prompt_tokens = 0  # noqa: SLF001
        sdk_client._completion_tokens = 0  # noqa: SLF001
        out = [c async for c in sdk_client._translate_events(list(events))]  # noqa: SLF001
        usage = sdk_client._usage_chunk()  # noqa: SLF001
        return [*out, usage] if usage is not None else out

    async def _via_httpx() -> list[object]:
        return [c async for c in httpx_client.stream_chat([])]

    assert await _via_sdk() == await _via_httpx()


# === 消息转换(to_anthropic_messages) ===

def test_system_extracted_to_top_level() -> None:
    """system 必须是顶层参数,且从 messages 里剔除。"""
    system, msgs = to_anthropic_messages([
        AgentMessage(role="system", content="你是助手"),
        AgentMessage(role="user", content="hi"),
    ])
    assert system == "你是助手"
    assert msgs == [{"role": "user", "content": "hi"}]


def test_adjacent_tool_results_merged_into_one_user_message() -> None:
    """⚠️ 承重:相邻 tool 结果必须合并成**一条** user 消息的多个 block。

    本项目对每个工具结果各 append 一条 role="tool" 消息;并行工具一轮就会产生
    连续多条。朴素转成连续 role:"user" 会触发 Anthropic 的
    「messages: roles must alternate」400 —— 即**并行工具调用必崩**。
    """
    _, msgs = to_anthropic_messages([
        AgentMessage(role="user", content="任务"),
        AgentMessage(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", name="read_file", arguments='{"path":"a"}'),
            ToolCall(id="t2", name="list_dir", arguments='{"path":"b"}'),
        ]),
        AgentMessage(role="tool", content="A 的内容", tool_call_id="t1"),
        AgentMessage(role="tool", content="B 的内容", tool_call_id="t2"),
    ])
    # 两个 tool 结果 → **一条** user 消息,内含两个 tool_result block
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert len(user_msgs) == 2  # 原始任务 + 合并后的结果(而非 3 条)
    merged = user_msgs[-1]["content"]
    assert isinstance(merged, list) and len(merged) == 2
    assert [b["tool_use_id"] for b in merged] == ["t1", "t2"]
    # role 严格交替:不得出现连续两条同 role
    roles = [m["role"] for m in msgs]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles


def test_tool_result_carries_is_error() -> None:
    """is_error 必须透传(Anthropic 有原生错误标记;丢了模型只能从正文猜)。"""
    _, msgs = to_anthropic_messages([
        AgentMessage(role="tool", content="失败了", tool_call_id="t1", is_error=True),
    ])
    block = msgs[0]["content"][0]  # type: ignore[index]
    assert block["type"] == "tool_result"
    assert block["is_error"] is True


def test_assistant_tool_use_arguments_decoded_to_object() -> None:
    """Anthropic 的 tool_use.input 是**对象**,不是 JSON 字符串。"""
    _, msgs = to_anthropic_messages([
        AgentMessage(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", name="read_file", arguments='{"path":"a.py"}')]),
    ])
    block = msgs[0]["content"][0]  # type: ignore[index]
    assert block["type"] == "tool_use"
    assert block["input"] == {"path": "a.py"}  # 已解码


def test_invalid_arguments_fall_back_to_empty_object() -> None:
    """坏参数不抛异常 —— 它是既有场景(参数校验失败后那条 assistant 消息仍会被重发)。

    退化为空对象让对话能继续,由模型自行修正;抛异常会让整个任务崩掉。
    """
    _, msgs = to_anthropic_messages([
        AgentMessage(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", name="read_file", arguments='{"path": "a.py')]),
    ])
    block = msgs[0]["content"][0]  # type: ignore[index]
    assert block["input"] == {}


# === 解析器边界 ===

def test_parse_anthropic_event_skips_blank_and_done() -> None:
    assert parse_anthropic_event("", "") is None
    assert parse_anthropic_event("", "   ") is None
    assert parse_anthropic_event("", "[DONE]") is None


def test_parse_anthropic_event_falls_back_to_payload_type() -> None:
    """event: 行缺失时回退到 payload 的 type 字段(流式协议两种都有)。"""
    parsed = parse_anthropic_event("", '{"type":"message_stop"}')
    assert parsed is not None and parsed["type"] == "message_stop"


def test_parse_anthropic_event_rejects_bad_json() -> None:
    with pytest.raises(ValueError, match="非合法 JSON"):
        parse_anthropic_event("message_stop", "{not json")


# === 与既有 OpenAI 路径的兼容 ===

async def test_openai_client_unchanged_shape() -> None:
    """OpenAI 客户端的 serialize_messages 返回 ({} , messages) —— 与 Anthropic 同形。"""
    from agent.client import StreamingClient

    cfg = _cfg(provider="openai", base_url="https://api.deepseek.com", model="deepseek-chat")
    client = StreamingClient(cfg)
    extra, msgs = client.serialize_messages([AgentMessage(role="user", content="hi")])
    assert extra == {}
    assert msgs == [{"role": "user", "content": "hi"}]
