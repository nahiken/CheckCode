"""M10 单元测试:推理通道配置(LLM_REASONING)。

覆盖:
- LLM_REASONING 环境变量 → supports_reasoning;空串/未设置 → False
- "0"/"false"/"no"/"off" 视为关闭;其余非空值视为开启
- 推理模型下请求体**省略** temperature(非推理模型保留)

推理通道的**聚合**行为(StreamCollector 收 reasoning_content)已在
tests/agent/test_stream_collector.py 覆盖;本文件只验 M10 新增的配置/请求契约。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.client import StreamingClient
from agent.config import LLMConfig, get_llm_config
from agent.types import AssistantMessage
from tests.agent.helpers import finish, reasoning_delta, text_delta

_REASONING_VARS = ("LLM_REASONING", "LLM_API_KEY", "LLM_MODEL", "DEEPSEEK_API_KEY")


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _REASONING_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "k")  # StreamingClient 需要非空 key


def test_supports_reasoning_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置 LLM_REASONING → supports_reasoning=False(默认非推理)。"""
    _clear(monkeypatch)
    assert get_llm_config().supports_reasoning is False


def test_supports_reasoning_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_REASONING=1 → supports_reasoning=True。"""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", "1")
    assert get_llm_config().supports_reasoning is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "FALSE", "Off"])
def test_supports_reasoning_falsy_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """空串与 0/false/no/off(忽略大小写)→ False。"""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", raw)
    assert get_llm_config().supports_reasoning is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "True"])
def test_supports_reasoning_truthy_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", raw)
    assert get_llm_config().supports_reasoning is True


def _capture_payload(config: LLMConfig) -> dict[str, object]:
    """用 MockTransport 抓取请求体(不发真实请求)。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, text=text_delta("ok") + finish())

    client = StreamingClient(config, transport=httpx.MockTransport(handler))

    async def _run() -> None:
        async for _ in client.stream_chat([{"role": "user", "content": "hi"}]):
            pass

    import asyncio

    asyncio.run(_run())
    return captured


def test_temperature_sent_for_non_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """非推理模型:请求体含 temperature(保持原行为)。"""
    _clear(monkeypatch)
    cfg = get_llm_config(temperature=0.3)
    payload = _capture_payload(cfg)
    assert payload["temperature"] == 0.3


def test_temperature_omitted_for_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理模型:请求体**省略** temperature(避免部分 provider 400)。"""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", "1")
    cfg = get_llm_config(temperature=0.3)
    payload = _capture_payload(cfg)
    assert "temperature" not in payload


def test_reasoning_channel_aggregated_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """推理模型流式返回 reasoning_content → StreamCollector 聚合为 AssistantMessage。"""
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_REASONING", "1")
    cfg = get_llm_config()
    stream = reasoning_delta("让我想想") + reasoning_delta("……") + text_delta("答案") + finish()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream)

    from agent.stream_collector import StreamCollector

    async def _run() -> AssistantMessage:
        client = StreamingClient(cfg, transport=httpx.MockTransport(handler))
        collector = StreamCollector()
        async for chunk in client.stream_chat([{"role": "user", "content": "hi"}]):
            collector.feed(chunk)
        return collector.finalize()

    import asyncio

    msg = asyncio.run(_run())
    assert msg.reasoning_content == "让我想想……"
    assert msg.content == "答案"
