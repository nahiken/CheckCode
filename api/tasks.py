"""api/tasks.py —— 任务管理 + SSE 事件桥。

职责:
- TaskManager:注册任务,持有每个任务的事件历史 + 订阅者队列(SSE 事件缓冲)。
- 把 VerifiedLoop.run(events=...) 的实时事件转发进历史并广播给所有订阅者。
- 单订阅者队列满则丢最旧事件(前端可能跟不上,保最新进度)。

SSE 事件协议:AgentLoop 事件见 agent/loop.StepKind(单一来源);
任务级事件:approval(等待人工审批,非终止)/ verify(每轮验证)/ resume(恢复标记)/
report(最终 RiskReport)/ error / rollback(回滚完成)。

M9 W1 交互审批:审批 gate 收到 policy decision="ask"(越权或 strict 模式写操作)
时,在 TaskHandle 注册 asyncio.Future,推非终止 approval 事件(SSE 流保持打开),
状态转 awaiting_approval 并 await future;浏览器经 POST /api/tasks/{id}/approvals/{aid}
resolve(approve/deny),loop 按结果放行或拒绝回灌。

订阅语义(单消费者 → 广播):每个 SSE 连接一个独立队列(subscribe),
并发连接各收全量事件;迟到订阅先回放 history(含 report/error 终止事件)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.loop import StepEvent
from policy.models import Plan

# 每个任务 SSE 事件缓冲上限:满则丢弃最旧,保最新进度(前端只关心最近状态)
SSE_QUEUE_MAX = 500

# 审批种类:tool = 逐工具审批(approval 事件);plan = 计划审批(plan_approval 事件)。
ApprovalKind = Literal["tool", "plan"]

# 任务状态(单一来源:set_running/resume 守卫/前端据此渲染行内动作)。
TaskStatus = Literal[
    "created", "running", "awaiting_approval", "awaiting_plan_approval",
    "done", "error", "rolled_back",
]


@dataclass
class _PendingApproval:
    """一个进行中的审批:待决 Future + 现场。

    list() 摘要与审批端点 resolve 都读同一记录(单一来源),不依赖 SSE 到达顺序。
    kind 区分两类审批;plan(tool 审批为 None)是计划审批的被审对象——list() 与前端
    据此渲染同一个 Plan,而不是把它拆成 tool_name/args 两个字段再拼回去。
    """

    approval_id: str
    future: asyncio.Future[bool]
    tool_name: str | None
    reason: str
    kind: ApprovalKind = "tool"
    plan: Plan | None = None


@dataclass
class TaskHandle:
    """一个运行中任务的事件历史与订阅者缓冲。

    - history:任务产生的全部事件(迟到订阅先回放;report/error 恒在其中)。
    - push:广播给当前每个订阅者的独立队列(满则丢最旧,保最新进度)。
    - pending:进行中审批(approval_id → 记录 + Future);gate await Future 即暂停 Agent,
      approve(True)/deny(False) 都由审批端点 resolve。
    - request:创建任务请求快照(strict/auto_approve/repo/task),resume 恢复现场。
    """

    task_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    status: TaskStatus = "created"
    report: dict[str, Any] | None = None
    error: str | None = None
    repo: str | None = None  # 目标仓库路径(回滚/恢复用)
    base_commit: str | None = None  # 基线提交(回滚目标)
    # M12:本次运行的 session JSONL 路径(artifact 端点据此读 Trace 与运行摘要)
    session_file: str | None = None
    # M9 W1:进行中审批与创建请求快照(见模块 docstring)
    pending: dict[str, _PendingApproval] = field(default_factory=dict)
    request: dict[str, Any] | None = None

    def push(self, data: dict[str, Any]) -> None:
        """写入事件历史并广播给所有订阅者;单订阅者队列满则丢最旧(保最新)。"""
        self.history.append(data)
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(data)

    def register_pending(
        self,
        approval_id: str,
        *,
        kind: ApprovalKind = "tool",
        tool_name: str | None = None,
        plan: Plan | None = None,
        reason: str,
    ) -> asyncio.Future[bool]:
        """登记一个新审批(逐工具或计划)并置对应 awaiting 状态。

        审批端点经 resolve_pending 完成 Future;await 它的 gate 随即放行(True)
        或返回拒绝(False)→ Agent 收到 deny 回灌 / 计划门提前返回。
        """
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = _PendingApproval(
            approval_id, future=fut, tool_name=tool_name, reason=reason, kind=kind, plan=plan,
        )
        self.status = (
            "awaiting_plan_approval" if kind == "plan" else "awaiting_approval"
        )
        return fut

    def resolve_pending(self, approval_id: str, approved: bool) -> bool:
        """完成一个审批(approve/deny)。返回是否找到并完成(找不到 → 调用方 404)。"""
        rec = self.pending.pop(approval_id, None)
        if rec is None or rec.future.done():
            return False
        rec.future.set_result(approved)
        if not self.pending:
            self.status = "running"
        return True

    @property
    def pending_summary(self) -> list[dict[str, Any]]:
        """进行中审批摘要(list() 端点用)。

        计划审批用 `plan` 字段承载被审对象;`tool_name` 对计划审批为 None(不拿 "plan"
        这个字面量冒充工具名)。
        """
        return [
            {
                "approval_id": rec.approval_id,
                "kind": rec.kind,
                "tool_name": rec.tool_name,
                "reason": rec.reason,
                "plan": rec.plan.model_dump() if rec.plan is not None else None,
            }
            for rec in self.pending.values()
        ]

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """注册一个新订阅者(每个 SSE 连接一个队列,互不干扰)。"""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SSE_QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(q)


class TaskManager:
    """任务注册表 + 事件转发。所有方法线程安全(单事件循环内)。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskHandle] = {}

    def create(self, task_id: str) -> TaskHandle:
        handle = TaskHandle(task_id=task_id)
        self._tasks[task_id] = handle
        return handle

    def get(self, task_id: str) -> TaskHandle | None:
        return self._tasks.get(task_id)

    def list(self) -> list[dict[str, Any]]:
        """全部任务概要(任务列表端点)。

        含 pending 审批摘要(逐工具/计划)与原始任务描述,前端据此渲染
        awaiting 行的「允许/拒绝」「批准/拒绝」与 done/error 行的「恢复」。
        """
        return [
            {
                "task_id": tid,
                "status": h.status,
                "error": h.error,
                "task": (h.request or {}).get("task", ""),
                "pending_approvals": [
                    p for p in h.pending_summary if p["kind"] == "tool"
                ],
                "pending_plan_approvals": [
                    p for p in h.pending_summary if p["kind"] == "plan"
                ],
            }
            for tid, h in self._tasks.items()
        ]

    def set_running(self, task_id: str) -> None:
        handle = self._tasks.get(task_id)
        # 等待审批(逐工具/计划)时不被 set_running 覆盖:审批 resolve 才转 running。
        if handle is not None and handle.status not in (
            "awaiting_approval",
            "awaiting_plan_approval",
        ):
            handle.status = "running"

    def set_awaiting_approval(
        self, task_id: str, approval_id: str, tool_name: str, args: dict[str, Any], reason: str
    ) -> asyncio.Future[bool]:
        """任务进入等待人工审批:登记审批记录 + 推 approval 事件(非终止,SSE 流保持)。

        返回该审批的 Future(审批 gate 将 await;浏览器 approve/deny 完成它)。
        """
        handle = self._tasks.get(task_id)
        if handle is None:
            raise KeyError(f"task {task_id} 不存在")
        fut = handle.register_pending(
            approval_id, tool_name=tool_name, reason=reason
        )
        handle.push(
            {
                "event": "approval",
                "data": {
                    "approval_id": approval_id,
                    "tool_name": tool_name,
                    "args": dict(args),
                    "reason": reason,
                },
            }
        )
        return fut

    def set_awaiting_plan_approval(
        self, task_id: str, approval_id: str, plan: Plan
    ) -> asyncio.Future[bool]:
        """任务进入等待计划审批(M10):登记 + 推**非终止** `plan_approval` 事件。

        与逐工具审批走同一 pending 机制,但 kind="plan";事件名不同——前端据此渲染
        计划面板(展示 plan.files/commands)而非工具审批面板。这是该事件**唯一**的发出点
        (VerifiedLoop 不发,避免重复事件里第一个缺 approval_id)。
        """
        handle = self._tasks.get(task_id)
        if handle is None:
            raise KeyError(f"task {task_id} 不存在")
        fut = handle.register_pending(approval_id, kind="plan", plan=plan,
                                      reason="严格模式:计划需人工批准后进入写阶段")
        handle.push(
            {
                "event": "plan_approval",
                "data": {
                    "approval_id": approval_id,
                    "files": list(plan.files),
                    "commands": list(plan.commands),
                },
            }
        )
        return fut

    def set_done(self, task_id: str, report: dict[str, Any]) -> None:
        handle = self._tasks.get(task_id)
        if handle is None:
            return
        handle.status = "done"
        handle.report = report
        handle.push({"event": "report", "data": report})

    def set_error(self, task_id: str, message: str) -> None:
        handle = self._tasks.get(task_id)
        if handle is None:
            return
        handle.status = "error"
        handle.error = message
        # 清掉上一轮的 report:resume 失败后若留着旧报告,`/report` `/diff` 会以 200
        # 返回**上一次**的成果,把「本次失败」读成「本次通过」(审计 M10–M15 B5)。
        # 契约:`_report_or_409` 以 `report is None` 判「尚无报告」,故 error 态必须为 None。
        handle.report = None
        handle.push({"event": "error", "data": {"message": message}})

    def set_rollback(self, task_id: str, message: str) -> None:
        """回滚完成:推 rollback 终止事件。"""
        handle = self._tasks.get(task_id)
        if handle is None:
            return
        handle.status = "rolled_back"
        handle.push({"event": "rollback", "data": {"message": message}})


def event_to_dict(ev: StepEvent) -> dict[str, Any]:
    """StepEvent → 可 JSON 序列化 dict(SSE 帧负载)。"""
    return {"event": ev.kind, "data": ev.data}


def sse_frame(payload: dict[str, Any]) -> str:
    """把事件 payload 编码为 SSE 帧:`data: {json}\n\n`。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def parse_sse_frame(frame: str) -> dict[str, Any] | None:
    """解析一条 SSE 帧为 payload(测试辅助)。非 data 行/空 → None。"""
    lines = frame.splitlines()
    data_lines = [line[6:] for line in lines if line.startswith("data:")]
    if not data_lines:
        return None
    parsed = json.loads("".join(data_lines))
    return parsed if isinstance(parsed, dict) else None
