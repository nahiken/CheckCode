"""ToolRunner:工具调用的统一安全入口。

执行流水线(对一次工具调用):
1. 参数 JSON Schema 校验   —— 缺必填字段 / 类型错误 → 拒绝(不进入权限层)
2. 权限评估 Policy         —— 沙箱 / 黑名单 / **Skill 白名单** / 规则 / 计划 / 运行模式
3. 权限通过或 Ask → 人工审批(ApprovalGate;无 gate → 默认拒绝)
4. 执行(带超时)

`tools_allow`(M15):参数字典由装配层给出 —— None = 未装配技能,不限制工具
(默认路径零变化);set = 只允许其中的本地工具,其余进第 2 步即 deny(layer `skill`)。
这是**工具级白名单**(限工具名),不是 run_command 意义上的 OS 沙箱。

审计(两段式 Hook,经 HookRegistry 分发):**无论放行还是被拒,before 在权限判定后
立刻触发**,记录「即将调用」现场;收尾(执行完成或被拒)后触发 after,落盘最终结论。
两条路径都在 before 之后走到各自的 after,因此每次调用都有两段记录,复盘可凭
`granted`/`executed` 区分「权限放行」「真的执行了」「被拒于执行前」。所有返回都经
`_finish` 保证 after Hook 一定触发。默认注册表只含 AuditLog(行为与 M2 一致);
传 hooks 可叠加自定义 Hook,语义见 hooks/registry.py。

设计要点:
- 与 agent.loop 解耦:签名 (name, args) -> AgentResult,直接可作为 dispatch。
- 工具执行捕获全部异常转 Observation,永不冒泡(安全型 Agent 的纪律)。
- 参数 schema 校验解决 M1 review 遗留:空对象 `{}` 缺必填字段被拒。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from agent.types import AgentResult
from hooks.audit import AuditLog
from hooks.base import ToolAuditEvent
from hooks.registry import HookRegistry, default_registry
from policy.evaluator import PolicyEvaluator
from policy.models import DecisionLayer, PermissionDecision, Plan, Rule, RunMode
from tools.base import Tool
from tools.registry import build_tools, tools_spec_from

# 人工审批网关:decision -> bool(批准执行)。None 表示无人工审批,Ask 默认拒绝。
ApprovalGate = Callable[[str, dict[str, Any], PermissionDecision], Awaitable[bool]]


class ToolRunner:
    """安全工具执行器。tools / evaluator / audit 均可注入(便于测试替换)。"""

    def __init__(
        self,
        workdir: Path,
        *,
        mode: RunMode = "normal",
        plan: Plan | None = None,
        rules: list[Rule] | None = None,
        tool_timeout: float = 120.0,
        tools: dict[str, Tool] | None = None,
        extra_tools: dict[str, Tool] | None = None,
        evaluator: PolicyEvaluator | None = None,
        audit: AuditLog | None = None,
        approval_gate: ApprovalGate | None = None,
        hooks: HookRegistry | None = None,
        tools_allow: set[str] | None = None,
    ) -> None:
        self.workdir = workdir
        self.mode = mode
        self.tool_timeout = tool_timeout
        base_tools = tools or build_tools(workdir, tool_timeout=tool_timeout)
        # M5:MCP 等外部工具合并进统一工具表(单一来源)。重名覆盖本地工具。
        self.tools = {**base_tools, **(extra_tools or {})}
        if evaluator is None:
            evaluator = PolicyEvaluator(
                workdir,
                mode=mode,
                plan=plan,
                rules=rules or [],
                tools_spec=self.tools_spec,
                # M15:Skill 白名单(装配层透传);None = 未装配技能 → 不限制
                tools_allow=tools_allow,
            )
        self.evaluator = evaluator
        self.audit = audit or AuditLog(workdir / ".whycode" / "audit.jsonl")
        # hooks 缺省 → 默认注册表(唯一 Hook = self.audit)。保留 self.audit 具名引用:
        # 测试经 r.audit.records() 读审计,且调用方可继续用它构造 default_registry 再加 Hook。
        self.hooks = hooks or default_registry(self.audit)
        self.approval_gate = approval_gate

    @property
    def tools_spec(self) -> dict[str, dict[str, Any]]:
        return tools_spec_from(self.tools)

    async def dispatch(self, name: str, args: dict[str, Any]) -> AgentResult:
        """对外入口:一次工具调用,返回 Agent 可用的 AgentResult(永不抛异常)。"""
        event = ToolAuditEvent(tool_name=name, args=dict(args), mode=self.mode)
        started = time.monotonic()

        # 1. 参数 schema 校验(M1 遗留:空对象缺必填字段在此拦截)
        tool = self.tools.get(name)
        if tool is None:
            return await self._reject(event, started, f"未知工具: {name}", layer="schema")
        model = tool.args_model
        if model is not None:
            try:
                validated = model.model_validate(args)
            except Exception as exc:
                return await self._reject(
                    event, started, f"参数校验失败,拒绝执行: {exc}", layer="schema"
                )
            # 用校验后的参数(应用 Pydantic 默认值)做评估与执行:
            # 例:list_dir {} 的 path 默认 "."——若用原始 args,沙箱会把 None 误判为越界
            args = validated.model_dump()
            event.args = dict(args)

        # 2. 权限评估
        decision = self.evaluator.evaluate(name, args)
        event.decision = decision.decision
        event.layer = decision.layer
        event.reason = decision.reason
        # granted = **权限**结论(≠ 已执行)。策略直接放行时立刻置位,使 before 现场
        # 就能反映「权限已放行」;Ask 情形留待审批结果决定(下面第 3 步)。
        event.granted = decision.decision == "permit"

        # 2.5 before Hook(权限结论已定,执行/拒绝**尚未发生**)。
        #     位置很关键:放在权限判定之后(现场含 decision/granted),放在**审批与执行
        #     之前**,使放行与被拒**两条路径都触发 before** —— 审计的前置记录不再只
        #     覆盖成功路径。before 抛错 → 视为拒绝(调用方的中断策略不该止步于日志)。
        try:
            await self.hooks.run("before", event)
        except Exception as exc:
            return await self._reject(event, started, f"before Hook 拒绝执行: {exc}", layer="hook")

        # 3. 审批(Ask → 人工;无 gate 默认拒绝)
        if decision.decision == "deny":
            return await self._reject(
                event, started, f"权限拒绝({decision.layer}): {decision.reason}"
            )
        if decision.decision == "ask":
            event.asked = True
            if self.approval_gate is None:
                event.approved = False
                return await self._reject(
                    event, started, f"操作需人工审批(无审批器,默认拒绝): {decision.reason}"
                )
            try:
                event.approved = await self.approval_gate(name, args, decision)
            except Exception as exc:
                return await self._reject(event, started, f"审批器异常,按拒绝处理: {exc}")
            if not event.approved:
                return await self._reject(event, started, f"操作被用户拒绝: {decision.reason}")
            event.granted = True  # 人工放行 = 权限结论成立
            # 审批放行后**补记一次** before 现场:上面那次 before 在审批之前触发,
            # `asked/approved` 都还是 False;审计要能看出「这次调用问过、批过」。
            # 被拒路径(用户拒绝/无 gate)不补记 —— 它们的 asked/approved 已在
            # _reject → _finish 的 after 记录里带上。
            # before Hook 抛错 → 调用不执行(与首次 before 同策略)。
            try:
                await self.hooks.run("before", event)
            except Exception as exc:
                return await self._reject(
                    event, started, f"before Hook 拒绝执行: {exc}", layer="hook"
                )

        # 4. 执行(带超时)。before 现场已在上面触发(granted 反映权限结论)。
        try:
            # M5:MCP 等异步工具在主事件循环直接 await(async_run);本地同步工具
            # 仍经 executor(不阻塞事件循环)。两种路径都用 tool_timeout 统一兜底。
            async_run = getattr(tool, "async_run", None)
            if async_run is not None:
                tool_result = await asyncio.wait_for(
                    async_run(args), timeout=self.tool_timeout
                )
            else:
                loop = asyncio.get_running_loop()
                tool_result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: tool.run(args)),
                    timeout=self.tool_timeout,
                )
        except TimeoutError:
            return await self._reject(
                event, started, f"工具执行超时({self.tool_timeout:.1f}s)", layer="timeout"
            )
        except Exception as exc:
            return await self._reject(event, started, f"工具执行异常: {exc}")

        # 执行**到达**了这个点(异常路径已在上面提前 return)→ 置位权威判据。
        # 放在这里而非更早:超时/执行异常也算「调用了工具」(可能已有副作用),
        # 但那两条走 _reject,由 executed 与 layer 共同区分,不在此处声称成功。
        event.executed = True
        return await self._finish(
            event,
            started,
            AgentResult(content=tool_result.content, is_error=tool_result.is_error),
            tool_result.content,
        )

    async def _reject(
        self, event: ToolAuditEvent, started: float, message: str, *, layer: str = ""
    ) -> AgentResult:
        """被拒路径:记录结论(含 before 现场,若有)+ 返回错误结果。"""
        if layer:
            event.layer = cast(DecisionLayer, layer)
        return await self._finish(
            event,
            started,
            AgentResult(content=message, is_error=True),
            message,
        )

    async def _finish(
        self,
        event: ToolAuditEvent,
        started: float,
        result: AgentResult,
        result_text: str,
    ) -> AgentResult:
        """收尾(after 阶段):填耗时/结果,触发 after Hook,返回结果。

        三条路径都由本方法收尾,靠字段区分(勿假定「after ⇒ 已执行」):
        - **执行成功**:`granted=True` + `executed=True`(执行前已触发 before 现场);
        - **被拒于执行前**(schema/权限/审批/Hook 拒绝):`granted=False`,`executed=False`;
        - **执行了但失败**(超时/异常):`granted=True`,`executed=False` —— 调用到达了
          工具但没正常完成,`is_error=True` 与 layer(timeout/)标记它。这类调用可能
          已产生副作用,故不能与「被拒于执行前」等同视之,区分靠 executed 与 layer 的组合。

        audit 字段随 AgentResult 返回:每个工具调用携带自己的审批现场(含 mode/
        decision/layer/asked/approved/granted/executed),供 M4 会话落盘复用——多工具
        调用时不依赖单槽状态,各拿各的审批边界(单一来源)。
        """
        event.duration_ms = round((time.monotonic() - started) * 1000, 1)
        event.is_error = result.is_error
        event.result = result_text
        # after Hook 的异常不应中断返回:工具已执行,结果必须回灌模型。
        # (默认注册表内是 AuditLog.append,不抛;自定义 Hook 抛由注册表吞掉并记日志。)
        await self.hooks.run("after", event)
        # 随结果返回审批现场(M4 落盘用);result 由调用方(_run_single)再组装,
        # 这里只透传字段,不覆盖 tool_call_id
        return AgentResult(
            tool_call_id=result.tool_call_id,
            content=result.content,
            is_error=result.is_error,
            audit=event.model_dump(exclude={"tool_name", "args", "result"}),
        )
