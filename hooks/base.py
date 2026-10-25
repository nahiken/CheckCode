"""工具审计事件模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from policy.models import DecisionKind, DecisionLayer, RunMode


class ToolAuditEvent(BaseModel):
    """一次工具调用的完整审计记录:参数、权限结论、耗时、结果。

    asked: 本次调用是否进入了人工审批(Ask);approved 仅在 asked=True 时才有意义。
    result: 完整工具结果内容(截断后的);审计落盘而非仅预览,保证可复盘。

    三个布尔字段各管一件事,**不要混用**(审计 M10–M15 C1 的教训):
    - granted  —— **权限**结论:策略层放行了这次调用(含 Ask 批准)。被沙箱/黑名单/
      规则/Hook 拦下时为 False。它**不代表工具真的跑了**:before Hook 拒绝时
      granted 已经是 True,但调用并未执行。
    - approved —— 仅 Ask 场景有意义:人工点了「允许」。
    - executed —— **执行**结论:调用真的到达并执行了工具,是「已执行」的权威判据。
      此前该结论靠 `granted and layer != "hook" and not is_error` 拼凑(见
      verify/trace.py `_is_executed`),是启发式;现在由执行路径直接置位。
    """

    tool_name: str
    args: dict[str, object] = Field(default_factory=dict)
    mode: RunMode = "normal"
    decision: DecisionKind = "deny"
    layer: DecisionLayer = "unknown"
    reason: str = ""
    asked: bool = False  # 是否进入人工审批
    approved: bool = False  # Ask 时人工是否批准(非 Ask 场景恒为 False,以 asked 区分)
    granted: bool = False  # 权限是否放行(≠ 已执行;见 docstring)
    executed: bool = False  # 工具是否真的执行了(与 granted 分开,权威判据)
    duration_ms: float | None = None
    is_error: bool | None = None
    result: str = ""
