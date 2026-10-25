"""tui/widgets/conversation.py —— 对话流(思考 / 文本 / 工具卡片 / 验证轮次)。

**为什么不是 RichLog**:`RichLog` 是 append-only,无法回填已写入的内容。而
`tool_exec_start` 事件**不带 `tool_call_id`**(只给工具名列表),只有 `tool_result`
带 id —— 并发工具乱序返回时,必须靠 id 回填到对应的卡片上。故这里用
`VerticalScroll` 持有 `dict[tool_call_id, ToolCard]`,与 Web 侧
`ConversationStream.vue` 的派生逻辑同构。
"""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll
from textual.widgets import Static


class ToolCard(Static):
    """一次工具调用的卡片:先显示「调用中」,结果到达后回填。"""

    def __init__(self, tool_name: str, tool_call_id: str) -> None:
        super().__init__(f"[工具] {tool_name} …", classes="tool-card")
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.completed = False

    def fill(self, content: str, *, is_error: bool) -> None:
        """用结果回填卡片(错误用红色标记)。"""
        self.completed = True
        mark = "[red]✗[/red]" if is_error else "[green]✓[/green]"
        body = content.strip().splitlines()
        preview = "\n".join(body[:12])
        if len(body) > 12:
            preview += f"\n…(共 {len(body)} 行)"
        self.update(f"{mark} [工具] {self.tool_name}\n{preview}")


class ConversationView(VerticalScroll):
    """ReAct 步骤流的渲染目标。

    `render_event` 接受与 Web 侧**同一形状**的事件 dict(`{event, data}`),
    故两端消费的是同一份事件契约(见 `api/tasks.py::event_to_dict`)。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pending_tools: list[ToolCard] = []
        self._by_id: dict[str, ToolCard] = {}

    def clear_events(self) -> None:
        """切换选中任务时清空重绘。"""
        self._pending_tools.clear()
        self._by_id.clear()
        self.remove_children()

    async def render_event(self, item: dict[str, Any]) -> None:
        """渲染一条事件。未知/纯数据事件静默忽略(不是错误)。"""
        kind = item.get("event")
        data = item.get("data") or {}
        if kind == "thinking" and data.get("content"):
            await self.mount(Static(f"[dim]💭 {data['content']}[/dim]", classes="thinking"))
        elif kind == "assistant" and data.get("content"):
            await self.mount(Static(data["content"], classes="assistant"))
        elif kind == "tool_exec_start":
            # 此事件**不带 id**,只给工具名 —— 先起卡片占位,等 tool_result 按 id 回填
            for name in data.get("tools") or []:
                card = ToolCard(str(name), "")
                self._pending_tools.append(card)
                await self.mount(card)
        elif kind == "tool_result":
            await self._fill_tool(data)
        elif kind == "verify":
            passed = "通过" if data.get("passed") else "未通过"
            await self.mount(
                Static(f"[bold]第 {data.get('round')} 轮验证:{passed}[/bold]", classes="verify")
            )
        elif kind == "invalid_params":
            await self.mount(
                Static(f"[yellow]参数非法({data.get('tool')}):{data.get('error')}[/yellow]")
            )
        elif kind in ("error",):
            await self.mount(Static(f"[red]{data.get('message') or data}[/red]", classes="error"))
        self.scroll_end(animate=False)

    async def _fill_tool(self, data: dict[str, Any]) -> None:
        """按 tool_call_id 回填卡片;无对应用 id 兜底新建。"""
        call_id = str(data.get("tool_call_id") or "")
        card = self._by_id.get(call_id)
        if card is None:
            # 优先回填最早未完成的占位卡(并发乱序时的常规路径)
            card = next((c for c in self._pending_tools if not c.completed), None)
            if card is None:
                card = ToolCard("工具", call_id)
                await self.mount(card)
            else:
                self._pending_tools.remove(card)
            if call_id:
                card.tool_call_id = call_id
                self._by_id[call_id] = card
        card.fill(str(data.get("content", "")), is_error=bool(data.get("is_error")))


class DiffView(Static):
    """Diff 视图:给 `+` / `-` / `@@` 行着色。

    不用 `Syntax`:diff 不是任何编程语言,语法高亮器没有对应规则。
    """

    def show_diff(self, diff: str) -> None:
        if not diff.strip():
            self.update("(无修改)")
            return
        lines = []
        for line in diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                lines.append(f"[bold]{line}[/bold]")
            elif line.startswith("+"):
                lines.append(f"[green]{line}[/green]")
            elif line.startswith("-"):
                lines.append(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                lines.append(f"[cyan]{line}[/cyan]")
            else:
                lines.append(line)
        self.update("\n".join(lines))
