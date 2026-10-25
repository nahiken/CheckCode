"""TUI 端到端(离线):**真 VerifiedLoop + 真 worktree + 假 LLM**。

这一层用 `httpx.MockTransport` 注入确定的模型输出,其余全是真的 —— 因此它能测到
Web 侧测不到的深度(Web 只能 monkeypatch 整个 VerifiedLoop,因为它的 `_run_task`
没有 transport 缝)。TUI 是进程内 UI,值得留这条真缝。

仍**不启动 Textual**:直接把事件收进列表。UI 层的验证在 test_app.py 里用 pilot 做。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from api.tasks import TaskManager
from tests.agent.helpers import finish, text_delta, tool_delta
from tui import actions


def _plain_transport(body: str) -> httpx.MockTransport:
    """恒定响应:无论模型被调用几次都返回同一段文本(纯收尾场景)。"""
    return httpx.MockTransport(lambda request: httpx.Response(200, text=body))


def _mock_transport(tool_body: str, *, plan_body: str) -> httpx.MockTransport:
    """按**发给模型的 system 提示词**判别当前处于哪个阶段。

    为什么不能用「请求的工具表里有没有 write_file」判别:Planner 与写阶段的工具表
    **都含写工具**(`policy/planner.py` 用全套 tools_spec,只读性靠权限层保证而非
    工具表裁剪,见其模块 docstring)。故工具表区分不开。

    唯一可靠的判别是 system 提示词:Planner 的提示词以「你是 WhyCode 的只读规划器」
    开头(`policy/planner.py::PLANNER_SYSTEM`)。分派规则:

    - 是 Planner → 回带 `[计划]` 块的文本(否则计划为空 → 闭环转只读 pass →
      永远走不到写阶段,表现为「写操作必须命中已审批计划」反复被拒到 max_steps);
    - 不是 Planner(写阶段 / 收尾轮)→ 回工具调用轮。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        system = ""
        for msg in body.get("messages") or []:
            if msg.get("role") == "system":
                system = str(msg.get("content") or "")
                break
        is_planner = "只读规划器" in system
        return httpx.Response(200, text=plan_body if is_planner else tool_body)

    return httpx.MockTransport(handler)


def _config() -> Any:
    from agent.config import LLMConfig

    return LLMConfig(
        base_url="https://test.example", api_key="k", model="m", max_steps=10, provider="openai"
    )


async def test_create_task_runs_to_done_and_emits_events(
    demo_repo: Path, monkeypatch: Any
) -> None:
    """创建 → 跑完 → done;事件流里能看到 assistant 与 report(消费协程工作正常)。"""
    monkeypatch.setattr(actions, "get_llm_config", _config)
    transport = _plain_transport(text_delta("好的") + finish("stop"))

    manager = TaskManager()
    events: list[dict[str, Any]] = []
    session = actions.create_task(
        manager,
        task="说明一下这个仓库",
        emit=events.append,
        repo=str(demo_repo),
        auto_approve=True,
        transport=transport,
        test_command="python -m pytest tests/ -q",
    )
    await session.wait()

    handle = manager.get(session.task_id)
    assert handle is not None
    kinds = [e.get("event") for e in events]
    assert "assistant" in kinds
    # 任务结束的**权威信号**是终止事件之一(消费协程据此停止)
    assert kinds[-1] in ("report", "error", "rollback")
    assert handle.status in ("done", "error")


async def test_unreachable_repo_becomes_task_error_not_process_exit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """坏仓库路径 → 任务转 error,**不**让进程退出(不污染全屏渲染)。

    `prepare_repo` 原本 `print` + `sys.exit(1)`;TUI 的 `resolve_repo` 先自检拦下。
    这条测试锁住「终端工作台不会因为一个打错的路径就死掉」。
    """
    monkeypatch.setattr(actions, "get_llm_config", _config)
    manager = TaskManager()
    session = actions.create_task(
        manager,
        task="修点什么",
        emit=lambda _e: None,
        repo=str(tmp_path / "does-not-exist"),
        auto_approve=True,
    )
    await session.wait()

    handle = manager.get(session.task_id)
    assert handle is not None
    assert handle.status == "error"
    assert handle.error is not None and "不存在" in handle.error


async def test_resume_after_error_reuses_repo(demo_repo: Path, monkeypatch: Any) -> None:
    """error 任务可恢复,且**复用原仓库**(不重新拷贝)。"""
    monkeypatch.setattr(actions, "get_llm_config", _config)
    manager = TaskManager()
    # 第一次:坏路径 → error(留下一个可恢复的任务记录)
    first = actions.create_task(
        manager, task="x", emit=lambda _e: None, repo=str(demo_repo), auto_approve=True,
        transport=_plain_transport(text_delta("nope") + finish("stop")),
        test_command="python -m pytest tests/ -q",
    )
    await first.wait()
    tid = first.task_id
    handle = manager.get(tid)
    assert handle is not None
    # 让它进入可恢复状态(error 或 done 都可恢复;用 set_error 明确构造)
    manager.set_error(tid, "第一次失败")
    repo_before = handle.repo

    session, err = actions.resume_task(
        manager, tid, emit=lambda _e: None,
        transport=_plain_transport(text_delta("再来一次") + finish("stop")),
        test_command="python -m pytest tests/ -q",
    )
    assert err is None and session is not None
    await session.wait()
    assert manager.get(tid).repo == repo_before  # type: ignore[union-attr]


async def test_resume_rejects_running_task(demo_repo: Path, monkeypatch: Any) -> None:
    """运行中的任务不可恢复(它还在跑)。"""
    monkeypatch.setattr(actions, "get_llm_config", _config)
    manager = TaskManager()
    manager.create("t1")
    manager.set_running("t1")
    session, err = actions.resume_task(manager, "t1", emit=lambda _e: None)
    assert session is None
    assert err is not None and "运行中" in err


async def test_approval_flow_end_to_end_with_real_loop(
    demo_repo: Path, monkeypatch: Any
) -> None:
    """⚠️ 承重:真闭环 + 真审批桥 —— **计划审批与逐工具审批各走一次**,然后写到 done。

    这条把「审批桥 + 任务编排 + VerifiedLoop 的两级 gate 注入」串起来验证。
    为什么要 strict:normal 模式下**计划内**的写是 `permit`(不审批)——
    只有 strict 才会对计划内写判 `ask`(见 policy/evaluator.py)。故这里开 strict,
    并依次 resolve 两个门:

    1. **计划门**(粗粒度):Planner 生成的计划在任何 worktree/写入之前提交审批;
    2. **逐工具门**(细粒度):计划批准后,那次 write_file 调用本身请求确认。
    """
    monkeypatch.setattr(actions, "get_llm_config", _config)
    # Planner 必须输出 [计划] 块,否则计划为空 → 闭环转只读 pass → 永远走不到写阶段
    plan_body = text_delta(
        "[计划]\nfile: NOTES.md\n[/计划]\n"
    ) + finish("stop")
    write_args = json.dumps({"path": "NOTES.md", "content": "# hi"})
    transport = _mock_transport(
        tool_delta(0, id_="c1", name="write_file", args=write_args) + finish("tool_calls"),
        plan_body=plan_body,
    )
    manager = TaskManager()
    approvals: list[tuple[str, str]] = []
    session = actions.create_task(
        manager,
        task="创建 NOTES.md",
        emit=lambda _e: None,
        repo=str(demo_repo),
        strict=True,  # 关键:strict 才对计划内写判 ask
        auto_approve=False,  # 关键:走人工审批路径
        transport=transport,
        test_command="python -m pytest tests/ -q",
        on_request_approval=lambda aid, kind: approvals.append((aid, kind)),
    )

    # 依次 resolve 出现的审批门(计划门 → 逐工具门),直到任务结束
    import asyncio

    resolved = 0
    for _ in range(600):
        if session._job is not None and session._job.done():  # noqa: SLF001
            break
        handle = manager.get(session.task_id)
        if handle is not None and handle.pending:
            aid = next(iter(handle.pending))
            assert actions.resolve_approval(manager, session.task_id, aid, True) is True
            resolved += 1
        await asyncio.sleep(0.005)

    await session.wait()
    final = manager.get(session.task_id)
    assert final is not None
    assert final.pending == {}  # 无残留
    assert resolved >= 1, "始终未出现待审批请求"
    # 两类审批都走过(计划门 + 逐工具门)
    assert ("plan" in [k for _aid, k in approvals]) and (
        "tool" in [k for _aid, k in approvals]
    ), approvals
    # 写入发生在 worktree(隔离),主工作树不被触碰
    assert (demo_repo / "NOTES.md").exists() is False
