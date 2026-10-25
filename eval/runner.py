"""eval/runner.py —— 单任务评测执行。

对每个 EvalTask:
1. prepare_repo(模板副本,隔离,不污染 demo_repo)。
2. 注入 Plan(files=plan_files,commands=[verify])—— 越权判定依据。
3. VerifiedLoop.run:注入 record_event 采集 token(usage 事件),测墙钟耗时。
4. 产出 EvalResult:验收通过?/验证通过?/越权修改?/token/耗时/工具成败。
5. 恢复类任务(recovery):先跑一次(用 mock 正常路径),再同 session resume。

口径:任务成功率 = 验收命令通过;越权 = changed_files 超出 plan.files;
恢复率 = resume 后验证通过 / 恢复类任务。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent.config import LLMConfig
from agent.loop import StepEvent
from eval.metrics import EvalResult
from eval.tasks import VERIFY_CMD, EvalTask
from policy.models import PermissionDecision, Plan
from verify.loop import VerifiedLoop
from verify.prepare import prepare_repo
from verify.runner import TestRunner
from worktree.manager import WorktreeError, WorktreeManager


def _last_round_passed(report: Any) -> bool | None:
    """闭环落盘的**最后一轮**验证是否通过。无轮次(只读/计划被拒)→ None。"""
    rounds = getattr(report, "rounds", None) or []
    if not rounds:
        return None
    return bool(rounds[-1].passed)


def _acceptance_ok(task: EvalTask, report: Any, repo: Path) -> bool:
    """任务验收:在**任务分支的最终态**上独立执行验收命令,不引用闭环的自我结论。

    为什么必须真跑(而不是复用 `report.passed`):
    `report.passed` 是闭环在 worktree 里执行的验证结果,拿它当「任务成功」会让
    成功率与验证通过率**恒等**(同一个布尔、同一个分母)—— 两个指标名讲的是同一件事,
    指标看着有、实际不可信(历史设计记录中的坑)。独立执行验收命令后,两者
    才会出现有意义的差异(闭环说通过、但最终态跑不过验收)。

    为什么要在**分支**上跑(而不是主工作区):闭环把改动 commit 到任务分支后拆掉
    worktree,**主工作树停在基线**(见 verify/loop.py 的 `keep_branch=True` 与
    worktree/manager.py 的 force_branch 说明)。直接在 `repo` 里跑验收只会在
    **未修改的基线**上跑,必然失败 —— 那不是「交给用户的最终态」。
    故这里 attach 任务分支,在改造后的树上跑验收,跑完拆掉(保留分支,不影响恢复)。

    验收命令取自 `task.verify`(每条任务自带),与闭环内的测试命令同源。
    """
    branch = getattr(report, "branch", None)
    if not branch:
        # 无分支(只读分流/计划被拒:无改动)→ 无验收面,视作通过
        return True
    manager = WorktreeManager(repo)
    wt: Any = None
    try:
        wt = manager.attach(branch)
    except WorktreeError:
        # 分支已被清理(如回滚后 force_branch 到基线仍存在;真删则是外部干预)
        # → 退化为「在仓库现状上跑」,不因取证基础设施失败而判任务失败。
        return TestRunner(repo, timeout=300.0).run(task.verify).passed
    try:
        return TestRunner(wt.path, timeout=300.0).run(task.verify).passed
    finally:
        manager.remove(wt, keep_branch=True)  # 只拆 worktree,保留分支(恢复锚点)


# 评测用审批网关:全放行(评测只衡量能力,不引入人工审批)
async def _auto_gate(name: str, args: dict[str, Any], decision: PermissionDecision) -> bool:
    return True


class _TokenCollector:
    """从 usage 事件采集 token(每任务一个实例)。

    usage 事件携带**累计值**(ContextWindow 累计,`loop._observe` 每次发当前总量),
    因此取**末值**即为任务总 token;若求和会按步数高估 ~N×(M8 review S1)。
    """

    def __init__(self) -> None:
        self.prompt = 0
        self.completion = 0

    async def __call__(self, ev: StepEvent) -> None:
        if ev.kind == "usage":
            # 累计值 → 覆盖式记录(最后一个 usage 事件 = 任务总 token)
            self.prompt = int(ev.data.get("prompt_tokens", 0))
            self.completion = int(ev.data.get("completion_tokens", 0))

    @property
    def total(self) -> int:
        return self.prompt + self.completion


class _ToolCounter:
    """从事件统计工具成败(工具成功率口径)。"""

    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0

    async def __call__(self, ev: StepEvent) -> None:
        if ev.kind == "tool_result":
            self.calls += 1
            if ev.data.get("is_error"):
                self.errors += 1


async def run_eval_task(
    task: EvalTask,
    *,
    config: LLMConfig,
    auto_approve: bool = True,
    repo: Path | None = None,
    session_id: str | None = None,
    transport: Any = None,
    max_fix_rounds: int | None = None,
) -> EvalResult:
    """运行单个评测任务,返回 EvalResult。不抛异常(失败 → EvalResult.ok=False)。

    repo:显式传入目标仓库(恢复类任务复用同一 repo 跑两阶段);默认复制 demo_repo 模板。
    session_id:恢复类任务的会话锚点(两阶段共用)——同 repo + 同 session 的第二次 run
    → VerifiedLoop 自动走 resume(records 非空,复用分支,不重放副作用)。

    恢复类(recovery)两阶段:
    - phase1:正常 run(可能未修完,验证失败)→ 已写分支落盘。
    - phase2:同 loop 实例再 run 一次 → 自动 resume → 完成修复。
    recovered = phase2 验证通过(resume 成功,恢复率口径)。
    """
    if repo is None:
        repo = prepare_repo(None)
    plan = Plan(
        description=task.task,
        files=list(task.plan_files),
        commands=[VERIFY_CMD],
    )
    collector = _TokenCollector()
    counter = _ToolCounter()
    gate = _auto_gate if auto_approve else None

    # 合并采集:usage(token)+ 工具成败(record_event 走 usage 事件,events 参数不带 usage)
    async def _record(ev: StepEvent) -> None:
        await collector(ev)
        await counter(ev)

    is_recovery = task.kind == "recovery"
    # 恢复类:固定 session_id(两阶段共用,第二次 run 自动 resume)
    sid = session_id or (f"eval-{task.id}" if is_recovery else None)
    loop = VerifiedLoop(
        config,
        plan=plan,
        strict=False,
        approval_gate=gate,
        max_fix_rounds=max_fix_rounds,
        test_command=task.verify,
        session_id=sid,
        transport=transport,
        record_event=_record,
    )

    t0 = time.perf_counter()
    try:
        # 注意:record_event(构造注入)采集 usage/token;events 参数(实时回调)不携带 usage。
        report = await loop.run(repo, task.task)
        if is_recovery:
            # phase2:同 repo + 同 session 再 run → VerifiedLoop 自动 resume
            report = await loop.run(repo, task.task)
    except BaseException as exc:  # noqa: BLE001 - 评测不容许任务悬挂,异常 → 记失败
        elapsed = time.perf_counter() - t0
        return EvalResult(
            task_id=task.id,
            ok=False,
            passed=False,
            changed_files=[],
            out_of_scope=False,
            elapsed=elapsed,
            tokens=collector.total,
            tool_calls=counter.calls,
            tool_errors=counter.errors,
            is_recovery=is_recovery,
            has_write_plan=bool(task.plan_files),
            is_readonly=task.kind == "readonly",
            error=str(exc),
        )
    elapsed = time.perf_counter() - t0

    # 两个率**必须来自不同的测量**,否则它们恒等(见 `_acceptance_ok` 说明):
    # - verify_passed:闭环的自我结论(worktree 内最后一轮验证);
    # - acceptance_ok:在仓库副本里独立重跑验收命令的客观结果。
    # 只读任务无验收面 → 两条都用 None 语义(不计入分母)。
    verify_passed = _last_round_passed(report)
    if task.kind == "readonly":
        acceptance_ok = True
        verify_ok = True  # 只读恒真(RiskReport.passed 同义),不进 scored 分母
    else:
        acceptance_ok = _acceptance_ok(task, report, repo)
        verify_ok = bool(report.passed)

    # 恢复类:phase2(resume)验证通过 → recovered=True(恢复率口径)
    recovered = is_recovery and report.passed

    # 越权判定:changed_files 超出 plan.files(越权修改率口径)
    out_of_scope = bool(task.plan_files) and any(
        not plan.allows(f) for f in report.changed_files
    )

    return EvalResult(
        task_id=task.id,
        ok=acceptance_ok,  # 验收命令真实通过(任务成功口径)
        passed=verify_ok,  # 闭环自我结论(验证通过率口径)
        changed_files=report.changed_files,
        out_of_scope=out_of_scope,
        elapsed=elapsed,
        tokens=collector.total,
        tool_calls=counter.calls,
        tool_errors=counter.errors,
        recovered=recovered,
        is_recovery=is_recovery,
        has_write_plan=bool(task.plan_files),
        is_readonly=task.kind == "readonly",
        verify_rounds_passed=verify_passed,
    )


async def run_eval_all(
    config: LLMConfig,
    *,
    tasks: list[EvalTask] | tuple[EvalTask, ...] | None = None,
    auto_approve: bool = True,
    transport: Any = None,
    max_fix_rounds: int | None = None,
) -> list[EvalResult]:
    """跑全部评测任务(默认 EVAL_TASKS),返回 EvalResult 列表。"""
    from eval.tasks import EVAL_TASKS

    selected = tasks if tasks is not None else EVAL_TASKS
    results: list[EvalResult] = []
    for task in selected:
        results.append(
            await run_eval_task(
                task,
                config=config,
                auto_approve=auto_approve,
                transport=transport,
                max_fix_rounds=max_fix_rounds,
            )
        )
    return results
