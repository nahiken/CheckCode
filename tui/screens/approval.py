"""tui/screens/approval.py —— 两类审批模态框(逐工具 / 计划)。

两者都 `dismiss(True/False)`。**注意**:模态框只是 resolve 的**触发点之一** ——
真正的写入点是 `tui.actions.resolve_approval`(任务列表行按键也调它)。
见 `tui/gates.py` 关于「为什么不把 gate 绑到 push_screen_wait」的说明。
"""

from __future__ import annotations

import json
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class _DecisionModal(ModalScreen[bool]):
    """审批模态框基类:`y` 允许 / `n`、`esc` 拒绝。

    **默认拒绝**(esc / 关窗 = 拒):审批是安全边界,不做「默认放行」——
    与 CLI 的 `_confirm` 同一条纪律(`cli/main.py`)。
    """

    BINDINGS = [
        Binding("y", "allow", "允许"),
        Binding("n", "deny", "拒绝"),
        Binding("escape", "deny", "拒绝"),
    ]

    #: 模态框标题(子类覆盖)
    TITLE_TEXT = "审批"

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static(self.TITLE_TEXT, id="approval-title")
            yield from self.body()
            with Horizontal(id="approval-buttons"):
                yield Button("允许 (y)", id="allow", variant="success")
                yield Button("拒绝 (n)", id="deny", variant="error")

    def body(self) -> ComposeResult:  # pragma: no cover - 由子类实现
        """模态框正文(子类覆盖)。"""
        return
        yield  # 让本方法成为生成器(textual 的 compose 约定)

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")


class ApprovalModal(_DecisionModal):
    """逐工具审批:显示工具名、判定理由与参数。

    `approval` 字段来自 `TaskHandle.pending_summary`(与 Web 侧同一份摘要),
    故终端与浏览器渲染的是同一个东西。
    """

    TITLE_TEXT = "工具调用需要确认"

    def __init__(self, approval: dict[str, Any]) -> None:
        super().__init__()
        self._approval = approval

    def body(self) -> ComposeResult:
        yield Static(f"工具: {self._approval.get('tool_name', '?')}")
        yield Static(f"理由: {self._approval.get('reason', '')}")
        args = self._approval.get("args") or {}
        yield Static("参数: " + json.dumps(args, ensure_ascii=False, indent=2))


class PlanApprovalModal(_DecisionModal):
    """计划审批:显示 Planner 生成的计划(文件 + 命令)。

    计划对象原样带出(`_PendingApproval.plan`),而不是把 files/commands 拆成
    字段再拼回去 —— 两侧渲染的是同一个 Plan(见 `api/tasks.py` 的说明)。
    """

    TITLE_TEXT = "计划需要批准(批准后进入写阶段)"

    def __init__(self, approval: dict[str, Any]) -> None:
        super().__init__()
        self._approval = approval

    def body(self) -> ComposeResult:
        plan = self._approval.get("plan") or {}
        files = plan.get("files") or []
        commands = plan.get("commands") or []
        yield Static("文件: " + ("\n  ".join(files) if files else "(无)"))
        yield Static("命令: " + (", ".join(commands) if commands else "(无)"))
