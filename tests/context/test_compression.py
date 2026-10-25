"""context/ 单元测试:上下文窗口模拟 + 早期历史压缩。

不依赖 LLM/git。压缩的语义:逼近窗口上限时剔除早期含工具调用的 assistant 消息
(工具结果依赖工具调用,二者成对剔除),system/user 保留。
"""

from __future__ import annotations

from agent.types import AgentMessage, ToolCall, to_openai_messages
from context.compression import (
    ContextWindow,
    compress_history,
    compress_to_budget,
    estimate_tokens,
)


def _msg(
    role: str,
    content: str,
    *,
    tool_calls: list[ToolCall] | None = None,
    tool_call_id: str | None = None,
    reasoning_content: str = "",
) -> AgentMessage:
    return AgentMessage(
        role=role,  # type: ignore[arg-type]
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls or [],
        tool_call_id=tool_call_id,
    )


def _call(tc_id: str, name: str = "read_file") -> ToolCall:
    return ToolCall(id=tc_id, name=name, arguments="{}")


def test_c1_under_window_unchanged() -> None:
    """未超限 → 原样返回。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "第一回合"),
    ]
    compressed = compress_history(history, window_size=1000)
    assert compressed == history
    assert len(compressed) == 3


def test_c2_over_window_trims_early_assistant_with_tool_calls() -> None:
    """超限 → 剔除早期含 tool_calls 的 assistant 消息(及成对的 tool 结果)。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "我想用工具", tool_calls=[_call("c1"), _call("c2")]),
        _msg("tool", "结果1", tool_call_id="c1"),
        _msg("tool", "结果2", tool_call_id="c2"),
        _msg("assistant", "继续"),
    ]
    compressed = compress_history(history, window_size=4)
    # 窗口太小:剔除最旧工具回合(assistant c1/c2 + 结果),system/user + 最后 assistant 保留
    assert compressed[0].role == "system"
    assert compressed[1].role == "user"
    assert all(m.role != "assistant" or m.tool_calls == [] for m in compressed)
    assert "继续" in [m.content for m in compressed]


def test_c3_compressed_serializes_to_openai() -> None:
    """压缩结果可序列化为 OpenAI 消息格式(不抛异常)。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "回合", tool_calls=[_call("c1")]),
        _msg("tool", "结果", tool_call_id="c1"),
    ]
    compressed = compress_history(history, window_size=5)
    oai = to_openai_messages(compressed)
    assert isinstance(oai, list)
    assert oai[0]["role"] == "system"


def test_c4_tool_call_requires_matching_result_trimmed_pair() -> None:
    """剔除 assistant 时,其 tool_calls 引用的 tool 结果必须一并剔除(成对完整性)。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("tool", "结果", tool_call_id="c1"),
        _msg("assistant", "最终", tool_calls=[_call("c2")]),
        _msg("tool", "结果2", tool_call_id="c2"),
        _msg("assistant", "完成"),
    ]
    compressed = compress_history(history, window_size=5)
    # c1 成对被剔除(最旧单元);c2 成对保留(窗口内)
    tools = [m for m in compressed if m.role == "tool"]
    ids = [m.tool_call_id for m in tools if m.tool_call_id]
    assert ids == ["c2"]


def test_context_window_counts_messages() -> None:
    """ContextWindow 按消息数计量(阶段 8 再量化 token)。"""
    win = ContextWindow(window_size=10)
    assert win.prompt_tokens == 0  # 未使用 token 字段,恒为 0
    assert win.total_messages == 0
    win.observe(5)
    assert win.total_messages == 5


# === M9 W5:token 水位压缩(estimate_tokens + compress_to_budget + ContextWindow.token_budget) ===

def test_w5_estimate_tokens_is_deterministic_and_grows_with_content() -> None:
    """estimate_tokens 是确定性的启发式:文本越长估算越大,系统/用户/工具消息都有开销。"""
    short = [_msg("system", "s"), _msg("user", "u")]
    long = [_msg("system", "s" * 400), _msg("user", "u" * 400)]
    assert estimate_tokens(short) > 0
    assert estimate_tokens(long) > estimate_tokens(short)  # 文本增长 → 估算增长


def test_w5_estimate_counts_tool_arguments_and_reasoning() -> None:
    """工具参数字符与 reasoning_content 计入估算。"""
    plain = _msg("assistant", "无工具")
    with_tool = _msg(
        "assistant",
        "",
        tool_calls=[ToolCall(id="c1", name="read_file", arguments="x" * 400)],
    )
    assert estimate_tokens([plain]) < estimate_tokens([with_tool])
    reasoned = _msg("assistant", "", reasoning_content="r" * 400)
    assert estimate_tokens([plain]) < estimate_tokens([reasoned])


def test_w5_under_budget_returns_unchanged() -> None:
    """估算未超预算 → 原样返回(不剔除任何内容)。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("tool", "结果", tool_call_id="c1"),
    ]
    compressed = compress_to_budget(history, token_budget=10**6)
    assert compressed == history
    assert len(compressed) == 4


def test_w5_estimate_zero_returns_unchanged() -> None:
    """估算恒 0(无文本内容)→ 无剔除(不无限循环、不删空历史)。"""
    history = [
        _msg("system", ""),
        _msg("user", ""),
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("tool", "", tool_call_id="c1"),
    ]
    compressed = compress_to_budget(history, token_budget=10**6, estimate=lambda ms: 0)
    assert compressed == history  # estimate 0 ≤ 预算 → 原样


def test_w5_over_budget_trims_oldest_round_pairwise() -> None:
    """超预算 → 从最旧回合成对剔除,直到估算 ≤ 预算。"""
    # 4 个回合单元,每回合含 assistant(带 tool_calls)+ tool 结果
    history: list[AgentMessage] = [_msg("system", "sys"), _msg("user", "任务")]
    for i in range(4):
        tc = ToolCall(id=f"c{i}", name="read_file", arguments="x" * 80)  # ~20 token/参数
        history.append(_msg("assistant", "a" * 200, tool_calls=[tc]))  # ~50 token
        history.append(_msg("tool", "t" * 200, tool_call_id=f"c{i}"))  # ~50 token
    # 预算只够 ~3 回合:剔除最旧 1 回合(成对),保留 system/user + 后 3 回合
    compressed = compress_to_budget(history, token_budget=estimate_tokens(history) - 100)
    assert len(compressed) < len(history)
    # system/user 永不剔除
    assert compressed[0].role == "system"
    assert compressed[1].role == "user"
    # 剔除成对完整:无孤立 tool 结果(每个 tool 都能在保留的 assistant tool_calls 找到来源)
    kept_tool_ids = {m.tool_call_id for m in compressed if m.role == "tool"}
    kept_assistant_ids = {
        tc.id for m in compressed if m.role == "assistant" for tc in m.tool_calls
    }
    assert kept_tool_ids <= kept_assistant_ids
    # 剔除的是最旧回合(最早 c0 的 assistant/结果被移除)
    assert not any(m.tool_call_id == "c0" for m in compressed)
    assert estimate_tokens(compressed) <= estimate_tokens(history)


def test_w5_preserves_order_and_pairs_in_payload() -> None:
    """compress_to_budget 保序,且压缩后可序列化为 OpenAI 消息(不抛异常)。"""
    history = [
        _msg("system", "sys"),
        _msg("user", "任务"),
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("tool", "结果1", tool_call_id="c1"),
        _msg("assistant", "", tool_calls=[_call("c2")]),
        _msg("tool", "结果2", tool_call_id="c2"),
    ]
    compressed = compress_to_budget(history, token_budget=estimate_tokens(history) - 1)
    oai = to_openai_messages(compressed)
    assert isinstance(oai, list)
    roles = [o.get("role") for o in oai]
    assert roles[0] == "system"
    assert roles[1] == "user"
    # 顺序保持:无孤立 tool(每个 tool 前必有匹配 assistant)
    seen: set[str] = set()
    for m in compressed:
        if m.role == "assistant":
            seen |= {tc.id for tc in m.tool_calls}
        elif m.role == "tool":
            assert m.tool_call_id in seen


def test_w5_context_window_holds_token_budget() -> None:
    """ContextWindow 携带 token_budget 字段(None 默认)。"""
    win = ContextWindow(window_size=200)
    assert win.token_budget is None
    win_budget = ContextWindow(window_size=200, token_budget=16000)
    assert win_budget.token_budget == 16000
