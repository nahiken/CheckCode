"""tui/session.py —— 单任务的编排与事件消费(**不导入 Textual**)。

对应 `api/app.py` 的 `_run_task` + SSE 消费,但去掉 HTTP/SSE 传输层:

- `TaskSession.start()` 起一个 asyncio 任务跑 VerifiedLoop,把事件推进 TaskHandle
  (与 Web 侧 `_forward_event` 同款:所有事件经 `handle.push` → 单一日志/广播入口)。
- `TaskSession.consume(emit)` 把 `handle` 的事件流推给 UI,遇终止事件停止。

**为什么用 `handle.subscribe()` 而不是只用 `loop.run(events=...)`**:
后者只覆盖 Agent 事件;而 `approval` / `plan_approval` / `report` / `error` / `rollback`
是 TaskManager 自己 `push` 的,**不经过 `events=`**。订阅队列是唯一的全量视图。

**为什么在任务创建时就订阅**:Web 侧先取 history 再 subscribe,中间有一个丢事件的
窗口(`api/app.py` 的 `gen()`)。TUI 是进程内 UI、任务由自己创建,创建即订阅就完全
没有这个竞态 —— 历史只用于「切换选中行时重渲染」。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.config import LLMConfig
from agent.loop import StepEvent
from api.tasks import TaskHandle, TaskManager, event_to_dict
from assembly.build import build_assembly, default_user_dir
from tui.gates import TERMINAL, ApprovalBridge
from verify.loop import VerifiedLoop
from verify.prepare import prepare_repo
from workbench.report import report_to_dict
from workbench.session_id import new_session_id
from worktree.manager import WorktreeManager

# 事件回调:把一条事件交给 UI(Textual 侧是 post_message;测试是无操作)
EmitFn = Callable[[dict[str, Any]], None]


class TaskSession:
    """一个任务的完整生命周期:装配 → 跑闭环 → 推进事件 → 收尾。

    `transport` / `test_command` / `max_fix_rounds` 是给测试留的缝(与
    `VerifiedLoop.__init__` 对齐)。Web 侧没有这条缝 —— 它只能 monkeypatch 整个
    VerifiedLoop;TUI 是进程内 UI,值得留真缝,能测到 Web 测不到的深度(真 loop +
    真 worktree + 假 LLM)。
    """

    def __init__(
        self,
        manager: TaskManager,
        task_id: str,
        handle: TaskHandle,
        request: dict[str, Any],
        *,
        emit: EmitFn,
        transport: Any = None,
        test_command: str | None = None,
        max_fix_rounds: int | None = None,
        on_request_approval: Callable[[str, str], None] | None = None,
    ) -> None:
        self._manager = manager
        self.task_id = task_id
        self.handle = handle
        self.request = request
        self._emit = emit
        self._transport = transport
        self._test_command = test_command
        self._max_fix_rounds = max_fix_rounds
        self._on_request_approval = on_request_approval
        self._job: asyncio.Task[None] | None = None
        self._consumer: asyncio.Task[None] | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        """起两个协程:执行(自带事件入队)+ 消费(出队交给 UI)。

        消费协程**先起**:它一创建就 subscribe,避免「执行已 push、订阅还没建」的窗口。
        """
        self._consumer = asyncio.create_task(self._run_consumer())
        self._job = asyncio.create_task(self._run())

    async def wait(self) -> None:
        """等执行协程结束(测试与「跑完再看结果」用)。"""
        if self._job is not None:
            await self._job

    def cancel(self) -> None:
        for task in (self._job, self._consumer):
            if task is not None and not task.done():
                task.cancel()

    # ---- 执行 ----

    async def _run(self) -> None:
        self._manager.set_running(self.task_id)
        self.handle.request = dict(self.request)
        config = self.request.get("_config")
        if not isinstance(config, LLMConfig):
            self._manager.set_error(self.task_id, "内部错误:缺少 LLM 配置")
            return

        bridge = ApprovalBridge(
            self._manager,
            self.task_id,
            auto_approve=bool(self.request.get("auto_approve", False)),
            on_request=self._on_request_approval,
        )
        loop = VerifiedLoop(
            config,
            strict=bool(self.request.get("strict", False)),
            approval_gate=bridge.tool_gate,
            plan_approval_gate=bridge.plan_gate,
            require_plan_approval=bool(self.request.get("strict", False))
            and bool(self.request.get("require_plan_approval", True)),
            session_id=new_session_id(self.task_id),
            max_fix_rounds=self._max_fix_rounds,
            test_command=self._test_command,
            transport=self._transport,
            record_event=self._forward_usage,
        )
        # resume:复用原任务仓库(不重拷),否则按请求准备
        resume = bool(self.request.get("_resume", False)) and self.handle.repo is not None
        try:
            # prepare_repo 含 copytree + git init(同步、可达数百 ms)→ 挪到线程,
            # 否则 Textual 的 UI 会在这段冻住。
            repo = (
                Path(self.handle.repo)
                if resume and self.handle.repo is not None
                else await asyncio.to_thread(resolve_repo, self.request.get("repo"))
            )
            self.handle.repo = str(repo)
            self.handle.base_commit = await asyncio.to_thread(
                WorktreeManager(repo).base_commit
            )
            # 在 loop.run **之前**赋值:trace/报告面板在运行中即可读(store 是 append 的)
            self.handle.session_file = str(
                WorktreeManager(repo).session_path(new_session_id(self.task_id))
            )
            async with build_assembly(
                repo,
                Path(self.request["config"]) if self.request.get("config") else None,
                user_dir=default_user_dir(),
                allow=self.request.get("skills"),
            ) as asm:
                loop.allowed_local_tools = asm.skills.allowed_local_tools
                # 装配告警必须外显(静默吞掉会让「技能名写错」这类配置错误无从察觉)
                for warning in asm.errors:
                    self.handle.push({"event": "assembly_warning", "data": {"message": warning}})
                report = await loop.run(
                    repo, str(self.request.get("task", "")), events=self._forward_event
                )
            self._manager.set_done(self.task_id, report_to_dict(report))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 任何异常 → error 事件,不悬挂
            self._manager.set_error(self.task_id, str(exc))

    async def _forward_event(self, ev: StepEvent) -> None:
        """Agent 实时事件 → TaskHandle(与 Web 侧 `_forward_event` 同款)。"""
        self.handle.push(event_to_dict(ev))

    def _forward_usage(self, ev: StepEvent) -> None:
        """`record_event` 钩子:补收实时流**看不到**的 `usage` 事件。

        `usage` 在 `agent/loop.py` 走 `_emit`(record_event)而非 `_broadcast`(events=),
        故订阅队列永远收不到它。`VerifiedLoop` 保证先落盘再转发给本钩子
        (`verify/loop.py`),故这里补一份进队列对会话持久化零影响。
        """
        if ev.kind == "usage":
            self.handle.push({"event": "usage", "data": dict(ev.data)})

    # ---- 消费 ----

    async def _run_consumer(self) -> None:
        queue = self.handle.subscribe()
        try:
            while True:
                item = await queue.get()
                self._emit(item)
                if item.get("event") in TERMINAL:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self.handle.unsubscribe(queue)


def resolve_repo(repo: str | None) -> Path:
    """把请求里的 repo 解析成可用仓库路径(**失败抛 ValueError,不退出进程**)。

    `verify.prepare.prepare_repo` 在坏路径上是 `print` + `sys.exit(1)`:
    - 那个 `print` 会**污染 Textual 的全屏渲染**(全屏应用被写一行裸文本);
    - `SystemExit` 会被 `_run` 的 `except BaseException` 吞成一条 error 事件。

    故这里**先自检**再委托:坏路径在进入 prepare_repo 之前就被拦下,连 print 都不会发生。
    自检覆盖 prepare_repo 的全部两条错误分支(路径不存在 / 不是 git 仓库)。
    """
    if repo is not None:
        path = Path(repo).resolve()
        if not path.exists():
            raise ValueError(f"仓库路径不存在: {path}")
        if not (path / ".git").exists():
            raise ValueError(f"不是 git 仓库: {path}")
    # 走到这里 prepare_repo 不会再触发 sys.exit(显式路径已校验;None 走 demo 复制)
    return prepare_repo(repo)
