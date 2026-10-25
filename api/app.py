"""api/app.py —— FastAPI 应用:任务创建 + SSE 事件流 + 交互审批 + 回滚/恢复。

端点:
- POST /api/tasks                创建任务 → 后台 asyncio 任务跑 VerifiedLoop
- GET  /api/tasks                任务列表(含 awaiting_approval 与 pending 概要)
- GET  /api/tasks/{id}           任务状态(未找到 → 404)
- GET  /api/tasks/{id}/report    风险报告(未完成 → 409)
- GET  /api/tasks/{id}/diff      Agent 改动 diff + changed_files(未完成 → 409)
- GET  /api/tasks/{id}/trace     会话 Trace(离线读 store;json/jsonl;run_id 过滤)
- GET  /api/tasks/{id}/events    SSE 实时事件流(AgentLoop 事件 + approval/verify/resume)
- POST /api/tasks/{id}/approvals/{aid}  审批:resolve 进行中的浏览器审批(404/409)
- POST /api/tasks/{id}/rollback  回滚任务修改(git 分支回退到基线)
- POST /api/tasks/{id}/resume    恢复会话(复用 handle.repo/session_id,不重放副作用)

设计(要点:后端如何把 CLI 闭环变成 SSE 流 + 浏览器交互审批):
- VerifiedLoop.run(events=...) 实时回调(M4 已预埋 AgentLoop.run(events=...))
  → 转发进 TaskHandle(历史 + 广播到订阅者队列)
- 审批不挂 loop:gate 收到 policy decision="ask" 时在 handle 登记 asyncio.Future、
  推非终止 approval 事件(SSE 流保持打开)、await Future;审批端点完成 Future。
  → approve(True) 放行执行;deny(False) 走 runner 拒绝回灌(Agent 收到反馈)。
- 任务执行与 SSE 消费解耦:事件缓冲,前端订阅不阻塞 Agent 运行。
- report 序列化已抽到 `workbench.report`(pydantic `mode="json"` 处理 Path),
  与终端工作台共用同一口径 —— 本模块不再依赖 fastapi 的 jsonable_encoder。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from agent.config import LLMConfig, get_llm_config
from agent.loop import StepEvent
from api.tasks import TaskHandle, TaskManager, event_to_dict, sse_frame
from assembly.build import build_assembly, default_user_dir
from policy.models import PermissionDecision, Plan
from verify.loop import VerifiedLoop
from verify.prepare import prepare_repo
from workbench.report import report_to_dict
from workbench.rollback import rollback_branch, rollback_guard
from workbench.session_id import new_session_id
from worktree.manager import WorktreeManager

app = FastAPI(title="WhyCode Workbench")
manager = TaskManager()
# 后台任务引用表:防挂起任务被 GC 中断(create_task 无引用会被回收)
_background_tasks: set[asyncio.Task[None]] = set()

# 旧名别名:实现已抽到 workbench(Web 与终端共用),这里保留原名以兼容既有调用方
# (测试直引 `api_app._TaskRequest` / `_report_to_dict` / `_new_session_id`)。
_report_to_dict = report_to_dict
_new_session_id = new_session_id


class _TaskRequest(BaseModel):
    """任务创建请求体(Pydantic 校验:bool 字段对字符串 "false" 也能正确解析)。

    strict:严格模式(计划内写仍需审批);auto_approve:自动放行(**默认 False**)。
    M9 W1:auto_approve=False + strict → policy decision="ask" 时进入浏览器交互审批。

    默认值口径:M9 起 auto_approve 默认 False —— 「审批」是默认行为,自动放行需
    调用方**显式**请求。理由是安全默认(secure by default):AI 改码工作台的缺省
    姿态应当是「人看一眼」,而非「静默放行」。CLI 同款语义(`--auto-approve` 是
    opt-in,见 cli/main.py)。自动化路径(scripts/smoke_real.py、走 API 的集成测试)
    必须显式传 auto_approve=true,否则任务会停在 awaiting_approval 等人工。
    """

    task: str = Field(min_length=1)
    repo: str | None = None
    strict: bool = False
    auto_approve: bool = False
    # M10:严格模式下是否要求计划审批(默认 True)。auto_approve=True 时计划门
    # 自动放行,故两者组合不阻塞自动化路径(tests/smoke_real)。
    require_plan_approval: bool = True
    # M15:只启用这些技能。语义(与 CLI `--skills`、build_assembly 一致):
    # - None / [] → **不筛选**(全部可见技能);这是默认值,故不施加 allowedTools 收窄;
    # - 非空列表 → 只启用列出的技能,**并因此施加**它们的 allowedTools 工具收窄;
    #   列表里有不存在的技能名 → 记入装配告警(经 SSE `assembly_warning` 外显)。
    # 注:API 路径不读 whycode.toml(无 --config 等价入参),故这是本路径唯一的技能入口。
    skills: list[str] | None = None

    @field_validator("task")
    @classmethod
    def _task_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task 不能为空")
        return v


def _make_approval_gate(
    task_id: str, auto_approve: bool
) -> Callable[[str, dict[str, Any], PermissionDecision], Awaitable[bool]]:
    """浏览器审批器:auto_approve=True 全放行;否则交给浏览器交互审批。

    交互路径(gate 捕获 handle):policy decision="ask" → 登记 approval_id + Future
    (approval 事件经 SSE 推给前端)→ await Future。浏览器在审批面板点允许/拒绝
    (POST approvals/{aid}),resolve Future:True 放行执行;False 走 runner 拒绝回灌。
    """

    async def gate(name: str, args: dict[str, Any], decision: PermissionDecision) -> bool:
        return await _await_human_approval(
            task_id,
            auto_approve=auto_approve,
            prefix="ap",
            register=lambda aid: manager.set_awaiting_approval(
                task_id, aid, name, args, decision.reason or decision.decision
            ),
        )

    return gate


def _make_plan_gate(
    task_id: str, auto_approve: bool
) -> Callable[[Plan], Awaitable[bool]]:
    """计划审批器(M10):auto_approve=True 全放行;否则交给浏览器交互审批。

    与 _make_approval_gate 共用 _await_human_approval;只有 id 前缀(pl-)与登记函数
    (plan_approval 事件 + awaiting_plan_approval 状态)不同。拒绝 → VerifiedLoop 在
    任何 worktree/写入之前提前返回(零改动,见 verify/loop.py)。
    """

    async def gate(plan: Plan) -> bool:
        return await _await_human_approval(
            task_id,
            auto_approve=auto_approve,
            prefix="pl",
            register=lambda aid: manager.set_awaiting_plan_approval(task_id, aid, plan),
        )

    return gate


async def _await_human_approval(
    task_id: str,
    *,
    auto_approve: bool,
    prefix: str,
    register: Callable[[str], Awaitable[bool]],
) -> bool:
    """两类浏览器审批的公共骨架:放行短路 → 任务存活检查 → 登记 → await Future。

    任务在等待期间消失(重启/清理)→ 拒绝(不悬挂)。register 里推事件 + 置 awaiting 状态。
    """
    if auto_approve:
        return True
    if manager.get(task_id) is None:
        return False
    return await register(f"{prefix}-{uuid.uuid4().hex[:8]}")


async def _run_task(
    task_id: str, req: _TaskRequest, config: LLMConfig, *, resume: bool = False
) -> None:
    """后台执行 VerifiedLoop,事件转发进 TaskHandle 队列。

    resume=True:复用 handle.repo(不新拷 demo)+ 恢复请求现场,同 session 自动续接
    (SessionStore resume 语义:从既有分支续跑、不重放已完成副作用)。
    """
    handle = manager.get(task_id)
    if handle is None:
        return
    manager.set_running(task_id)
    handle.request = req.model_dump()

    loop = VerifiedLoop(
        config,
        strict=req.strict,
        approval_gate=_make_approval_gate(task_id, req.auto_approve),
        session_id=_new_session_id(task_id),
        # M10:严格模式 → 计划在任何写入前提交浏览器审批(auto_approve 放行)
        plan_approval_gate=_make_plan_gate(task_id, req.auto_approve),
        require_plan_approval=req.strict and req.require_plan_approval,
    )

    try:
        # resume:复用原任务仓库(不重拷 demo,会话续接需同一 repo 的 store 记录);
        # 否则走 prepare_repo(复制 demo 或显式路径)。prepare_repo 模块级导入(见文件头)。
        repo = (
            Path(handle.repo) if resume and handle.repo is not None else prepare_repo(req.repo)
        )
        handle.repo = str(repo)
        handle.base_commit = WorktreeManager(repo).base_commit()
        # M12:会话文件路径(单一来源 = 传给 loop 的同一 session id)。
        # 在 loop.run **之前**赋值:trace/diff 端点在任务运行中即可读(store 是 append 的)。
        handle.session_file = str(
            WorktreeManager(repo).session_path(_new_session_id(task_id))
        )
        # M15:装配技能 → allowedTools 收窄可用本地工具。
        # ⚠️ `build_assembly` 必须**模块级**导入(见文件头 import)——它曾在函数体内
        # 导入,而 MCP 相关传递依赖的首次导入会**同步阻塞事件循环**,把首次工具调用
        # 推迟数秒(M15 审计缺陷 A:测试 `_wait_status(timeout=3.0)` 因此必然超时)。
        # `assembly.build` 内部已把 `assembly.mcp` 延迟到「真配了 MCP」的分支,
        # 故 API 路径不再为 MCP SDK 付启动成本。
        #
        # 注:API 路径不读 whycode.toml(config_path=None,与 CLI 缺 `--config` 时等价),
        # 故此处不会连 MCP —— 只从仓库的 `.whycode/skills/` 与用户级根求白名单。
        # user_dir 显式传用户级根(~/.whycode,WHYCODE_USER_DIR 可覆盖):三级加载的
        # 「用户级」在生产路径真正生效(此前只在测试里覆盖)。
        async with build_assembly(
            repo, None, user_dir=default_user_dir(), allow=req.skills
        ) as asm:
            loop.allowed_local_tools = asm.skills.allowed_local_tools
            # 装配告警必须**外显**:静默吞掉会让「技能名拼错 / 收窄未生效」这类
            # 配置错误无从察觉(审计 M10–M15 B4/B6)。CLI 同款做法见 cli/main.py。
            for warning in asm.errors:
                handle.push({"event": "assembly_warning", "data": {"message": warning}})
            report = await loop.run(
                repo,
                req.task,
                events=lambda ev: _forward_event(handle, ev),
            )
        manager.set_done(task_id, _report_to_dict(report))
    except BaseException as exc:  # noqa: BLE001 - 后台任务任何异常 → error 事件,不悬挂
        # SystemExit(如 prepare_repo 内部 sys.exit)也要捕获,否则任务卡 running
        manager.set_error(task_id, str(exc))


async def _forward_event(handle: Any, ev: StepEvent) -> None:
    """VerifiedLoop 事件 → 任务队列。"""
    handle.push(event_to_dict(ev))


@app.post("/api/tasks", status_code=202)
async def create_task(req: _TaskRequest) -> dict[str, Any]:
    """创建任务:校验 → 注册 → 后台启动 → 返回 task_id + sse_url。"""
    task_id = uuid.uuid4().hex[:12]
    manager.create(task_id)

    config = get_llm_config()
    if not config.api_key:
        manager.set_error(task_id, "未配置 API key(.env 中 DEEPSEEK_API_KEY 或 LLM_API_KEY)")
        return {"task_id": task_id, "sse_url": f"/api/tasks/{task_id}/events", "status": "error"}

    # 后台执行(不阻塞请求返回);保存引用防 GC 中断
    task = asyncio.create_task(_run_task(task_id, req, config))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {
        "task_id": task_id,
        "sse_url": f"/api/tasks/{task_id}/events",
        "status": "created",
    }


@app.get("/api/tasks")
async def task_list() -> list[dict[str, Any]]:
    """任务列表(前端任务列表组件)。"""
    return manager.list()


@app.get("/api/tasks/{task_id}")
async def task_status(task_id: str) -> dict[str, Any]:
    """查询任务状态(结果缓存)。未找到 → 404。"""
    handle = _require_handle(task_id)
    return {
        "task_id": task_id,
        "status": handle.status,
        "report": handle.report,
        "error": handle.error,
    }


def _require_handle(task_id: str) -> TaskHandle:
    """取任务句柄;未找到 → 404(artifact 端点共用)。"""
    from fastapi import HTTPException

    handle = manager.get(task_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    return handle


def _report_or_409(task_id: str) -> TaskHandle:
    """取已完成任务的句柄;任务不存在 → 404,尚无报告 → 409。

    409 的 detail 是**结构化**的:区分「还在跑」(可稍后重试)与「已永久失败」
    (status=error,不会再有报告)——客户端不必解析中文文案。
    """
    from fastapi import HTTPException

    handle = _require_handle(task_id)
    if handle.report is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "任务尚未完成",
                "status": handle.status,
                "error": handle.error,
            },
        )
    return handle


@app.get("/api/tasks/{task_id}/report")
async def task_report(task_id: str) -> Any:
    """风险报告(RiskReport JSON)。未知任务 → 404;尚未完成 → 409。"""
    return _report_or_409(task_id).report


@app.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: str) -> dict[str, Any]:
    """Agent 改动 diff(取自报告;未完成 → 409)。便于 CI/前端直接取补丁。"""
    report = _report_or_409(task_id).report
    assert report is not None  # _report_or_409 已保证
    return {
        "diff": report.get("diff", ""),
        "changed_files": report.get("changed_files", []),
    }


@app.get("/api/tasks/{task_id}/trace")
async def task_trace(
    task_id: str,
    fmt: str = Query("json", alias="format", pattern="^(json|jsonl)$"),
    run_id: str | None = None,
) -> Any:
    """会话 Trace(离线读 store;format=json|jsonl;run_id 过滤某次运行)。

    未完成的**运行中**任务也能读(store 是 append 的,已有部分记录)——故不设 409。
    session 文件不存在/无记录 → 空结果而非 404(任务存在但还没落盘是正常状态)。
    """
    from fastapi.responses import PlainTextResponse

    from store.session import SessionStore
    from verify.trace import build_trace, run_summary, to_jsonl, to_summary_dict

    handle = _require_handle(task_id)
    entries = []
    if handle.session_file is not None and Path(handle.session_file).exists():
        records = SessionStore(Path(handle.session_file)).records()
        entries = build_trace(records, run_id=run_id)
    if fmt == "jsonl":
        return PlainTextResponse(to_jsonl(entries), media_type="application/x-ndjson")
    payload = to_summary_dict(entries)
    payload["summary"] = run_summary(entries)
    return payload


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str) -> StreamingResponse:
    """SSE 事件流:AgentLoop 事件 + verify + report/error/rollback 终止事件。

    订阅语义:
    - 每个连接一个独立队列(subscribe),并发连接各收全量事件。
    - 先回放任务已产生的历史事件,再实时等待(迟到订阅不丢 report/error)。
    - 任务尚未产生任何事件(created)时 → 404 让前端主动刷新。
    """
    handle = manager.get(task_id)
    if handle is None:
        return StreamingResponse(
            iter([sse_frame({"event": "error", "data": {"message": "task not found"}})]),
            media_type="text/event-stream",
            status_code=404,
        )

    # 终止事件:report / error / rollback(其中一个到达即流结束)
    TERMINAL = ("report", "error", "rollback")

    # 历史事件(迟到订阅先回放;含终止事件则回放完即结束)
    history = list(handle.history)

    async def gen() -> AsyncIterator[str]:
        queue = handle.subscribe()
        try:
            for item in history:
                yield sse_frame(item)
                if item.get("event") in TERMINAL:
                    return
            # 实时:从本连接专属队列取;心跳保活;终止事件到达即结束
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse_frame(item)
                if item.get("event") in TERMINAL:
                    return
        finally:
            handle.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/tasks/{task_id}/rollback")
async def rollback_task(task_id: str) -> dict[str, Any]:
    """回滚任务修改:把分支 git reset 到基线提交(丢弃 Agent 全部改动)。

    守卫(repo/基线/状态/零改动三条)在 `workbench.rollback.rollback_guard` ——
    Web 与终端共用同一份,避免安全守卫被抄漏(见该模块 docstring)。
    回滚后推送 rollback 终止事件(前端收到后刷新视图)。
    """
    handle = _require_handle(task_id)
    reason = rollback_guard(handle)
    if reason is not None:
        return {"error": reason}

    try:
        # 前置检查已保证 handle.repo / base_commit 非空(rollback_guard 的第 1 条)。
        assert handle.repo is not None and handle.base_commit is not None
        message = rollback_branch(handle.repo, handle.base_commit, task_id)
        manager.set_rollback(task_id, message)
        return {"status": "rolled_back", "message": message}
    except Exception as exc:  # noqa: BLE001
        manager.set_error(task_id, f"回滚失败: {exc}")
        return {"status": "error", "error": str(exc)}


class _ApprovalBody(BaseModel):
    """审批决策请求体:approve=True 允许执行;False 拒绝(Agent 收到 deny 回灌)。"""

    approve: bool


def _resolve_approval(
    task_id: str, approval_id: str, approved: bool
) -> dict[str, Any]:
    """两类审批端点共用的 resolve 骨架:找任务 → resolve → 恢复 running。

    任务不存在 / 审批 id 不存在(或已完成)→ 404。逐工具与计划审批仅有路由不同。
    """
    from fastapi import HTTPException

    handle = manager.get(task_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="task not found")
    if not handle.resolve_pending(approval_id, approved):
        raise HTTPException(status_code=404, detail="approval not found")
    manager.set_running(task_id)
    return {
        "task_id": task_id,
        "approval_id": approval_id,
        "approved": approved,
        "status": "resolved",
    }


@app.post("/api/tasks/{task_id}/approvals/{approval_id}")
async def resolve_approval(
    task_id: str, approval_id: str, body: _ApprovalBody
) -> dict[str, Any]:
    """浏览器审批决策:resolve 进行中的逐工具审批(approve/deny)。

    审批 gate 在 handle.pending 登记的 Future 由这里完成:approve(True)→ 放行工具执行;
    deny(False)→ ToolRunner 走拒绝路径(Agent 收到「操作被用户拒绝」回灌,继续调整)。
    """
    return _resolve_approval(task_id, approval_id, body.approve)


@app.post("/api/tasks/{task_id}/plan-approvals/{approval_id}")
async def resolve_plan_approval(
    task_id: str, approval_id: str, body: _ApprovalBody
) -> dict[str, Any]:
    """计划审批决策(M10):resolve 进行中的计划审批(approve/deny)。

    与逐工具审批端点同构,但针对 kind="plan" 的 pending 记录(事件为 plan_approval)。
    approve(True)→ 闭环进入写阶段;deny(False)→ 闭环提前返回(零 worktree/零改动)。
    """
    return _resolve_approval(task_id, approval_id, body.approve)


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str) -> dict[str, Any]:
    """恢复任务:复用原任务仓库 + 会话(同 session_id 自动续接),后台重新跑闭环。

    - 仅 error/rolled_back(或中断的 awaiting_approval 已被拒绝)可恢复;running/done 拒绝。
    - resume 语义(SessionStore):从既有分支续跑,不重放已完成副作用操作;
      模型上下文以恢复 seed 预热(已完成历史 + 待续动作)。
    - 前端「恢复」即此端点;完成后照常推 report/error 终止事件。
    """
    handle = _require_handle(task_id)
    if handle.status in ("running", "awaiting_approval", "awaiting_plan_approval"):
        return {"error": f"任务 {handle.status} 运行中,无需恢复"}
    if handle.repo is None:
        return {"error": "任务尚未绑定仓库,无法恢复(请先运行一次)"}
    # 恢复现场 = 原请求快照 + resume 标志(repo 复用,不新拷 demo)
    request = _TaskRequest(**dict(handle.request or {"task": "resume"}))
    config = get_llm_config()
    if not config.api_key:
        return {"error": "未配置 API key(.env 中 DEEPSEEK_API_KEY 或 LLM_API_KEY)"}
    task = asyncio.create_task(_run_task(task_id, request, config, resume=True))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {
        "task_id": task_id,
        "sse_url": f"/api/tasks/{task_id}/events",
        "status": "resumed",
    }
