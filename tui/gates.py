"""tui/gates.py —— 审批桥:把 TaskManager 的 pending 记录接成 VerifiedLoop 的 async gate。

**本模块不导入 Textual**(见 `tui/__init__.py` 的分层约定),因此可以用纯 asyncio 测试。

机制与 `api/app.py` 的 `_await_human_approval` **同构**,不是另起一套:

    gate 在 Agent 协程里 await 一个 Future
      → UI 侧任意触发点调 handle.resolve_pending(aid, ok) 完成它

Web 的触发点是 HTTP 端点,TUI 的是模态框或列表行按键 —— 但两条路径汇入**同一个**
`resolve_pending`,因此「pending 记录 → 任务状态 → approval 事件」的一致性天然共享。

**关键决策:gate `await fut`,而不是 `await push_screen_wait(...)`。**
Web 的 `TaskList.vue` 行内也有允许/拒绝按钮,所以 resolve 必须能来自多个触发点。
若把「能否放行」与「某个具体 screen 是否被 dismiss」耦合,用户在模态框开着时从
列表 resolve,那个 `await` 就会**永远挂着**(Agent 卡死,且看不出原因)。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from api.tasks import TaskManager
from policy.models import PermissionDecision, Plan

# 终止事件:到达即说明任务结束(TUI 的消费协程据此停止等待)
TERMINAL = ("report", "error", "rollback")


class ApprovalBridge:
    """把 TaskManager 的 pending 记录桥成 VerifiedLoop 需要的两个 gate。

    `on_request(approval_id, kind)` 是「有新的审批待决」的通知回调 —— Textual 侧传
    一个 post_message 的封装,TUI 据此弹出模态框;测试传 None(不需要 UI)。
    """

    def __init__(
        self,
        manager: TaskManager,
        task_id: str,
        *,
        auto_approve: bool = False,
        on_request: Callable[[str, str], None] | None = None,
    ) -> None:
        self._manager = manager
        self._task_id = task_id
        self._auto_approve = auto_approve
        self._on_request = on_request or (lambda _aid, _kind: None)

    async def _await(
        self, approval_id: str, kind: str, register: Callable[[str], asyncio.Future[bool]]
    ) -> bool:
        # 自动放行短路:不登记 pending、不推事件(与 api/app.py 同款语义)
        if self._auto_approve:
            return True
        # 任务已消失(被清理/重启)→ 拒绝,不悬挂。审批是安全边界,不默认放行。
        if self._manager.get(self._task_id) is None:
            return False
        future = register(approval_id)  # 登记 pending + 置 awaiting 状态 + 推 approval 事件
        self._on_request(approval_id, kind)
        return await future  # ← 事件循环在此让出:Textual 的 UI 照常响应

    async def tool_gate(
        self, name: str, args: dict[str, object], decision: PermissionDecision
    ) -> bool:
        """逐工具审批 gate(签名与 `verify.loop.ApprovalGate` 一致)。"""
        approval_id = f"ap-{uuid.uuid4().hex[:8]}"
        return await self._await(
            approval_id,
            "tool",
            lambda aid: self._manager.set_awaiting_approval(
                self._task_id, aid, name, args, decision.reason or decision.decision
            ),
        )

    async def plan_gate(self, plan: Plan) -> bool:
        """计划审批 gate(签名与 `verify.plan_gate.PlanGate` 一致)。"""
        approval_id = f"pl-{uuid.uuid4().hex[:8]}"
        return await self._await(
            approval_id,
            "plan",
            lambda aid: self._manager.set_awaiting_plan_approval(self._task_id, aid, plan),
        )
