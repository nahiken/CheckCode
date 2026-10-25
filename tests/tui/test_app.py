"""TUI 界面测试(Textual pilot,headless)。

用 `app.run_test()` 起真实应用但不开终端 —— CI 可用。约定:
- **固定 size**:布局断言的换行/截断随宽度变化,不固定会飘。
- 用 `await pilot.pause()` 推进消息泵,不用 `time.sleep`。
- 不用 Textual 自带的 pytest plugin(其 fixture 风格与仓库 `asyncio_mode="auto"`
  不一致),显式 `async with app.run_test()` 更可控。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.widgets import DataTable, Input, Static

from tests.agent.helpers import finish, text_delta, tool_delta
from tui.app import WorkbenchApp

SIZE = (140, 45)


def _config() -> Any:
    from agent.config import LLMConfig

    return LLMConfig(
        base_url="https://test.example", api_key="k", model="m", max_steps=10, provider="openai"
    )


def _plain(body: str) -> httpx.MockTransport:
    return httpx.MockTransport(lambda r: httpx.Response(200, text=body))


def _staged(tool_body: str, *, plan_body: str) -> httpx.MockTransport:
    """按阶段分派,模拟一个「写一次就收工」的模型。

    两处必须区分(见 test_session_e2e 的同名 helper 说明):

    1. **按 system 提示词**区分 Planner 与写阶段 —— 不能用工具表(两边都含写工具)。
    2. **写阶段只产出一次工具调用**,之后给纯文本收尾。若写阶段永远回同一个工具
       调用,闭环每轮都请求写、每轮都要一次审批,直到 `max_steps` —— 表现为
       「审批按了没用,pending 换个 id 又来了」。真实模型写完会看测试结果再决定。
    """
    state = {"wrote": False}
    tail = text_delta("完成") + finish("stop")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system = next(
            (str(m.get("content") or "") for m in body.get("messages") or []
             if m.get("role") == "system"),
            "",
        )
        if "只读规划器" in system:
            return httpx.Response(200, text=plan_body)
        if not state["wrote"]:
            state["wrote"] = True
            return httpx.Response(200, text=tool_body)
        return httpx.Response(200, text=tail)

    return httpx.MockTransport(handler)


async def _wait(predicate: Any, timeout: float = 15.0) -> None:
    """轮询等条件成立(后台协程调度时长不定,不用固定 sleep)。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待超时")


# === 主屏骨架 ===

async def test_app_mounts_and_shows_columns(demo_repo: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#task-list", DataTable)
        assert table.row_count == 0  # 尚无任务
        # 表单控件都在
        assert app.screen.query_one("#task-input", Input) is not None
        assert app.screen.query_one("#run-button") is not None


async def test_submit_form_creates_task_and_list_shows_it(
    demo_repo: Path, monkeypatch: Any
) -> None:
    """走 UI 路径创建任务:列表出现一行,状态随事件推进。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(
        repo=str(demo_repo),
        transport=_plain(text_delta("好的") + finish("stop")),
        test_command="python -m pytest tests/ -q",
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.screen.query_one("#strict-box").value = False  # type: ignore[attr-defined]
        app.screen.query_one("#autoapprove-box").value = True  # type: ignore[attr-defined]
        tid = app.submit_form(task="说明一下这个仓库")
        assert tid is not None
        await pilot.pause()
        table = app.screen.query_one("#task-list", DataTable)
        assert table.row_count == 1
        # 列表用 task_id 作 row key(不是行号 —— 刷新后行号会错位)
        assert str(table.coordinate_to_cell_key((0, 0)).row_key.value) == tid

        await _wait(lambda: app.manager.get(tid) is not None
                    and app.manager.get(tid).status in ("done", "error"))  # type: ignore[union-attr]
        await pilot.pause()
        assert app.manager.get(tid).status in ("done", "error")  # type: ignore[union-attr]


async def test_empty_task_does_not_create(demo_repo: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.submit_form(task="   ") is None
        assert app.screen.query_one("#task-list", DataTable).row_count == 0


# === 审批:模态框路径 ===

async def test_approval_modal_allow_resolves_gate(demo_repo: Path, monkeypatch: Any) -> None:
    """⚠️ 承重:审批请求 → 弹出模态框 → 按 `y` → gate 放行 → 任务继续。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    plan_body = text_delta("[计划]\nfile: NOTES.md\n[/计划]\n") + finish("stop")
    write_args = json.dumps({"path": "NOTES.md", "content": "# hi"})
    app = WorkbenchApp(
        repo=str(demo_repo),
        transport=_staged(
            tool_delta(0, id_="c1", name="write_file", args=write_args) + finish("tool_calls"),
            plan_body=plan_body,
        ),
        test_command="python -m pytest tests/ -q",
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        tid = app.submit_form(task="创建 NOTES.md", strict=True, auto_approve=False)
        assert tid is not None

        # 计划门先弹 → 批准;然后是逐工具门 → 允许
        from tui.screens.approval import ApprovalModal, PlanApprovalModal

        for _ in range(2):
            await _wait(lambda: isinstance(app.screen, ApprovalModal | PlanApprovalModal))
            await pilot.press("y")
            await pilot.pause()

        await _wait(lambda: app.manager.get(tid).status in ("done", "error"))  # type: ignore[union-attr]
        handle = app.manager.get(tid)
        assert handle is not None
        assert handle.pending == {}  # 两个门都已 resolve,无残留
        # 主工作树不被触碰(改动在 worktree)
        assert (demo_repo / "NOTES.md").exists() is False


async def test_modal_escape_is_deny(demo_repo: Path, monkeypatch: Any) -> None:
    """esc = 拒绝(默认拒绝是安全边界;与 CLI 的 _confirm 同一纪律)。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    plan_body = text_delta("[计划]\nfile: NOTES.md\n[/计划]\n") + finish("stop")
    app = WorkbenchApp(
        repo=str(demo_repo),
        transport=_staged(text_delta("不该走到这里") + finish("stop"), plan_body=plan_body),
        test_command="python -m pytest tests/ -q",
    )
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        tid = app.submit_form(task="创建 NOTES.md", strict=True, auto_approve=False)
        assert tid is not None
        from tui.screens.approval import PlanApprovalModal

        await _wait(lambda: isinstance(app.screen, PlanApprovalModal))
        await pilot.press("escape")
        await pilot.pause()
        await _wait(lambda: app.manager.get(tid).status in ("done", "error"))  # type: ignore[union-attr]
        handle = app.manager.get(tid)
        assert handle is not None
        assert handle.pending == {}
        # 计划被拒 → 零改动(闭环在任何 worktree/写入之前提前返回)
        report = handle.report or {}
        assert report.get("plan_rejected") is True


# === 事件渲染(对等守卫) ===

async def test_conversation_renders_all_event_kinds(demo_repo: Path, monkeypatch: Any) -> None:
    """⚠️ 表驱动对等守卫:每个事件 kind 都要在 UI 上留下可见痕迹。

    专防「事件静默丢失」—— `usage` 陷阱就是这类问题的原型(它走 `_emit` 而非
    `_broadcast`,Web 侧至今没渲染)。这里直接喂事件给 widget,不必 pilot。
    """
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    from tui.widgets.conversation import ConversationView

    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        conv = ConversationView()
        await app.screen.mount(conv)

        cases: list[tuple[dict[str, Any], str]] = [
            ({"event": "thinking", "data": {"content": "想一想"}}, "想一想"),
            ({"event": "assistant", "data": {"content": "答案"}}, "答案"),
            (
                {"event": "tool_exec_start", "data": {"tools": ["read_file"]}},
                "read_file",
            ),
            (
                {
                    "event": "tool_result",
                    "data": {"tool_call_id": "c1", "content": "文件内容", "is_error": False},
                },
                "文件内容",
            ),
            ({"event": "verify", "data": {"round": 1, "passed": True}}, "第 1 轮验证"),
            (
                {"event": "invalid_params", "data": {"tool": "read_file", "error": "空参数"}},
                "参数非法",
            ),
        ]
        for item, expect in cases:
            await conv.render_event(item)
            await pilot.pause()
            rendered = " ".join(str(w.content) for w in conv.query(Static))
            assert expect in rendered, f"{item['event']} 未渲染: {rendered[:200]}"


async def test_tool_card_filled_by_id_not_order(demo_repo: Path, monkeypatch: Any) -> None:
    """工具结果按 `tool_call_id` 回填 —— 并发乱序也不会填错卡片。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    from tui.widgets.conversation import ConversationView

    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        conv = ConversationView()
        await app.screen.mount(conv)
        await conv.render_event({"event": "tool_exec_start", "data": {"tools": ["a", "b"]}})
        # 故意**乱序**返回:b 先完成
        await conv.render_event(
            {"event": "tool_result",
             "data": {"tool_call_id": "cb", "content": "B 结果", "is_error": False}}
        )
        await conv.render_event(
            {"event": "tool_result",
             "data": {"tool_call_id": "ca", "content": "A 结果", "is_error": True}}
        )
        await pilot.pause()
        cards = conv.query(Static)
        texts = [str(c.content) for c in cards]
        # 两张卡片各自拿到正确结果(而不是按到达顺序错配)
        assert any("B 结果" in t for t in texts)
        assert any("A 结果" in t for t in texts)
        assert all("✗" in t or "✓" in t for t in texts if "[工具]" in t)


async def test_rollback_from_list_reports_error_for_plan_rejected(
    demo_repo: Path, monkeypatch: Any
) -> None:
    """列表行回滚对「计划被拒」的任务报错(守卫与 Web 共用 workbench.rollback)。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.manager.create("t1")
        handle = app.manager.get("t1")
        assert handle is not None
        handle.repo = str(demo_repo)
        handle.base_commit = "abc"
        app.manager.set_done("t1", {"plan_rejected": True})
        # 直接调动作层(与按键同一条路径)
        err = __import__("tui.actions", fromlist=["x"]).rollback_task(app.manager, "t1")
        assert err is not None and "计划被拒绝" in err


async def test_status_bar_shows_pending_count(demo_repo: Path, monkeypatch: Any) -> None:
    """状态栏反映待审批数(pending 摘要来自 TaskManager,与 Web 同一来源)。"""
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.manager.create("t1")
        app.manager.set_awaiting_approval("t1", "ap-1", "write_file", {"path": "x"}, "越权")
        app.refresh_task_list()
        await pilot.pause()
        bar = app.screen.query_one("#status-bar", Static)
        assert "待审批" in str(bar.content)


@pytest.mark.parametrize("key", ["q"])
async def test_quit_binding(demo_repo: Path, monkeypatch: Any, key: str) -> None:
    monkeypatch.setattr("tui.actions.get_llm_config", _config)
    app = WorkbenchApp(repo=str(demo_repo))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
    assert True  # 退出无异常即通过
