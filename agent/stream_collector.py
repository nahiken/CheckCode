"""StreamCollector —— 流式 Tool Use 事件聚合器。

把流式增量(文本/思考/工具参数)聚合成一回合完整的 AssistantMessage。

核心安全语义:
- 工具参数通过流式增量拼装,可能不完整或非法(JSON 截断、类型错误)。
- 本模块不执行任何工具;只负责聚合**与参数合法性校验**。
- `finalize_checked` 返回聚合结果 **+ 逐个非法参数的错误**;参数非法时必须把它们
  作为错误 Observation 回灌,而不是直接执行。`finalize` 是不校验的旧入口(等价于
  `finalize_checked` 的 msg 部分),保留给只关心聚合的调用方。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent.types import AssistantMessage, StreamChunk, ToolCall, ToolCallChunk, Usage


@dataclass(frozen=True)
class ParamError:
    """一个工具调用的参数非法现场(供回灌错误 Observation 用)。

    定型在本模块(而不是让每个调用方各自 `str(异常)`):错误文案是模型自我修正的
    输入,必须只有一处措辞;`loop` 与执行层兜底共用同一来源。
    """

    tool_call_id: str
    tool_name: str
    reason: str  # 校验失败原因(用户/模型可读)
    arguments: str  # 原始参数串(截断前,便于排查)


class StreamCollector:
    """聚合单个回合的流式增量。

    - feed(chunk): 逐块喂入。
    - finalize_checked(): 返回 (聚合消息, 非法参数列表) —— 带校验的推荐入口。
    - finalize(): 仅返回聚合消息(不校验;委托给 finalize_checked)。
    - 对每次模型调用请新建实例(内部状态不可复用)。
    - usage:include_usage 的 usage-only 块 → 记录该回合 token 用量(评测/前端消费)。
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_call_builders: dict[int, ToolCallChunk] = {}
        self._finish_reason: str | None = None
        self.usage: Usage | None = None  # 该回合 token 用量(有则记录)

    def feed(self, chunk: StreamChunk) -> None:
        if chunk.usage is not None:
            self.usage = chunk.usage  # usage-only 块覆盖记录(通常每个回合一个)
        delta = chunk.delta
        if delta.content:
            self._content_parts.append(delta.content)
        if delta.reasoning_content:
            self._reasoning_parts.append(delta.reasoning_content)
        if delta.tool_calls:
            for tc in delta.tool_calls:
                index = tc.index if tc.index is not None else 0
                builder = self._tool_call_builders.setdefault(index, ToolCallChunk())
                if tc.id:
                    builder.id = tc.id
                if tc.name:
                    builder.name = tc.name
                if tc.arguments:
                    builder.arguments += tc.arguments
        if chunk.finish_reason:
            self._finish_reason = chunk.finish_reason

    def finalize_checked(self) -> tuple[AssistantMessage, list[ParamError]]:
        """聚合 + 校验:返回 (消息, 非法参数列表)。

        校验在此定型,使「聚合」与「拒绝执行」是同一个模块的同一件事 —— 这也是
        模块自述的能力边界(见文件头)。非法调用的判定**只做一次**,AgentLoop 与
        执行层兜底都消费同一结果,不在两处各写一套 JSON 解析。
        """
        msg = self._aggregate()
        errors: list[ParamError] = []
        for tc in msg.tool_calls:
            try:
                parse_tool_arguments(tc.arguments)
            except ValueError as e:
                errors.append(
                    ParamError(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        reason=str(e),
                        arguments=tc.arguments,
                    )
                )
        return msg, errors

    def finalize(self) -> AssistantMessage:
        """返回聚合后的消息(不校验参数合法性)。

        兼容入口:等价于 `finalize_checked()[0]`。需要拒绝非法调用的调用方应改用
        `finalize_checked`(AgentLoop 即如此)。
        """
        return self._aggregate()

    def _aggregate(self) -> AssistantMessage:
        """把已喂入的增量拼成一回合消息(无校验)。"""
        tool_calls = []
        for index in sorted(self._tool_call_builders):
            b = self._tool_call_builders[index]
            tool_calls.append(
                ToolCall(
                    id=b.id or f"call_{index}",
                    name=b.name or "",
                    arguments=b.arguments,
                    index=index,
                )
            )
        return AssistantMessage(
            content="".join(self._content_parts),
            reasoning_content="".join(self._reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=self._finish_reason,
        )


def param_error_observation(err: ParamError) -> str:
    """非法参数的错误 Observation 文案(单一来源)。

    loop(主路径)与执行层兜底都用它,保证模型看到同一种措辞 —— 内容随上下文微调
    时不会出现两套说法。
    """
    return f"参数校验失败,拒绝执行: {err.reason}"


def parse_tool_arguments(arguments: str) -> dict[str, object]:
    """解析工具参数字符串。非法/不完整时抛 ValueError(不静默降级)。"""
    if not arguments or not arguments.strip():
        raise ValueError("工具参数为空")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as e:
        raise ValueError(f"工具参数不是合法 JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return parsed
