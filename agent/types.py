"""Agent 核心数据模型。

遵循 OpenAI Chat Completions 协议:
- 系统提示词 system
- 消息 messages(role: system/user/assistant/tool)
- 工具调用 tool_calls(id, name, arguments JSON 字符串)
- 流式增量 delta(role / content / tool_calls)

本模块只定义类型,不包含任何执行逻辑。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    """模型发起的一次工具调用(参数为原始 JSON 字符串,未校验)。"""

    id: str
    name: str
    arguments: str = ""
    index: int = 0


class ToolCallChunk(BaseModel):
    """流式增量中的工具调用片段:参数可能不完整,由 StreamCollector 聚合。"""

    id: str | None = None
    name: str | None = None
    arguments: str = ""
    index: int | None = None


class Delta(BaseModel):
    """Chat Completions delta 增量。

    reasoning_content: DeepSeek 等 provider 流式输出的思考增量(对应"思考"事件)。
    """

    role: Role | None = None
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCallChunk] | None = None


class StreamChunk(BaseModel):
    """解析后的单个流式分块(透传 SSE 原始字段,方便测试注入)。"""

    index: int = 0
    delta: Delta = Field(default_factory=Delta)
    finish_reason: str | None = None
    logprobs: object | None = None
    usage: Usage | None = None  # include_usage 的 usage-only 块会携带 token 用量


class AssistantMessage(BaseModel):
    """完整聚合后的助手消息(一回合模型输出的最终形态)。"""

    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None

    @model_validator(mode="after")
    def _content_may_be_empty(self) -> AssistantMessage:
        # 纯工具调用回合允许 content 为空
        return self


class AgentResult(BaseModel):
    """一次工具调用的执行结果(dispatch 契约)。is_error=True 表示执行失败(错误 Observation)。

    注意与 tools/base.ToolResult(工具实现返回值,含 truncated)区分——名称不同避免歧义。
    tool_call_id:协议 id,由 agent/tool_calls.py 的 _run_single 在组装 Observation 时
    强制绑定请求的 tool.id(单一职责——工具层不知道协议 id,无需填写)。
    audit:本次调用的审批现场(ToolRunner._finish 附带 event.model_dump() 的字段;
    注入 dispatch 的测试路径可为空 dict)。供 M4 会话落盘复用审批边界——
    **每个工具拿自己的事件**,避免单槽 last_event 在多工具调用时错配。
    """

    tool_call_id: str = ""
    content: str
    is_error: bool = False
    audit: dict[str, object] = Field(default_factory=dict)


class Usage(BaseModel):
    """token 用量(来自 stream_options.include_usage 或非流式响应)。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AgentMessage(BaseModel):
    """ReAct 循环中的统一消息类型(消息 + 工具调用 + 结果)。"""

    role: Role
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # 工具结果是否表示失败(仅 role="tool" 有意义)。Anthropic 的 tool_result block
    # 有原生 is_error 字段;OpenAI 协议没有,靠正文表达。此前该信息在 loop 落地
    # tool 消息时被丢弃,导致转 Anthropic 时无法还原「这是错误结果」。
    is_error: bool = False


def to_openai_messages(messages: list[AgentMessage]) -> list[dict[str, object]]:
    """把 AgentMessage 列表转成 OpenAI Chat Completions 请求的 messages 数组。

    - assistant 带 tool_calls → 透传 tool_calls
    - tool 消息 → role=tool + tool_call_id + content
    - 其余 → role + content
    """
    out: list[dict[str, object]] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        elif m.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content,
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _tool_input(arguments: str) -> dict[str, object]:
    """工具调用的 JSON 字符串参数 → Anthropic 的 `input` 对象。

    非法/空参数在这里**不抛异常** —— 它是本项目的既有场景:参数校验失败时错误
    Observation 会被回灌,而那条 assistant 消息(带坏参数的 tool_use)仍可能在
    后续轮次里被重新序列化发出。此时退化为空对象,让对话能继续,由模型自行修正。
    """
    if not arguments or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def to_anthropic_messages(
    messages: list[AgentMessage],
) -> tuple[str, list[dict[str, object]]]:
    """把 AgentMessage 列表转成 Anthropic Messages API 的 `(system, messages)`。

    与 OpenAI 的三点结构性差异(每一条都对应一个会直接 400 的坑):

    1. **system 是顶层参数**,不是 messages 里的 role。全部 system 消息合并成
       一个字符串返回,并从 messages 中剔除。
    2. **role 必须交替**。本项目对每个工具结果各 append 一条 `role="tool"` 消息
       (`agent/loop.py`),并行工具一轮就会产生**连续多条** —— 朴素转成连续
       `role:"user"` 会触发「roles must alternate」错误。故这里把**相邻的 tool
       结果合并进同一条 user 消息的多个 tool_result block**。
    3. **tool 结果是 content block**,不是顶层字段:`{"type":"tool_result",
       "tool_use_id":..., "content":..., "is_error":...}`。

    assistant 侧同理用 content blocks:`text` + `tool_use`(后者的 `input`
    由 JSON 字符串解码而来)。
    """
    system_parts: list[str] = []
    out: list[dict[str, object]] = []
    # 待合并的相邻 tool 结果(遇到非 tool 消息时 flush 成一条 user 消息)
    pending_tool_results: list[dict[str, object]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            # Anthropic 要求 tool_result 承载在 user 消息里;多个结果共用一条,
            # 而不是各发一条 user(会违反 role 交替约束)。
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        if m.role == "system":
            # 差异 1:抽出为顶层 system,不进 messages
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == "tool":
            # 差异 2/3:攒进待合并列表,不立即成条
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content,
                    # Anthropic 原生支持错误标记;缺了它,模型只能从正文猜
                    "is_error": m.is_error,
                }
            )
            continue
        flush_tool_results()  # 任何非 tool 消息前,先把攒的 tool_result 落成 user 条

        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, object]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": _tool_input(tc.arguments),  # 差异:str → object
                }
                for tc in m.tool_calls
            )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.content})

    flush_tool_results()  # 收尾(最后一条恰好是 tool 结果时)
    return "\n\n".join(system_parts), out
