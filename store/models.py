"""store/models.py —— 会话持久化的记录模型。

每条记录是一个 Pydantic 模型,`type` 字段区分类型:
- header:会话头(任务/仓库/分支/模式)
- message:ReAct 循环中的一条消息(与 AgentMessage 同构)
- tool_call:一次工具调用的完整现场(参数/权限结论/审批边界/结果)
- phase:验证/修复等运行阶段标记(append-only 顺序)

全部模型带 __test__ = False,防止 pytest 收集为测试类。
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agent.types import ToolCall
from policy.models import DecisionLayer, RunMode


class StoreRecord(BaseModel):
    __test__ = False  # 防止 pytest 收集
    type: str
    # M9 W2:落盘时间戳(Unix 秒)。默认 None:旧文件无 ts 仍可解析,
    # Trace/顺序推导退化为按 append 顺序(缺 ts 的记录 ts 为 None)。
    ts: float | None = None
    # M12:本次运行标识。默认 ""(旧文件无 run_id 仍可解析);由 SessionStore._append
    # 在缺省时统一填充(与 ts 同为单一写入点纪律,避免每个构造点各写一遍)。
    # 只加在**共享基类**——判别式 union 按 type 分发,子类各自添加会破坏解析。
    run_id: str = ""


class HeaderRecord(StoreRecord):
    type: Literal["header"] = "header"
    session_id: str
    repo: str
    task: str
    branch: str
    mode: RunMode = "normal"


class MessageRecord(StoreRecord):
    """一条消息。tool_calls 可选(assistant 带工具调用时填充)。"""

    type: Literal["message"] = "message"
    role: str
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolCallRecord(StoreRecord):
    """一次工具调用的完整现场(M4 恢复语义:副作用判定 + 跳过/覆写标记)。"""

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    args: dict[str, object] = Field(default_factory=dict)
    mode: RunMode = "normal"
    decision: str = "deny"
    layer: DecisionLayer = "unknown"
    reason: str = ""
    asked: bool = False
    approved: bool = False
    granted: bool = False
    # 工具是否真的执行了(≠ granted 的「权限放行」)。默认 False 是对**老会话记录**的
    # 兼容:旧 JSONL 里没有这个字段,读回为 False,由 verify/trace.py `_is_executed`
    # 回退到旧启发式,使历史 Trace 统计口径不变。
    executed: bool = False
    overridden: bool = False  # 恢复时被覆写(跳过重放),原始结果不喂给模型
    duration_ms: float | None = None
    is_error: bool | None = None
    result: str = ""
    truncated: bool = False


class PhaseRecord(StoreRecord):
    """运行阶段标记(phase_begin/phase_end/...)。append-only 顺序事件。"""

    type: Literal["phase"] = "phase"
    phase: str
    data: dict[str, object] = Field(default_factory=dict)


# 反序列化用类型联合(按 type 字段分发)。用 Annotated 避免 pydantic 直接实例化抽象基类。
StoreRecordT = Annotated[
    HeaderRecord | MessageRecord | ToolCallRecord | PhaseRecord,
    Field(discriminator="type"),
]


def parse_record(line: str) -> StoreRecord:
    """把一行 JSON 解析为对应记录类型(损坏/未知类型 → 抛 ValueError,由调用方容忍)。"""
    raw = json.loads(line)
    kind = raw.get("type")
    registry: dict[str, type[StoreRecord]] = {
        "header": HeaderRecord,
        "message": MessageRecord,
        "tool_call": ToolCallRecord,
        "phase": PhaseRecord,
    }
    cls = registry.get(kind)
    if cls is None:
        raise ValueError(f"未知记录类型: {kind!r}")
    return cls.model_validate(raw)
