"""tui/screens/workbench.py —— 主屏:任务表单 + 任务列表 + 详情面板。

布局(与 Web 工作台对标):

    Header
    TaskForm(任务输入 + 严格/自动放行 + 运行)
    ┌──────────────┬────────────────────────────────┐
    │ TaskList     │ TabbedContent                  │
    │ (DataTable)  │  对话 / 报告 / Diff / Trace    │
    └──────────────┴────────────────────────────────┘
    StatusBar
    Footer
"""

from __future__ import annotations

import contextlib
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Markdown,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from api.tasks import TaskManager
from tui.widgets.conversation import ConversationView, DiffView

# 任务列表列:动作以文本提示(Textual 没有 per-row 按钮,用光标行 + 键位)
_COLUMNS = ("id", "状态", "任务", "可执行动作")


class WorkbenchScreen(Screen[None]):
    """终端工作台主屏。"""

    BINDINGS = [
        Binding("n", "new_task", "新建", show=False),
        Binding("a", "approve", "允许"),
        Binding("d", "deny", "拒绝"),
        Binding("b", "rollback", "回滚"),
        Binding("r", "resume", "恢复"),
        Binding("q", "quit_app", "退出"),
    ]

    def __init__(self, app_ref: Any) -> None:
        super().__init__()
        self._ref = app_ref
        # 构造时就抓住 manager(而不是每次经 Any 的 app_ref 取):既避免 Any 泄漏
        # 进类型检查,也明确「主屏只依赖任务注册表,不依赖整个 App」。
        self.manager: TaskManager = app_ref.manager

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="task-form"):
            yield Input(
                placeholder="输入任务描述,例如:修复 math_utils.py 的 divide",
                id="task-input",
            )
            with Horizontal(id="form-options"):
                yield Checkbox("严格模式(先出计划并审批)", id="strict-box")
                yield Checkbox("自动放行(跳过人工审批)", id="autoapprove-box")
                yield Button("运行任务", id="run-button", variant="primary")
        with Horizontal(id="body"):
            yield DataTable(id="task-list")
            with TabbedContent(id="detail"):
                with TabPane("对话", id="tab-conv"):
                    yield ConversationView(id="conversation")
                with TabPane("报告", id="tab-report"):
                    yield Markdown("(尚无任务)", id="report")
                with TabPane("Diff", id="tab-diff"):
                    yield DiffView("(无修改)", id="diff")
                with TabPane("Trace", id="tab-trace"):
                    yield RichLog(id="trace", highlight=True, markup=True)
        yield Static("就绪", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#task-list", DataTable)
        table.add_columns(*_COLUMNS)
        table.cursor_type = "row"
        self._ref.refresh_task_list()

    # ---- 任务列表 ----

    def selected_task_id(self) -> str | None:
        """当前光标行的 task_id(用 row key,不是行号 —— 刷新后行号会错位)。

        屏幕尚未挂载完时返回 None:任务事件可能**早于** `compose` 完成到达
        (`on_agent_event` 由消息泵驱动,与挂载时序无关),此时 widget 还不存在。
        这是正常的启动竞态,不是错误 —— 用 `query` 而非 `query_one` 避免抛异常。
        """
        found = self.query("#task-list")
        if not found:
            return None
        table = found.first(DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001 - 空表/无光标
            return None
        return str(key.value) if key.value is not None else None

    def refresh_list(self) -> None:
        """按 TaskManager 的摘要重建任务表(**保留光标所在任务**)。"""
        found = self.query("#task-list")
        if not found:  # 未挂载完(与 selected_task_id 同一条防御)
            return
        table = found.first(DataTable)
        keep = self.selected_task_id()
        table.clear()
        for item in self.manager.list():
            tid = item["task_id"]
            actions = self._actions_hint(item)
            table.add_row(
                tid[:8],
                item["status"],
                str(item.get("task", ""))[:40],
                actions,
                key=tid,
            )
        if keep is not None:
            with contextlib.suppress(Exception):  # 任务已被清掉
                table.move_cursor(row=table.get_row_index(keep))
        self._update_status()

    @staticmethod
    def _actions_hint(item: dict[str, Any]) -> str:
        """该行当前可执行的动作(Textual 无 per-row 按钮,以文本提示 + 全局键位)。"""
        status = item["status"]
        if status == "awaiting_plan_approval":
            return "a 批准 / d 拒绝"
        if status == "awaiting_approval":
            return "a 允许 / d 拒绝"
        if status in ("done", "error", "rolled_back"):
            return "r 恢复" + (" / b 回滚" if status != "created" else "")
        return "-"

    def _update_status(self) -> None:
        pending = sum(
            len(item.get("pending_approvals") or []) + len(item.get("pending_plan_approvals") or [])
            for item in self.manager.list()
        )
        text = f"任务 {len(self.manager.list())} 个"
        if pending:
            text += f" · [yellow]待审批 {pending}[/yellow]"
        self.query_one("#status-bar", Static).update(text)

    # ---- 详情面板 ----

    async def show_task(self, task_id: str) -> None:
        """切换选中任务 → 用 `handle.history` 重绘各面板。"""
        handle = self.manager.get(task_id)
        if handle is None:
            return
        conv = self.query_one("#conversation", ConversationView)
        conv.clear_events()
        for item in handle.history:
            await conv.render_event(item)
        report = handle.report or {}
        markdown = self.query_one("#report", Markdown)
        if report:
            # RiskReport.render() 已产出完整 markdown(web 侧是自己拼 HTML)
            markdown.update(_report_markdown(report))
        else:
            markdown.update("(尚无报告)")
        self.query_one("#diff", DiffView).show_diff(str(report.get("diff", "")))
        trace = self.query_one("#trace", RichLog)
        trace.clear()
        trace.write(str(handle.session_file or "(会话文件尚未生成)"))

    # ---- 动作 ----

    def on_data_table_row_highlighted(self) -> None:
        """光标移动 → 刷新详情面板(单任务视图的切换方式)。"""
        tid = self.selected_task_id()
        if tid is not None:
            self.run_worker(self.show_task(tid), exclusive=True)

    def action_approve(self) -> None:
        self._resolve_selected(True)

    def action_deny(self) -> None:
        self._resolve_selected(False)

    def _resolve_selected(self, approved: bool) -> None:
        """从**任务列表行**resolve 当前审批(与模态框共用同一个写入点)。"""
        tid = self.selected_task_id()
        if tid is None:
            return
        handle = self.manager.get(tid)
        if handle is None or not handle.pending:
            self._ref.notify("该任务没有待审批项")
            return
        approval_id = next(iter(handle.pending))
        self._ref.resolve_approval(tid, approval_id, approved)

    def action_rollback(self) -> None:
        tid = self.selected_task_id()
        if tid is not None:
            self._ref.do_rollback(tid)

    def action_resume(self) -> None:
        tid = self.selected_task_id()
        if tid is not None:
            self._ref.do_resume(tid)

    def action_new_task(self) -> None:
        self.query_one("#task-input", Input).focus()

    def action_quit_app(self) -> None:
        self._ref.exit()

    def on_input_submitted(self) -> None:
        self._ref.submit_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-button":
            self._ref.submit_form()


def _report_markdown(report: dict[str, Any]) -> str:
    """从 report dict 渲染人读摘要(diff 单独在 Diff 页,不在此重复)。"""
    passed = "通过" if report.get("passed") else "未通过"
    files = report.get("changed_files") or []
    lines = [
        f"# 验证结果:{passed}",
        "",
        f"- 分支: `{report.get('branch', '')}`",
        f"- 测试命令: `{report.get('test_command', '')}`",
        f"- 剩余风险: {report.get('remaining_risk') or '(无)'}",
        "",
        "## 修改文件",
        "",
    ]
    lines += [f"- `{f}`" for f in files] or ["(无)"]
    return "\n".join(lines)
