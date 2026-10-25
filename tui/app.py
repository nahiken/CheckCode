"""tui/app.py —— Textual 应用(组合根)。

持有 `TaskManager` **实例**(不是像 `api/app.py` 那样的模块级全局):测试可以起多个
App 互不污染,而模块级单例在多个 pilot 之间会串状态(Web 侧的测试注释已承认这一点)。

屏幕之外的一切都在 `tui/{gates,session,actions}.py` 里,且那些模块不导入 Textual ——
本模块只负责把它们的输出接进 widget。
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App
from textual.message import Message

from api.tasks import TaskManager
from tui import actions, gates
from tui.screens.approval import ApprovalModal, PlanApprovalModal
from tui.screens.workbench import WorkbenchScreen
from tui.widgets.conversation import ConversationView


class AgentEvent(Message):
    """一条任务事件(由消费协程经 `post_message` 投递到 UI 线程)。

    **为什么用 post_message 而不是 `call_from_thread`**:Agent 协程由 App 自己
    `asyncio.create_task` 出来,与 Textual **同 loop 同线程**;`call_from_thread` 是
    跨**线程**原语,同线程调用会直接报错。post_message 是线程安全的消息入队,
    同线程时就是一次廉价 put,由消息泵在渲染节拍处理。
    """

    def __init__(self, task_id: str, item: dict[str, Any]) -> None:
        super().__init__()
        self.task_id = task_id
        self.item = item


class WorkbenchApp(App[None]):
    """终端工作台。

    注入缝(与 `VerifiedLoop.__init__` 对齐,供离线端到端测试):
    `transport` / `test_command` / `max_fix_rounds` —— Web 侧没有这些缝(它只能
    monkeypatch 整个 VerifiedLoop),TUI 是进程内 UI,留真缝能测到更深的层次。
    """

    CSS = """
    #task-form { height: auto; padding: 1 1 0 1; }
    #task-input { width: 1fr; }
    #form-options { height: auto; }
    #form-options Checkbox { margin-right: 2; }
    #body { height: 1fr; }
    #task-list { width: 45%; }
    #detail { width: 1fr; border-left: solid $accent; }
    #conversation { height: 1fr; }
    .tool-card { border: round $panel; padding: 0 1; margin: 0 0 1 0; }
    .thinking { color: $text-muted; }
    .assistant { margin: 0 0 1 0; }
    .verify { margin: 0 0 1 0; }
    #status-bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    #approval-box { width: 70; height: auto; padding: 1 2; background: $surface;
                    border: thick $accent; }
    #approval-title { text-style: bold; margin-bottom: 1; }
    #approval-buttons { height: auto; margin-top: 1; }
    """

    def __init__(
        self,
        *,
        repo: str | None = None,
        transport: Any = None,
        test_command: str | None = None,
        max_fix_rounds: int | None = None,
        auto_approve: bool = False,
        strict: bool = False,
    ) -> None:
        super().__init__()
        self.manager = TaskManager()
        self.repo = repo
        self._transport = transport
        self._test_command = test_command
        self._max_fix_rounds = max_fix_rounds
        self._default_auto_approve = auto_approve
        self._default_strict = strict
        self._sessions: dict[str, Any] = {}
        # 后台任务强引用:create_task 无引用会被 GC 回收(api/app.py 同款守卫)
        self._jobs: set[asyncio.Task[None]] = set()
        # 已弹出模态框的 approval_id → 避免重复弹(计划门与逐工具门各一次)
        self._modal_shown: set[str] = set()

    def on_mount(self) -> None:
        self.push_screen(WorkbenchScreen(self))

    # ---- 供 screen 调用 ----

    def refresh_task_list(self) -> None:
        screen = self._workbench()
        if screen is not None:
            screen.refresh_list()

    def flash(self, message: str) -> None:
        """弹一条提示(toast)。

        刻意**不叫** `notify`:那是 Textual `App` 的既有方法,覆盖它需要匹配一长串
        关键字参数签名;另起名字更省事也更好读。
        """
        self.notify(message)

    def submit_form(self, *, task: str | None = None, **overrides: Any) -> str | None:
        """从表单(或显式参数)创建任务。返回 task_id;失败返回 None。"""
        screen = self._workbench()
        if screen is None:
            return None
        from textual.widgets import Input as _Input

        if task is None:
            task = screen.query_one("#task-input", _Input).value
        from textual.widgets import Checkbox as _Checkbox

        strict = overrides.pop("strict", None)
        if strict is None:
            strict = screen.query_one("#strict-box", _Checkbox).value
        auto = overrides.pop("auto_approve", None)
        if auto is None:
            auto = screen.query_one("#autoapprove-box", _Checkbox).value
        if not str(task).strip():
            self.notify("任务不能为空")
            return None
        try:
            session = actions.create_task(
                self.manager,
                task=str(task),
                emit=lambda item: self._emit(session.task_id, item),
                repo=self.repo,
                strict=bool(strict),
                auto_approve=bool(auto),
                transport=self._transport,
                test_command=self._test_command,
                max_fix_rounds=self._max_fix_rounds,
                on_request_approval=self._request_approval,
            )
        except ValueError as exc:
            self.notify(str(exc))
            return None
        self._sessions[session.task_id] = session
        screen.query_one("#task-input", _Input).value = ""
        self.refresh_task_list()
        return session.task_id

    def resolve_approval(self, task_id: str, approval_id: str, approved: bool) -> None:
        """**唯一的审批写入点** —— 模态框与列表行按键都调这里。"""
        if not actions.resolve_approval(self.manager, task_id, approval_id, approved):
            self.notify("审批项已失效")
        self._close_modal(approval_id)
        self.refresh_task_list()

    def do_rollback(self, task_id: str) -> None:
        err = actions.rollback_task(self.manager, task_id)
        self.notify(f"回滚失败:{err}" if err else "已回滚")
        self.refresh_task_list()

    def do_resume(self, task_id: str) -> None:
        session, err = actions.resume_task(
            self.manager,
            task_id,
            emit=lambda item: self._emit(task_id, item),
            transport=self._transport,
            test_command=self._test_command,
            max_fix_rounds=self._max_fix_rounds,
            on_request_approval=self._request_approval,
        )
        if err is not None:
            self.notify(err)
            return
        if session is not None:
            self._sessions[task_id] = session
        self.notify("已恢复")
        self.refresh_task_list()

    # ---- 内部接线 ----

    def _workbench(self) -> WorkbenchScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, WorkbenchScreen):
                return screen
        return None

    def _emit(self, task_id: str, item: dict[str, Any]) -> None:
        """消费协程的事件 → UI(经 post_message,不阻塞 Agent)。"""
        self.post_message(AgentEvent(task_id, item))

    def _request_approval(self, approval_id: str, kind: str) -> None:
        """审批桥通知「有新审批待决」→ 弹模态框。

        模态框只是触发点之一:列表行按键(`a`/`d`)走同一个 `resolve_approval`。
        故这里即使不弹(或用户直接关掉),Agent 仍会在列表上等待被 resolve。

        ⚠️ `push_screen` **不在此处直接调用**:本方法由 Agent 协程经审批桥同步调用
        (`tui/gates.py` 的 `_await` → `on_request`),不在 Textual 的消息泵调用栈上。
        屏幕栈操作必须在 app 的调度节拍里做,否则不生效(表现为审批永不弹窗、Agent
        静默挂起)。`call_after_refresh` 把这次 push 排到下一帧。
        """
        if approval_id in self._modal_shown:
            return
        self._modal_shown.add(approval_id)
        self.call_after_refresh(self._push_approval_modal, approval_id, kind)

    def _push_approval_modal(self, approval_id: str, kind: str) -> None:
        task_id = self._last_requesting_task()
        if task_id is None:
            return
        handle = self.manager.get(task_id)
        if handle is None:
            return
        approval = next(
            (p for p in handle.pending_summary if p["approval_id"] == approval_id), None
        )
        if approval is None:
            return
        modal = PlanApprovalModal(approval) if kind == "plan" else ApprovalModal(approval)
        self.push_screen(modal, callback=lambda ok: self._on_modal_result(task_id, approval_id, ok))

    def _last_requesting_task(self) -> str | None:
        """最近一个处于 awaiting 状态的任务(审批回调不带 task_id,由此反查)。"""
        for item in reversed(self.manager.list()):
            if item["status"] in ("awaiting_approval", "awaiting_plan_approval"):
                return str(item["task_id"])
        return None

    def _on_modal_result(self, task_id: str, approval_id: str, approved: bool | None) -> None:
        # 关窗/None 视为拒绝(与 esc 同义:审批默认拒绝)
        self.resolve_approval(task_id, approval_id, bool(approved))

    def _close_modal(self, approval_id: str) -> None:
        """从列表 resolve 时,把该审批对应的模态框关掉(否则它悬在栈上)。"""
        for screen in list(self.screen_stack):
            if isinstance(screen, ApprovalModal | PlanApprovalModal):
                self.pop_screen()

    # ---- 消息处理 ----

    async def on_agent_event(self, message: AgentEvent) -> None:
        """事件进 UI:更新当前选中任务的对话流,并按需刷新列表。

        屏幕未挂载完时安静跳过 —— 事件由消息泵投递,可能早于 `compose` 完成到达
        (启动竞态)。事件本身不会丢:它们已进 `handle.history`,选中该任务时整体重绘。
        """
        screen = self._workbench()
        if screen is None or not screen.is_mounted:
            return
        selected = screen.selected_task_id()
        conv = screen.query("#conversation")
        if message.task_id == selected and conv:
            await conv.first(ConversationView).render_event(message.item)
        # 任务级事件(状态变化)刷新列表;高频 Agent 事件不刷,避免抖动
        if message.item.get("event") in (*gates.TERMINAL, "approval", "plan_approval"):
            self.refresh_task_list()
        if message.item.get("event") == "report" and message.task_id == selected:
            await screen.show_task(message.task_id)
