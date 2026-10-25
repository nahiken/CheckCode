"""context/compression.py —— 上下文窗口模拟 + 早期历史压缩。

目的:长任务中上下文逼近窗口上限时,压缩早期历史而不是丢弃整个会话。

语义(要点):压缩的对象是「早期回合」——一条含工具调用的 assistant 消息
与其 tool 结果必须成对剔除(否则模型会收到无来源的 tool 消息);system/user
始终保留(它们是任务约束,压缩掉会让后续回合失去上下文)。

双触发(M9 W5):
- token 预算:estimate_tokens(当前 messages 列表)超 AGENT_TOKEN_BUDGET →
  compress_to_budget 从最旧回合剔除直到估算 ≤ 预算;
- 消息数兜底:len(messages) > window_size → compress_history(历史语义,防回归)。

token 估算是启发式(chars//4 + 每消息/工具开销),非 tokenizer;作用于**当前消息列表**
而非 usage 累计值(累计值单调上涨,压缩后仍超预算会反复触发,无法作为压缩目标)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent.types import AgentMessage

# 每个角色消息的固定估算开销(tokens),近似 tokenizer 的每消息常数项
_PER_MESSAGE_OVERHEAD = 4
# 每个工具调用的固定估算开销(tokens)
_PER_TOOL_CALL_OVERHEAD = 8
# 每 4 个字符约 1 token 的中英文近似(启发式,非精确 tokenizer)
_CHARS_PER_TOKEN = 4


def estimate_tokens(messages: list[AgentMessage]) -> int:
    """估算当前消息列表的 token 用量(启发式)。

    基于文本长度 + 工具参数字符数 + 固定每消息/每工具开销;对中英文混合文本
    以 chars//4 近似。仅供压缩触发决策,不做精确计量。注入 dispatch 的测试路径
    可用文本长度为 0 或短文本使估算尽量小。
    """
    total = 0
    for m in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += (len(m.content) + len(m.reasoning_content)) // _CHARS_PER_TOKEN
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                total += _PER_TOOL_CALL_OVERHEAD
                total += len(tc.arguments) // _CHARS_PER_TOKEN
    return total


@dataclass
class ContextWindow:
    """上下文窗口:消息数 window_size 兜底 + token 预算 token_budget(M9 W5)。

    prompt/completion_tokens 仍记录**全会话累计**真实 usage(M8,只读/展示用);
    token 预算压缩触发不使用累计值(单调上涨无法作目标),而是估算当前消息列表。
    """

    window_size: int = 200
    token_budget: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_messages: int = 0

    def observe(self, messages: int) -> None:
        self.total_messages += messages

    def add_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """累计真实 token 用量(M8 接入:来自 StreamCollector.usage 的 usage-only 块)。"""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens


def _split_units(history: list[AgentMessage]) -> list[list[int]]:
    """把历史切成「回合单元」:assistant(带工具调用)+ 紧随其后引用其 call ids 的 tool 结果。

    返回索引单元列表,已按索引升序(旧 → 新)。system/user 及无工具调用的消息
    不构成单元(永不被剔除)。
    """
    n = len(history)
    units: list[list[int]] = []
    i = 0
    while i < n:
        m = history[i]
        if m.role == "assistant" and m.tool_calls:
            ids = {tc.id for tc in m.tool_calls}
            unit = [i]
            j = i + 1
            while j < n and history[j].role == "tool" and history[j].tool_call_id in ids:
                unit.append(j)
                j += 1
            units.append(unit)
            i = j
        else:
            i += 1
    return units


def compress_history(
    history: list[AgentMessage],
    window_size: int,
) -> list[AgentMessage]:
    """按消息数压缩早期历史(历史语义,防回归),返回新列表(不修改入参)。

    算法:从最旧开始剔除回合单元,直到消息数 ≤ window_size。system/user 永不剔除。
    成对完整性:剔除 assistant 时其 tool 结果一并剔除,保证模型上下文不出现
    无来源的 tool 消息(否则 API 校验会拒绝请求)。
    """
    if len(history) <= window_size:
        return list(history)

    n = len(history)
    units = _split_units(history)

    # 从最旧单元开始剔除,直到消息数 ≤ window_size
    kept = set(range(n))
    for unit in units:  # 已按索引升序(旧 → 新)
        if len(kept) <= window_size:
            break
        kept -= set(unit)

    return [history[i] for i in range(n) if i in kept]


def compress_to_budget(
    history: list[AgentMessage],
    token_budget: int,
    *,
    estimate: Callable[[list[AgentMessage]], int] = estimate_tokens,
) -> list[AgentMessage]:
    """按 token 预算压缩早期历史(M9 W5),返回新列表(不修改入参)。

    从最旧开始剔除回合单元,直到 estimate(剩余) ≤ token_budget。
    system/user 永不剔除;assistant 与其 tool 结果成对剔除(与 compress_history 同不变量)。
    estimate 恒为 0 或已 ≤ 预算时原样返回(不剔除任何内容)。
    """
    if token_budget <= 0 or estimate(history) <= token_budget:
        return list(history)

    n = len(history)
    units = _split_units(history)
    kept = set(range(n))
    for unit in units:  # 已按索引升序(旧 → 新)
        if estimate([history[i] for i in sorted(kept)]) <= token_budget:
            break
        kept -= set(unit)
        if len(kept) <= 0:
            break

    return [history[i] for i in range(n) if i in kept]
