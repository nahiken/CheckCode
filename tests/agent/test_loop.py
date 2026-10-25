"""M1 集成测试:ReAct 主循环(使用 httpx.MockTransport 模拟流式响应)。

覆盖:
- 单轮无工具调用 → 直接返回最终回答
- 读→写→读回 的多轮工具循环
- 非法参数 → 错误 Observation 回灌,不执行
- 全非法参数 → 自动终止(不触发执行)
- max_steps 上限
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agent.client import StreamingClient
from agent.config import LLMConfig
from agent.loop import AgentLoop, StepEvent
from agent.types import AgentResult
from tests.agent.helpers import (
    finish,
    make_mock_loop,
    noop_dispatch,
    reasoning_delta,
    text_delta,
    tool_delta,
    usage_block,
)

SYSTEM = "你是测试助手"


def _make_loop(
    requests: list[str],
    dispatch: Callable[[str, dict[str, object]], Awaitable[AgentResult]] | None = None,
    max_steps: int = 20,
) -> AgentLoop:
    """根据预置的请求-响应序列构造 AgentLoop(mock transport)。"""
    return make_mock_loop(requests, dispatch=dispatch, max_steps=max_steps)


async def test_single_turn_no_tools() -> None:
    payload = text_delta("你好") + finish() + usage_block()
    loop = _make_loop([payload])
    messages = await loop.run("打招呼")
    last = messages[-1]
    assert last.role == "assistant"
    assert last.content == "你好"
    assert last.tool_calls == []


async def test_read_write_read_back_loop() -> None:
    """读→写→读回:三轮工具调用后模型给出最终回答。"""
    # 请求1: assistant 发起 read_file
    r1 = tool_delta(0, id_="call_1", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    # 请求2: assistant 发起 write_file
    r2 = tool_delta(0, id_="call_2", name="write_file") + tool_delta(0, args='{"path":"a.py","content":"# new"}') + finish("tool_calls") + usage_block()
    # 请求3: 无工具调用,输出最终回答
    r3 = text_delta("完成") + finish() + usage_block()

    seen: list[StepEvent] = []
    dispatch_history: list[tuple[str, dict[str, object]]] = []

    async def dispatch(name: str, args: dict[str, object]) -> AgentResult:
        dispatch_history.append((name, args))
        return AgentResult(content=f"{name} done")

    loop = _make_loop([r1, r2, r3], dispatch=dispatch)
    messages = await loop.run("读取并写入", events=lambda ev: _record(seen, ev))

    # 最终回答
    assert messages[-1].content == "完成"
    # 工具调用顺序符合模型调用顺序
    assert [d[0] for d in dispatch_history] == ["read_file", "write_file"]
    # 事件流里记录了 tool_result
    assert any(ev.kind == "tool_result" for ev in seen)


async def _record(seen: list[StepEvent], ev: StepEvent) -> None:
    seen.append(ev)


async def test_invalid_params_rejected_and_not_executed() -> None:
    """模型给出非法 JSON 参数 → 转错误 Observation,不执行,模型修正。"""
    # 请求1: 非法参数
    r1 = tool_delta(0, id_="call_bad", name="read_file") + tool_delta(0, args='{"path":') + finish("tool_calls") + usage_block()
    # 请求2: 修正为合法调用
    r2 = tool_delta(0, id_="call_ok", name="write_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    # 请求3: 完成
    r3 = text_delta("完成") + finish() + usage_block()

    executed: list[str] = []

    async def dispatch(name: str, args: dict[str, object]) -> AgentResult:
        executed.append(name)
        return AgentResult(content="ok")

    loop = _make_loop([r1, r2, r3], dispatch=dispatch)
    seen: list[StepEvent] = []
    messages = await loop.run("任务", events=lambda ev: _record(seen, ev))

    # 非法 read_file 未被执行,只有修正后的 write_file 被执行一次
    assert executed == ["write_file"]
    assert any(ev.kind == "invalid_params" for ev in seen)
    # 消息历史中先出现"参数校验失败"的 tool 消息,再出现模型修正后的 assistant 回合
    tool_msgs = [m for m in messages if m.role == "tool" and "参数校验失败" in m.content]
    assert len(tool_msgs) == 1
    # 最终回答
    assert messages[-1].content == "完成"


async def test_all_invalid_params_feeds_back_error_and_continues() -> None:
    """模型只给出非法参数 → 回灌错误 Observation,继续循环让模型修正。"""
    # 请求1: 非法参数
    r1 = tool_delta(0, id_="call_bad", name="read_file") + tool_delta(0, args="not json") + finish("tool_calls") + usage_block()
    # 请求2: 模型收到错误 Observation 后给出最终回答(不再调工具)
    r2 = text_delta("抱歉,我修正了") + finish() + usage_block()
    executed: list[str] = []

    async def dispatch(name: str, args: dict[str, object]) -> AgentResult:
        executed.append(name)
        return AgentResult(content="should not happen")

    loop = _make_loop([r1, r2], dispatch=dispatch)
    seen: list[StepEvent] = []
    messages = await loop.run("任务", events=lambda ev: _record(seen, ev))
    assert executed == []
    # 回灌了错误 Observation
    assert any(m.role == "tool" and "参数校验失败" in m.content for m in messages)
    # 继续循环后模型给出最终回答
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "抱歉,我修正了"


async def test_max_steps_limit() -> None:
    """模型每轮都发起工具调用且从不完成 → 达到 max_steps 后终止。"""
    # 每轮: assistant 发起合法工具调用(read_file)
    r_tool = tool_delta(0, id_="call_x", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    loop = _make_loop([r_tool] * 5, dispatch=None, max_steps=3)
    seen: list[StepEvent] = []
    await loop.run("任务", events=lambda ev: _record(seen, ev))
    # max_steps 上限生效
    assert any(ev.kind == "max_steps" for ev in seen)
    # 循环次数受 max_steps 约束
    model_calls = [ev for ev in seen if ev.kind == "model_call"]
    assert len(model_calls) == 3


# === 新增:完整事件序列 / 全非法参数到 max_steps / tool_call_id 回归 ===

async def test_full_event_sequence_read_write_finish() -> None:
    """验证 loop 在 读→写→完成 三轮中产生的完整 StepEvent 序列(顺序敏感)。"""
    r1 = tool_delta(0, id_="call_1", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r2 = tool_delta(0, id_="call_2", name="write_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r3 = text_delta("完成") + finish() + usage_block()

    async def dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        return AgentResult(content="ok")

    loop = _make_loop([r1, r2, r3], dispatch=dispatch)
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    await loop.run("任务", events=record)
    assert seen == [
        "model_call",
        "assistant",
        "tool_exec_start",
        "tool_result",
        "model_call",
        "assistant",
        "tool_exec_start",
        "tool_result",
        "model_call",
        "assistant",
        "finish",
    ]


async def test_invalid_event_sequence_all_invalid_then_finish() -> None:
    """非法参数轮:model_call → invalid_params → all_invalid →(下一轮)model_call → finish。"""
    r1 = tool_delta(0, id_="bad", name="read_file") + tool_delta(0, args="not json") + finish("tool_calls") + usage_block()
    r2 = text_delta("好了") + finish() + usage_block()

    async def dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        raise AssertionError("不应执行")

    loop = _make_loop([r1, r2], dispatch=dispatch)
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    await loop.run("任务", events=record)
    assert seen == [
        "model_call",
        "assistant",
        "invalid_params",
        "all_invalid",
        "model_call",
        "assistant",
        "finish",
    ]


async def test_token_usage_accumulated_in_usage_event() -> None:
    """include_usage 的 usage-only 块 → usage 事件(record_event)带累计 token(M8 token 记账)。

    注意:usage 事件走 record_event(记账),不走调用方 events(实时 SSE 不含 usage,
    与既有事件序列断言保持一致)。评测 runner 通过 record_event 采集 token。
    """
    # 两个回合:回合1 调工具(继续循环)→ 回合2 完成。每回合 usage_block(prompt=1, completion=1)
    r1 = tool_delta(0, id_="call_1", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()
    request_iter = iter([r1, r2])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=next(request_iter)))
    config = LLMConfig(base_url="http://test", api_key="k", model="m")
    usage_events: list[StepEvent] = []

    async def record(ev: StepEvent) -> None:
        if ev.kind == "usage":
            usage_events.append(ev)

    loop = AgentLoop(
        StreamingClient(config, transport=transport),
        {"read_file": {"description": "读文件", "parameters": {"type": "object", "properties": {}}}},
        noop_dispatch,
        system_prompt=SYSTEM,
        max_steps=5,
        record_event=record,
    )
    await loop.run("任务")
    assert len(usage_events) == 2  # 每回合一次 usage 事件
    # usage 事件携带累计 token:每回合 prompt=1, completion=1 → 最后 total=4
    last_usage = usage_events[-1]
    assert last_usage.data["prompt_tokens"] == 2
    assert last_usage.data["completion_tokens"] == 2
    assert last_usage.data["total_tokens"] == 4


async def test_all_invalid_params_until_max_steps() -> None:
    """模型每轮都只给非法参数、从不修正 → 持续回灌错误 Observation 直到 max_steps。

    关键断言:dispatch 全程不被调用;错误 Observation 随轮次累积;max_steps 触发。
    """
    r_invalid = tool_delta(0, id_="bad", name="read_file") + tool_delta(0, args="not json") + finish("tool_calls") + usage_block()
    loop = _make_loop([r_invalid] * 5, dispatch=None, max_steps=3)
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    messages = await loop.run("任务", events=record)
    # dispatch 默认 noop_dispatch,若被执行会返回 "ok";这里校验非法调用从不执行
    assert seen.count("invalid_params") == 3
    assert seen.count("all_invalid") == 3
    assert seen[-1] == "max_steps"
    # 每轮回灌 1 条错误 Observation,共 3 条
    error_tools = [m for m in messages if m.role == "tool" and "参数校验失败" in m.content]
    assert len(error_tools) == 3


# === 新增:truncated(finish_reason=length)与混排回灌顺序 ===

async def test_truncated_finish_emits_truncated_event() -> None:
    """max_tokens 截断(finish_reason=length)应触发 truncated 事件,不当作自然完成。"""
    payload = text_delta("半截回答") + finish("length") + usage_block()
    loop = _make_loop([payload])
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    await loop.run("任务", events=record)
    # truncated 事件在 finish 之前触发
    assert "truncated" in seen
    assert seen[-1] == "finish"


async def test_mixed_valid_invalid_keeps_model_order() -> None:
    """非法与合法调用混排时,回灌顺序与 assistant.tool_calls 一致。

    模型发 [合法a, 非法b, 合法c],tool 消息顺序必须是 [a结果, b错误, c结果]。
    """
    # 一个 assistant 回合发 3 个 tool_calls: index 0 合法、1 非法、2 合法
    r1 = (
        tool_delta(0, id_="call_a", name="read_file", args='{"path":"a.py"}')
        + tool_delta(1, id_="call_b", name="read_file", args="not json")
        + tool_delta(2, id_="call_c", name="write_file", args='{"path":"c.py"}')
        + finish("tool_calls")
        + usage_block()
    )
    r2 = text_delta("完成") + finish() + usage_block()
    executed: list[str] = []

    async def dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        executed.append(name)
        return AgentResult(content=f"{name}:ok")

    loop = _make_loop([r1, r2], dispatch=dispatch)
    messages = await loop.run("任务")
    # 只有合法调用被执行,且按模型顺序
    assert executed == ["read_file", "write_file"]
    # tool 消息顺序: a结果 → b错误 → c结果(与模型调用顺序一致)
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 3
    assert "read_file:ok" in tool_msgs[0].content  # a 合法
    assert "参数校验失败" in tool_msgs[1].content  # b 非法
    assert "write_file:ok" in tool_msgs[2].content  # c 合法


async def test_max_steps_single_call() -> None:
    """max_steps=1:只发起一次模型调用即终止,无最终回答。"""
    r_tool = tool_delta(0, id_="call_x", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    loop = _make_loop([r_tool], dispatch=None, max_steps=1)
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    messages = await loop.run("任务", events=record)
    # M4 新增 assistant 事件(每轮模型输出后发出,持久化完整消息含 tool_calls)
    assert seen == ["model_call", "assistant", "tool_exec_start", "tool_result", "max_steps"]
    # 终止时历史以最后一个工具结果(tool)结尾,无关闭性 assistant 回合
    assert messages[-1].role == "tool"
    assert messages[-1].tool_call_id == "call_x"
    # 历史里存在带 tool_calls 的 assistant 回合(未得到最终回答)
    assert any(m.role == "assistant" and len(m.tool_calls) == 1 for m in messages)


async def test_loop_tool_call_id_forced_from_request_regression() -> None:
    """【回归】loop 层:tool 消息的 tool_call_id 强制使用请求 id。

    协议硬约束:tool 消息的 tool_call_id 必须与 assistant 的 tool_calls id 精确匹配,
    provider 会拒绝不匹配的请求。真实 DeepSeek 调用暴露过该问题。
    """
    r1 = tool_delta(0, id_="call_1", name="read_file") + tool_delta(0, args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()

    async def ok_dispatch(name: str, args: dict[str, Any]) -> AgentResult:
        return AgentResult(content="ok")

    loop = _make_loop([r1, r2], dispatch=ok_dispatch)
    messages = await loop.run("任务")
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1"  # 强制用请求 id


async def test_thinking_event_and_reasoning_propagated() -> None:
    """DeepSeek 思考增量触发 thinking 事件,reasoning_content 传播到消息历史。"""
    r1 = reasoning_delta("先分析问题") + reasoning_delta(", 再读文件") + tool_delta(0, id_="c1", name="read_file", args='{"path":"a.py"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()
    loop = _make_loop([r1, r2])
    seen: list[str] = []

    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)

    messages = await loop.run("任务", events=record)
    # thinking 事件在 tool_exec 之前
    assert "thinking" in seen
    assert seen.index("thinking") < seen.index("tool_exec_start")
    # assistant 消息携带 reasoning_content
    asst = [m for m in messages if m.role == "assistant"][0]
    assert asst.reasoning_content == "先分析问题, 再读文件"
    assert asst.content == ""


# === M9 W5:token 水位压缩集成(超预算时在构造请求前压缩当前消息列表) ===

async def test_w5_token_budget_triggers_compression_before_request() -> None:
    """token_budget 设置且估算超预算 → 请求构造前压缩早期工具回合。

    通过 MockTransport 捕获每次请求的 messages 长度,断言超预算后
    发送给模型的 messages 被压缩到远小于未压缩长度;循环正常完成。
    """
    captured_lengths: list[int] = []
    # 两个回合:回合1 长工具调用(继续循环)→ 回合2 完成
    long_args = '{"path":"' + "x" * 600 + '"}'
    r1 = tool_delta(0, id_="call_1", name="read_file", args=long_args) + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()
    request_iter = iter([r1, r2])

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        captured_lengths.append(len(body["messages"]))
        return httpx.Response(200, text=next(request_iter))

    transport = httpx.MockTransport(handler)
    config = LLMConfig(base_url="http://test", api_key="k", model="m")

    async def ok_dispatch(name: str, args: dict[str, object]) -> AgentResult:
        return AgentResult(content="ok")

    loop = AgentLoop(
        StreamingClient(config, transport=transport),
        {
            "read_file": {
                "description": "读文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        ok_dispatch,
        system_prompt=SYSTEM,
        max_steps=5,
        # 极低预算强制第二回合模型调用前触发 token 压缩
        token_budget=200,
    )
    messages = await loop.run("任务")
    assert len(captured_lengths) == 2
    # 第二轮请求构造前,消息被压缩到小集合(system + user 恒保留)
    second_payload = captured_lengths[1]
    assert second_payload >= 2  # system + user 恒保留
    assert second_payload < 10  # 长历史被压缩到小集合(未压缩会远大于此)
    # 压缩仍成对完整、循环正常返回最终回答
    assert messages[-1].role == "assistant"


# === M9 W4:parallel_safe 元数据(只读归组并发/写是串行屏障) ===

def _w4_spec() -> dict[str, dict[str, Any]]:
    """带 parallel_safe 元数据的工具规格:read_file 安全、write_file 不安全。"""
    return {
        "read_file": {
            "description": "读文件",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            "parallel_safe": True,
        },
        "write_file": {
            "description": "写文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}}
            },
            "parallel_safe": False,
        },
    }


async def test_w4_parallel_safe_reads_run_concurrently() -> None:
    """同轮 3 个 read_file(max_concurrency=3)→ 归组同一批并发执行(峰值水位=3)。"""
    r1 = (
        tool_delta(0, id_="call_0", name="read_file", args='{"path":"a.py"}')
        + tool_delta(1, id_="call_1", name="read_file", args='{"path":"b.py"}')
        + tool_delta(2, id_="call_2", name="read_file", args='{"path":"c.py"}')
        + finish("tool_calls")
        + usage_block()
    )
    r2 = text_delta("完成") + finish() + usage_block()
    request_iter = iter([r1, r2])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=next(request_iter)))
    config = LLMConfig(base_url="http://test", api_key="k", model="m")

    lock = asyncio.Lock()
    running: list[str] = []
    peak = 0

    async def slow_read(name: str, args: dict[str, object]) -> AgentResult:
        nonlocal peak
        async with lock:
            running.append(name)
            peak = max(peak, len(running))
        await asyncio.sleep(0.05)  # 拉开窗口,让并发可被观察到
        async with lock:
            running.remove(name)
        return AgentResult(content=f"{name}:ok")

    loop = AgentLoop(
        StreamingClient(config, transport=transport),
        _w4_spec(),
        slow_read,
        system_prompt=SYSTEM,
        max_steps=5,
        max_concurrency=3,  # 只读可并发:安全调用应归组到同一批
    )
    messages = await loop.run("任务")
    assert peak == 3  # 3 个只读调用确实并发执行(同一批)
    assert messages[-1].role == "assistant"


async def test_w4_write_is_serial_barrier_and_order_preserved() -> None:
    """mixed [read,write,read,read] @2 → 写独立成组(串行屏障),只读归组并发,顺序保持。"""
    r1 = (
        tool_delta(0, id_="call_0", name="read_file", args='{"path":"a.py"}')
        + tool_delta(1, id_="call_1", name="write_file", args='{"path":"b.py","content":"x"}')
        + tool_delta(2, id_="call_2", name="read_file", args='{"path":"c.py"}')
        + tool_delta(3, id_="call_3", name="read_file", args='{"path":"d.py"}')
        + finish("tool_calls")
        + usage_block()
    )
    r2 = text_delta("完成") + finish() + usage_block()
    request_iter = iter([r1, r2])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=next(request_iter)))
    config = LLMConfig(base_url="http://test", api_key="k", model="m")

    lock = asyncio.Lock()
    start_order: list[str] = []
    running: list[str] = []
    peak_read = 0
    read_overlap_with_write = 0

    async def dispatch(name: str, args: dict[str, object]) -> AgentResult:
        nonlocal peak_read, read_overlap_with_write
        async with lock:
            running.append(name)
            start_order.append(name)
            reads_running = [x for x in running if x != "write_file"]
            peak_read = max(peak_read, len(reads_running))
            if "write_file" in running and reads_running:
                read_overlap_with_write += 1
        # 读稍慢:若写与读同组并发,写执行期间必有读在跑(会被 read_overlap 捕获)
        await asyncio.sleep(0.05 if name == "read_file" else 0.02)
        async with lock:
            running.remove(name)
        return AgentResult(content=f"{name}:ok")

    loop = AgentLoop(
        StreamingClient(config, transport=transport),
        _w4_spec(),
        dispatch,
        system_prompt=SYSTEM,
        max_steps=5,
        max_concurrency=2,
    )
    messages = await loop.run("任务")
    # 只读归组并发:相邻 [read(call_2), read(call_3)] 同批 → 峰值并发读 = 2
    assert peak_read == 2
    # 写是串行屏障:写执行期间从无读并发
    assert read_overlap_with_write == 0
    # 执行顺序保持模型调用顺序(组间串行)
    assert start_order == ["read_file", "write_file", "read_file", "read_file"]
    assert messages[-1].role == "assistant"
