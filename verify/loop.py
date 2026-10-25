"""VerifiedLoop:修改→测试→修复→出 Diff/风险报告的端到端闭环。

流程(每步都在 worktree 隔离中):
1. 计划:注入 plan 或只读 Planner 生成(build_plan)。
2. 隔离:在 repo 上创建独立 worktree。
3. 修改:Agent 以 worktree 为 workdir 运行(ToolRunner 沙箱锁定到 worktree)。
   mode 决定审批语义(normal:计划内写放行、越权才确认;strict:写须命中计划+审批)。
4. 验证:TestRunner 在 worktree 内确定性跑测试命令。
5. 限轮次修复:失败且未达上限 → 把失败输出回灌给 Agent 再改一轮(独立会话)。
6. 报告:生成 RiskReport(diff / changed_files / rounds / remaining_risk)。
7. 清理:finally 拆除 worktree。

审计落盘在 worktree **之外**(repo/.git/whycode-audits/):worktree 拆除后审计仍保留,
复盘时可回溯「Agent 到底执行了什么」(不随工作区销毁)。

M4 会话持久化(JSONL + 恢复):
- 每个 run 绑定一个 session(branch 名),落盘 repo/.whycode/sessions/<branch>.jsonl:
  header/message/tool_call/phase 全部追加写(崩溃安全,最多丢最后一行)。
- run 正常完成后**保留分支**(worktree 拆除、分支保留):resume 时按 session_id(branch)
  从既有分支重建 worktree —— 已完成副作用(写/命令)在分支上,恢复**不重放**。
- resume 的模型上下文 = build_resume_seed(store 记录):已完成历史原样 +
  overridden 剔除 + pending 只读重放 / 副作用跳过提示。
- 超长工具输出落盘裁剪(max_output_chars)。

每次 loop.run 是独立会话:第 N 轮修复以「验证失败输出」为新任务上下文,
Agent 在**同一 worktree** 继续修改(前一轮改动保留),直到测试通过或到限。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from agent.config import LLMConfig
from agent.loop import StepEvent, build_agent
from agent.types import ToolCall
from hooks.audit import AuditLog
from policy.models import PermissionDecision, Plan, RunMode
from policy.planner import build_plan
from store.models import HeaderRecord, MessageRecord, PhaseRecord, ToolCallRecord
from store.session import SessionStore, build_resume_seed
from tools.base import Tool
from tools.runner import ToolRunner
from verify.plan_gate import PlanGate
from verify.report import RiskReport, derive_remaining_risk
from verify.runner import TestResult, TestRunner
from worktree.manager import Worktree, WorktreeManager

# 默认验证命令(worktree 内的样例仓库测试)
DEFAULT_TEST_COMMAND = "python -m pytest tests/ -q"

# 测试类命令判定:命中这些前缀的命令被视作「验证命令」,否则当普通命令处理
_TEST_PREFIXES = ("python -m pytest", "pytest", "python -m unittest", "nosetests")

# 人工审批网关(与 ToolRunner 同款签名)
ApprovalGate = Callable[[str, dict[str, object], PermissionDecision], Awaitable[bool]]

# 会话记录实时回调(供 CLI/SSE 消费)
RecordEvent = Callable[[StepEvent], Awaitable[None] | None]


class VerifiedLoop:
    """Verified Coding Loop 编排器。plan/transport/gate/test_command 均可注入。

    M4 新增:session_id(恢复锚点)/dispatch(工具注入)/record_event(实时事件)/
    max_output_chars(超长输出落盘裁剪)。resume 语义见模块 docstring。
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        plan: Plan | None = None,
        strict: bool = False,
        approval_gate: ApprovalGate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_fix_rounds: int | None = None,
        test_command: str | None = None,
        session_id: str | None = None,
        dispatch: Callable[[str, dict[str, object]], Awaitable[Any]] | None = None,
        record_event: RecordEvent | None = None,
        max_output_chars: int = 8000,
        # M9 W3:装配层注入(MCP 工具 + 技能提示追加;None = 纯本地)
        extra_tools: dict[str, Tool] | None = None,
        system_prompt_extra: str = "",
        # M10:计划审批门(严格模式下,计划在任何 worktree/写入之前提交人工审批)。
        # require_plan_approval=False(默认)→ 完全无变化(向后兼容既有调用方)。
        plan_approval_gate: PlanGate | None = None,
        require_plan_approval: bool = False,
        # M15:装配层给出的本地工具白名单(None = 未装配技能 → 不限制,默认零变化)
        allowed_local_tools: set[str] | None = None,
    ) -> None:
        self.config = config
        self.plan = plan
        self.mode: RunMode = "strict" if strict else "normal"
        self.approval_gate = approval_gate
        self.transport = transport
        self.max_fix_rounds = max_fix_rounds or _env_fix_rounds()
        self.test_command = test_command
        # M4 持久化挂接
        self.session_id = session_id or f"sess-{uuid.uuid4().hex[:10]}"
        # M12:本次运行标识(每次 run/resume 一个新值;落盘进每条记录的 run_id 字段)
        self.run_id = f"run-{uuid.uuid4().hex[:12]}"
        self.dispatch = dispatch
        self.record_event = record_event
        self.max_output_chars = max_output_chars
        # M9 W3 装配层注入
        self.extra_tools = extra_tools
        self.system_prompt_extra = system_prompt_extra
        # M10 计划审批门
        self.plan_approval_gate = plan_approval_gate
        self.require_plan_approval = require_plan_approval
        # M15 Skill 本地工具白名单(None → 不限制)
        self.allowed_local_tools = allowed_local_tools
        # 恢复状态(供 CLI/调试检查)
        self.store: SessionStore | None = None
        self.last_resumed: bool = False
        self._wt: Worktree | None = None
        # M9 W6:Planner 空输出(无写意图)→ 本次 run 走只读分析 pass
        self._readonly = False

    async def run(
        self,
        repo: Path,
        task: str,
        events: Callable[[StepEvent], Awaitable[None]] | None = None,
    ) -> RiskReport:
        """执行整个闭环,返回 RiskReport。worktree 在 finally 中清理(分支保留)。

        events:可选的实时事件回调(M7 SSE 挂接)。每次 AgentLoop 事件(思考/工具/完成)
        与每轮验证结果都会转发;内部与 record_event(持久化/记账)组合,两者独立。

        M9 W6 只读分流:未注入计划时跑 Planner;若 Planner 空输出(任务无需写文件,
        纯自然语言分析意图)→ 转入只读分析 pass,不建修改分支/不验证/不改任何文件。
        """
        manager = WorktreeManager(repo)
        base = manager.base_commit()

        # 0. 只读读取既有会话记录(恢复判定用)。此处**不落盘** —— 目录/文件的创建
        #    统一推迟到 _open_session(审批通过后);计划被拒绝则完全不产生 session 文件。
        store = SessionStore(
            manager.session_path(self.session_id),
            max_output_chars=self.max_output_chars,
            run_id=self.run_id,
        )
        records = store.records()
        existing_branch = _branch_from_records(records) or self.session_id

        # 1. 计划(未注入则只读 Planner 生成)。
        #    空计划(Planner 未输出 file:/cmd:)= 无写意图 → 只读分析 pass(W6)。
        #    resume 且既有会话已批准过计划 → 直接复用落盘计划,不重跑 Planner(M10)。
        #    本步骤只读:SessionStore 尚未落盘(拒绝路径零 session 文件)。
        plan, plan_approved = await self._resolve_plan(repo, task, records)

        # 空计划 → 只读分析 pass。严格模式下空计划无写意图,故不触发计划门(已知边界,
        # 见 docs/10-m10.md):模型在 readonly runner 中仍受 strict 空计划约束,写一律被拒。
        if plan is None:
            return await self._run_readonly(repo, task, manager, base, events)

        # 2. 计划审批门(M10):严格模式下、**任何 worktree/写入之前**提交人工审批。
        #    拒绝 → 零 worktree、零分支、零 session 文件、零命令(store 全程未落盘)。
        #    恢复时复用既有批准的计划 → 不重复询问、不重新生成。
        if (
            self.require_plan_approval
            and self.plan_approval_gate is not None
            and not plan_approved
        ):
            approved = await self.plan_approval_gate(plan)
            if not approved:
                # 拒绝:store 未落盘 → 不写 plan_rejected phase(写即产生 session 文件)。
                return RiskReport(
                    repo=repo,
                    base_commit=base,
                    branch=self.session_id,
                    changed_files=[],
                    test_command=self.test_command or DEFAULT_TEST_COMMAND,
                    rounds=[],
                    rounds_used=0,
                    remaining_risk="计划被用户拒绝,未做任何修改",
                    diff="",
                    plan_rejected=True,
                    plan=plan,
                )

        # 3. 会话存储落盘(审批之后):批准/未启用计划门才写 header、session 与 plan 记录。
        #    局部变量 store 让 mypy 收窄为已初始化(self.store 声明为可选)。
        store = self._open_session(manager)
        # 恢复判定:既有会话**有内容**(含已批准的恢复)才复用分支,否则建新 worktree。
        # 空目录/空文件不是会话 —— 避免把新建 run 误判成恢复。
        # ⚠️ 只读 pass 的会话**不算可恢复**:它从不建分支,`create_from_branch` 必然
        # 失败(审计 C5,实测 `fatal: invalid reference: sess-X`)。这类会话按
        # 「新会话」处理(建新分支),既不改写也不静默丢结果。
        readonly_session = _is_readonly_session(records)
        resumed = (bool(records) or plan_approved) and not readonly_session

        try:
            if resumed:
                # 恢复既有会话:复用分支(已完成副作用在分支上,不重放)。plan_approved
                # (若有)已在上次运行落盘,不重复写。
                self.last_resumed = True
                self._wt = manager.create_from_branch(existing_branch)
                store.record_phase(
                    PhaseRecord(phase="resume", data={"task": task, "branch": existing_branch})
                )
                await _call_events(
                    events, StepEvent(kind="resume", data={"branch": existing_branch})
                )
            else:
                # 新会话:新建 worktree + 写 header
                self._wt = manager.create(branch=self.session_id)
                self._record_session_start(store, task, repo)
                store.record_phase(PhaseRecord(phase="begin", data={"task": task}))

            # 批准落盘**完整计划**:resume 时据此复用,不重新生成(规格 §2)。
            # ⚠️ 必须在**两个分支之外**写:早先只写在「新会话」分支,于是
            # 门在 resume 路径上被问到时(records 非空但无 plan_approved),
            # 用户的批准**不会落盘** → 每次 resume 都被重问(审计 C3)。
            # `not plan_approved` 保证幂等:已批准过的会话不重复写。
            if (
                self.require_plan_approval
                and self.plan_approval_gate is not None
                and not plan_approved
            ):
                store.record_phase(
                    PhaseRecord(phase="plan_approved", data=_plan_data(plan))
                )

            _record_run_start(store, self.run_id)

            # 4. Agent 修改(worktree 为沙箱边界;审批语义由 self.mode 决定)
            runner = self._build_runner(manager, plan)
            loop = self._build_agent_loop(runner)
            test_cmd = self._resolve_test_command(plan)
            verifier = TestRunner(self._wt.path)

            # resume:复用分支上的既有写操作已提交,先重验证是否已通过。
            # 通过 → 无需模型,直接完成(不重放副作用,模型不被调用);
            # 失败 → 以恢复上下文 seed 预热继续(已完成历史 + 恢复动作,不重复执行)。
            rounds: list[TestResult] = []
            if resumed:
                pre = verifier.run(test_cmd)
                rounds.append(pre)
                store.record_phase(
                    PhaseRecord(
                        phase="resume_verify",
                        data={"passed": pre.passed, "exit_code": pre.exit_code},
                    )
                )
                await _emit_verify(events, 1, pre)
                if not pre.passed:
                    seed = build_resume_seed(records)
                    await loop.resume_with_seed(seed, task, events=events)
            else:
                await loop.run(task, events=events)

            # 4-6. 验证 + 限轮次修复(若 resume 复用分支已通过 → 跳过,不重复验证)
            if not rounds or not rounds[-1].passed:
                for round_no in range(len(rounds) + 1, self.max_fix_rounds + 1):
                    result = verifier.run(test_cmd)
                    rounds.append(result)
                    store.record_phase(
                        PhaseRecord(
                            phase="verify",
                            data={
                                "round": round_no,
                                "passed": result.passed,
                                "exit_code": result.exit_code,
                                "output": result.output[:500],
                            },
                        )
                    )
                    await _emit_verify(events, round_no, result)
                    if result.passed:
                        break
                    if round_no < self.max_fix_rounds:
                        # 失败 → 回灌给 Agent 再修(新会话,保留 worktree 内改动)
                        fix_task = (
                            f"验证失败(第{round_no}轮):\n{result.output}\n"
                            f"请修复问题后重试,确保以下命令通过:\n{test_cmd}"
                        )
                        await loop.run(fix_task, events=events)

            # 落盘改动为分支提交,供 diff/changed_files 取证(Agent 的写工具不 commit)
            manager.commit_all(self._wt, "whycode: task changes")
            changed = manager.changed_files(base, self._wt)
            diff = manager.diff(base, self._wt)
            risk = derive_remaining_risk(
                rounds=rounds,
                changed_files=changed,
                plan=plan,
                max_fix_rounds=self.max_fix_rounds,
            )
            store.record_phase(
                PhaseRecord(phase="done", data={"passed": risk.startswith("低")})
            )
            return RiskReport(
                repo=repo,
                base_commit=base,
                branch=self.session_id,
                changed_files=changed,
                test_command=test_cmd,
                rounds=rounds,
                rounds_used=len(rounds),
                remaining_risk=risk,
                diff=diff,
                plan=plan,
                run_id=self.run_id,
            )
        finally:
            # 拆除 worktree,但**保留分支**(M4 恢复锚点;正常完成或中断都可 resume)
            if self._wt is not None:
                manager.remove(self._wt, keep_branch=True)

    def _open_session(self, manager: WorktreeManager) -> SessionStore:
        """创建并持有本次运行的 SessionStore(首次真正追加时才建目录)。

        只在「确定要写入」的时点调用(run 的审批通过后 / 只读 pass 起跑时),
        以保证计划被拒绝的路径零 session 文件(连目录都不建)。
        """
        self.store = SessionStore(
            manager.session_path(self.session_id),
            max_output_chars=self.max_output_chars,
            run_id=self.run_id,
        )
        return self.store

    async def _resolve_plan(
        self, repo: Path, task: str, records: list[Any]
    ) -> tuple[Plan | None, bool]:
        """确定本次闭环采用的计划 + 该计划是否**已在既有会话中批准过**。

        返回 (plan, plan_approved):
        - 既有会话已有 plan_approved(且落盘了完整计划)→ 复用该计划,**不再询问**。
        - 注入计划 → 直接用;是否还需询问取决于会话里是否已有 plan_approved。
        - 否则只读 Planner 生成;空计划(ValueError)→ (None, False),由调用方转只读 pass。
        """
        approved = _plan_from_records(records)
        if self.plan is not None:
            return self.plan, approved is not None
        if approved is not None:
            return approved, True
        try:
            return await build_plan(self.config, repo, task, transport=self.transport), False
        except ValueError:
            # 空计划是预 LLM 工具链下唯一可用的「无写意图」信号:映射为只读 pass,
            # 而不是把任务打成失败。误判(真写任务被空计划)→ 只读作答、无改动,
            # 报告明确「无文件改动」——安全默认,绝不越权执行。
            return None, False

    async def _run_readonly(
        self,
        repo: Path,
        task: str,
        manager: WorktreeManager,
        base: str,
        events: Callable[[StepEvent], Awaitable[None]] | None,
    ) -> RiskReport:
        """只读分析 pass(M9 W6):Planner 空输出 → 只读作答,不改任何文件。

        - 不创建修改分支/worktree:无写入,无需隔离工作区;只读工具直接作用主仓库。
        - 写工具在 strict + 空计划下**一律 deny**(runner mode 强制 strict + 空 Plan):
          即使模型试图写,也被 deny 回灌,不会越权修改。
        - 不跑验证命令:无改动可验证(空计划 = 无写意图)。rounds=[]。
        - 产出 readonly=True 的 RiskReport:passed 恒真、changed_files=[]、diff 空;
          模型只读分析的最终回答收入 readonly_reply(CLI/前端可展示)。
        """
        self._readonly = True
        # 会话与审计照常落盘(worktree 之外),供复盘/resume 定位
        store = self._open_session(manager)
        # M12:只读 pass 同样记 header(首次)+ run phase —— 否则 Trace 摘要拿不到
        # session_id/run_id,`passed` 也无权威来源(审计 M12-M1)。
        self._record_session_start(store, task, repo)
        _record_run_start(store, self.run_id)
        runner = ToolRunner(
            repo,  # 只读工具不改任何内容,直接作用主仓库;写会被 deny
            mode="strict",  # 强制严格:写必须命中计划 → 空计划下全 deny
            plan=Plan(),
            audit=AuditLog(manager.audit_path(self.session_id)),
            tools=None,
            extra_tools=self.extra_tools,  # MCP 等并入;只读声明者仍放行,其余被 strict 拒
            # M15:技能白名单在只读 pass 同样生效 —— 收窄是任务级属性,不是写阶段专属。
            tools_allow=self.allowed_local_tools,
        )
        loop = self._build_agent_loop(runner)
        messages = await loop.run(task, events=events)
        # 最终回答 = 最后一条 assistant 文本(只读分析的产出;报告带出)
        reply = ""
        for m in reversed(messages):
            if m.role == "assistant" and m.content:
                reply = m.content
                break
        store.record_phase(PhaseRecord(phase="done", data={"passed": True, "readonly": True}))
        return RiskReport(
            repo=repo,
            base_commit=base,
            branch=self.session_id,
            changed_files=[],
            test_command=DEFAULT_TEST_COMMAND,
            rounds=[],
            rounds_used=0,
            remaining_risk="只读分析(无文件改动;Planner 未输出修改计划)",
            diff="",
            readonly=True,
            readonly_reply=reply,
            run_id=self.run_id,
        )

    def _record_session_start(self, store: SessionStore, task: str, repo: Path) -> None:
        """首次运行写 header(会话级上下文);resume 时已有 header → 跳过。

        与主路径同一「header 只在首跑写」语义 —— 补齐只读 pass 与主路径的差异
        (审计 M12-M1:只读会话此前无 header,Trace 摘要 session_id 恒空)。
        """
        if any(isinstance(r, HeaderRecord) for r in store.records()):
            return
        store.record_header(
            HeaderRecord(
                session_id=self.session_id,
                repo=str(repo),
                task=task,
                branch=self.session_id,
                mode=self.mode,
            )
        )

    def _build_runner(self, manager: WorktreeManager, plan: Plan) -> ToolRunner:
        """构建 ToolRunner。注入 audit 到 worktree 之外;dispatch 可被测试覆盖。

        M9 W3:extra_tools(MCP 等)并入工具表 → 走统一安全链;重名覆盖本地工具
        (与 M5 runner 语义一致)。
        """
        audit_path = manager.audit_path(self.session_id)
        if self._wt is None:  # run() 内调用,必已初始化;防御性检查
            raise RuntimeError("VerifiedLoop.run 尚未创建 worktree")
        return ToolRunner(
            self._wt.path,
            mode=self.mode,
            plan=plan,
            approval_gate=self.approval_gate,
            audit=AuditLog(audit_path),
            tools=None,
            extra_tools=self.extra_tools,
            tools_allow=self.allowed_local_tools,  # M15:技能白名单
        )

    def _build_agent_loop(self, runner: ToolRunner) -> Any:
        """构建 AgentLoop。dispatch 注入时(测试)优先;否则用 runner.dispatch。

        record_call:把每次工具调用(名称/参数/审批边界/结果)落盘为 ToolCallRecord,
        并补一条 role=tool 消息(store 据此识别「已完成回灌」,resume 时干净重放)。
        审批边界随 AgentResult.audit 回传(每个工具拿自己的事件,避免与审计重复采集——单一来源)。
        record_event 用于 usage 记账与前端 SSE 消费。
        """
        dispatch = self.dispatch or runner.dispatch
        session_store = self.store

        async def _record_call(tool: ToolCall, result: Any) -> None:
            if session_store is None:
                return
            try:
                from agent.stream_collector import parse_tool_arguments

                args = parse_tool_arguments(tool.arguments)
            except ValueError:
                args = {}
            # 审批边界随 AgentResult.audit 透传(每个工具自己的事件,单一来源);
            # 注入 dispatch(测试)时无 audit → 记 granted=False 且决策=deny,
            # 与「未授权拒绝」语义一致(测试路径,不产生错误授权)。
            a = result.audit or {}
            session_store.record_tool_call(
                ToolCallRecord(
                    tool_call_id=tool.id,
                    tool_name=tool.name,
                    args=args,
                    mode=a.get("mode") or self.mode,
                    decision=a.get("decision") or "deny",
                    layer=a.get("layer") or "unknown",
                    reason=a.get("reason") or "",
                    asked=a.get("asked") or False,
                    approved=a.get("approved") or False,
                    granted=a.get("granted") or False,
                    # 执行结论随审批现场一并落盘(权威判据,见 hooks/base.py)。
                    # 缺省 False:老 run 的 audit 快照无此键 → 退化为「未执行」,
                    # 由 verify/trace.py `_is_executed` 回退旧启发式读取历史。
                    executed=a.get("executed") or False,
                    is_error=result.is_error,
                    result=result.content,
                )
            )
            # P3 修复:补 role=tool 消息(结果已回灌),store 才能识别「已完成」。
            # 超长内容同样裁剪(与 ToolCallRecord 一致),避免 session 文件与恢复上下文膨胀。
            from store.session import truncate_for_store

            truncated_content = truncate_for_store(
                result.content, session_store.max_output_chars
            )
            session_store.record_message(
                MessageRecord(role="tool", content=truncated_content, tool_call_id=tool.id)
            )

        async def _record(ev: StepEvent) -> None:
            if session_store is not None:
                _persist_event(session_store, ev)
            if self.record_event is not None:
                result = self.record_event(ev)
                if result is not None:
                    await result

        return build_agent(
            self.config,
            runner.tools_spec,
            dispatch,
            # M9 W3:装配注入的技能说明追加到系统提示(模型知道可请求注入)
            system_prompt=_WORKTREE_SYSTEM + self.system_prompt_extra,
            transport=self.transport,
            # M13:生产路径启用并发(AGENT_MAX_CONCURRENCY,默认 4)。AgentLoop/build_agent
            # 的**库默认**仍是 1;只有这里(生产装配)从 env 取值。写工具永远是串行屏障
            # (partition_tool_calls 按 Tool.parallel_safe 分组),故并发只作用于只读批。
            max_concurrency=_env_max_concurrency(),
            session_id=self.session_id,
            record_event=_record,
            record_call=_record_call,
            window_size=_env_window_size(),
            token_budget=_env_token_budget(),
        )

    def _resolve_test_command(self, plan: Plan) -> str:
        """确定验证命令:注入 > plan.commands 中的测试类命令 > 默认 pytest。"""
        if self.test_command:
            return self.test_command
        for cmd in plan.commands:
            if cmd.strip().startswith(_TEST_PREFIXES):
                return cmd.strip()
        return DEFAULT_TEST_COMMAND


def _env_fix_rounds() -> int:
    """修复轮次上限,来自 AGENT_FIX_ROUNDS 环境变量(默认 2)。"""
    raw = os.environ.get("AGENT_FIX_ROUNDS")
    if raw is None or raw.strip() == "":
        return 2
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


def _env_max_concurrency() -> int:
    """同批工具并发上限(薄封装 `agent.config.max_concurrency`,保留本模块既有命名)。

    并发策略的**单一来源**在 `agent.config`(中立模块)——Planner(policy)与诊断脚本
    也用它,避免 policy → verify 的循环依赖。
    """
    from agent.config import max_concurrency

    return max_concurrency()


def _env_window_size() -> int:
    """上下文窗口(消息数),来自 AGENT_CONTEXT_WINDOW 环境变量(默认 200)。"""
    raw = os.environ.get("AGENT_CONTEXT_WINDOW")
    if raw is None or raw.strip() == "":
        return 200
    try:
        return max(10, int(raw))
    except ValueError:
        return 200


def _env_token_budget() -> int | None:
    """token 水位预算(M9 W5),来自 AGENT_TOKEN_BUDGET 环境变量(默认 16000)。

    None/0/非数字 → 关闭 token 水位压缩(仅消息数兜底)。默认开启 16000 tokens,
    由 estimate_tokens(当前消息列表启发式估算)触发 compress_to_budget。
    """
    raw = os.environ.get("AGENT_TOKEN_BUDGET")
    if raw is None or raw.strip() == "":
        return 16000
    try:
        val = int(raw)
    except ValueError:
        return 16000
    if val <= 0:
        return None
    return val


def _branch_from_records(records: list[Any]) -> str | None:
    """从既有会话记录推导 branch(header 中存储;无 header 则 None)。"""
    for rec in records:
        if getattr(rec, "type", "") == "header":
            return getattr(rec, "branch", None)
    return None


def _is_readonly_session(records: list[Any]) -> bool:
    """既有会话是否是**只读 pass**(M9 W6)留下的?

    只读 pass 写 header/run/done,但**从不创建分支** —— 若把它当成可恢复会话,
    `create_from_branch` 会去重建一个不存在的分支 → `WorktreeError`(审计 M10–M15 C5,
    实测 `fatal: invalid reference: sess-X`)。会话从此永久不可恢复。

    判据:存在 done phase 且 `data.readonly` 为真。这类会话只能**重新开始**,
    不能 resume;调用方据此改走「新会话」分支(建新分支)。
    """
    for rec in records:
        if (
            getattr(rec, "type", "") == "phase"
            and getattr(rec, "phase", "") == "done"
            and getattr(rec, "data", {}).get("readonly")
        ):
            return True
    return False


def _plan_data(plan: Plan) -> dict[str, object]:
    """Plan → phase.data(落盘用)。只有显式列出的字段,避免把 Plan 内部字段写进会话。"""
    return {"files": list(plan.files), "commands": list(plan.commands)}


def _record_run_start(store: SessionStore, run_id: str) -> None:
    """记录一次运行的标识(M12)。每次 run/resume 一条 —— resume 复用同一 session 文件,
    多条 run 记录让 Trace 能按 run_id 分段(区分「这一次跑」)。主路径与只读 pass 共用。
    """
    store.record_phase(PhaseRecord(phase="run", data={"run_id": run_id}))


def _plan_from_records(records: list[Any]) -> Plan | None:
    """既有会话里**已批准的计划**(M10 计划审批门 resume 语义:复用计划,不重复询问)。

    取最后一条 plan_approved phase(同一会话只应有一条)。旧格式只存 `{files}`、
    没有 commands → 用空 commands 恢复(仍复用文件范围,不重跑 Planner)。
    无 plan_approved → None(调用方回退到 Planner 生成)。
    """
    for rec in reversed(records):
        if getattr(rec, "type", "") != "phase":
            continue
        if getattr(rec, "phase", "") != "plan_approved":
            continue
        data = getattr(rec, "data", {}) or {}
        files = data.get("files") or []
        commands = data.get("commands") or []
        return Plan(
            files=[str(f) for f in files] if isinstance(files, list) else [],
            commands=[str(c) for c in commands] if isinstance(commands, list) else [],
        )
    return None


def _persist_event(store: SessionStore, ev: StepEvent) -> None:
    """把 StepEvent 落盘为 session 记录(assistant 文本 / 轻量 tool 占位)。

    完整的 ToolCallRecord(工具名/参数/结果)由 record_call 落盘(AgentLoop 回调);
    这里只补 message 语义:finish → assistant 文本;tool_result → 无内容占位。
    """
    kind = ev.kind
    data = ev.data
    if kind == "assistant":
        store.record_message(
            MessageRecord(
                role="assistant",
                content=data.get("content", ""),
                reasoning_content=data.get("reasoning_content", ""),
                tool_calls=[ToolCall(**tc) for tc in data.get("tool_calls", [])],
            )
        )
    elif kind == "finish" or kind == "truncated":
        content = data.get("content", "")
        if content:
            store.record_message(MessageRecord(role="assistant", content=content))
    elif kind == "all_invalid":
        content = f"[参数校验失败 {data.get('count', 0)} 个工具调用,已拒绝执行]"
        store.record_message(MessageRecord(role="assistant", content=content))


async def _emit_verify(
    events: Callable[[StepEvent], Awaitable[None]] | None,
    round_no: int,
    result: TestResult,
) -> None:
    """发一轮验证结果事件(resume 预验用第 1 轮,与正常路径轮号一致)。"""
    await _call_events(
        events,
        StepEvent(
            kind="verify",
            data={"round": round_no, "passed": result.passed, "result": result.model_dump()},
        ),
    )


async def _call_events(
    callback: Callable[[StepEvent], Awaitable[None]] | None,
    ev: StepEvent,
) -> None:
    """转发单个事件给调用方回调(可空;SSE 挂接用)。"""
    if callback is not None:
        await callback(ev)


_WORKTREE_SYSTEM = """你是 WhyCode 的安全编码助手,工作在一个隔离的 Git Worktree 中。
- 修改文件前必须先 read_file 查看当前内容。
- 只做被明确要求的修改,不要额外重构。
- 修改后如被要求修复验证失败,请基于失败输出定位并修正。
- 每次修改后给出简要总结。"""
