"""verify/plan_gate.py —— 计划审批门(M10)。

严格模式下,只读 Planner 生成的任务专属计划在**任何 worktree/写入之前**提交人工审批:
- 批准 → 闭环继续(计划作为写操作白名单)。
- 拒绝 → 闭环提前返回 `RiskReport(plan_rejected=True)`,零 worktree、零分支、零写入。

与逐工具审批(`ToolRunner` 的 approval_gate)**正交**:即便计划批准,严格模式下每个
命中计划的写操作仍会走 policy `decision="ask"` 的逐工具确认。两级审批语义不同——
计划门是粗粒度「你要动这些文件/跑这些命令吗」,逐工具门是细粒度「这一次调用放行吗」。

门只回答「批不批」这个布尔量;**`plan_approval` SSE 事件由掌握 approval_id 的一侧发出**
(`api/tasks.py:set_awaiting_plan_approval`),VerifiedLoop 不再重复发一个字段不全的事件。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from policy.models import Plan

# 计划审批网关:plan -> 是否批准进入写阶段(True 放行 / False 拒绝)。
PlanGate = Callable[[Plan], Awaitable[bool]]


def auto_plan_gate() -> PlanGate:
    """恒批准的计划门(CLI --auto-approve / Web auto_approve=True / 测试注入)。"""

    async def gate(plan: Plan) -> bool:
        return True

    return gate


def reject_plan_gate() -> PlanGate:
    """恒拒绝的计划门(测试注入)。"""

    async def gate(plan: Plan) -> bool:
        return False

    return gate
