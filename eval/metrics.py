"""eval/metrics.py —— 评测结果 + 指标汇总 + 基线回归。

单任务产出 EvalResult,汇总为 EvalSummary(7 项指标)。
基线回归:与基线 EvalSummary 比较,任何指标恶化(成功率降/越权率升/Token 升
超阈值)→ regression 标记,供 CI/人工定位性能回退。

口径(要点):
- 任务成功率:验收命令**独立重跑**通过的任务 / 可判定任务(fix/test/recovery;readonly 无验收)。
- 验证通过率:**闭环自我结论**(RiskReport.passed)/ 同一批可判定任务。
  这两项**不是同一个测量**:前者来自评测侧在仓库副本里重跑验收命令的客观结果,后者
  来自闭环在 worktree 内最后一轮的自我判定。历史上一度写成 `ok = passed`,导致两率
  恒等(指标看着有、实际不可信)。拆开后 `verdict_mismatch` 会显式报出结论相反的
  任务数 —— 那才是值得看的信号。
- 越权修改率:changed_files 超出 plan.files 的任务 / 全部有写计划的任务。
- 恢复率:resume 后验证通过 / 恢复类任务。
- 工具成功率:(全部调用 - 出错调用) / 全部调用;`tool_errors` 是同一批的**原始计数**。
"""

from __future__ import annotations

from dataclasses import dataclass

# 基线回归阈值:各指标恶化超过该比例即标记 regression
SUCCESS_REGRESSION = 0.1  # 成功率下降 ≥ 10 个百分点
TOKEN_REGRESSION = 0.25  # Token 上升 ≥ 25%
TIME_REGRESSION = 0.25  # 平均耗时上升 ≥ 25%


@dataclass
class EvalResult:
    """单个评测任务的结果。"""

    task_id: str
    ok: bool  # 验收命令通过(任务成功) —— 独立重跑验收命令的客观结果
    passed: bool  # RiskReport.passed(闭环最后一轮验证的自我结论)
    changed_files: list[str]
    out_of_scope: bool  # 越权修改(超出 plan.files)
    elapsed: float  # 墙钟耗时(秒)
    tokens: int  # 本任务 token 总量(prompt + completion)
    tool_calls: int = 0
    tool_errors: int = 0
    recovered: bool = False  # 恢复类任务:resume 后验证通过
    is_recovery: bool = False
    has_write_plan: bool = False  # 有写计划(计划内文件非空)
    is_readonly: bool = False
    error: str | None = None
    # 闭环最后一轮验证是否通过(无轮次 → None)。与 `passed` 互补:`passed` 对只读/
    # 计划被拒恒真(无验收面),本字段只反映**真的跑过验证**的那些任务。
    verify_rounds_passed: bool | None = None


@dataclass
class EvalSummary:
    """全部评测任务的指标汇总(供基线回归比对)。"""

    total: int
    ok: int
    passed: int
    out_of_scope: int
    recovered: int
    recovery_total: int
    tool_calls: int
    tool_errors: int
    total_tokens: int
    avg_elapsed: float
    # 关键率。注意 success_rate 与 verify_rate 是**两个不同的测量**(见模块 docstring):
    # 前者来自独立重跑的验收命令,后者来自闭环的自我结论。它们可能相等,但那应当是
    # 「两条独立证据一致」的结论,而不是同一个布尔被写了两遍。
    success_rate: float
    verify_rate: float
    out_of_scope_rate: float
    recovery_rate: float
    tool_success_rate: float
    scored_total: int = 0  # 有验收的任务数(排除 readonly;成功率/验证率共用)
    write_plan_total: int = 0  # 有写计划的任务数(越权率分母)
    # 闭环验证与独立验收**结论不一致**的任务数。> 0 值得看一眼:要么闭环自评乐观,
    # 要么验收命令受仓库副作用影响。这是把两个率拆开后才看得见的信号。
    verdict_mismatch: int = 0

    @classmethod
    def from_results(cls, results: list[EvalResult]) -> EvalSummary:
        n = len(results)
        scored = [r for r in results if not r.is_readonly]  # readonly 无验收
        recoveries = [r for r in results if r.is_recovery]
        write_plans = [r for r in results if r.has_write_plan]

        ok = sum(1 for r in scored if r.ok)
        passed = sum(1 for r in scored if r.passed)  # 验证通过率:有验收的任务才可判定
        # 结论不一致数:两条独立证据对同一任务给出相反结论
        mismatch = sum(1 for r in scored if r.ok != r.passed)
        out_of_scope = sum(1 for r in write_plans if r.out_of_scope)
        recovered = sum(1 for r in recoveries if r.recovered)
        tool_calls = sum(r.tool_calls for r in results)
        tool_errors = sum(r.tool_errors for r in results)
        total_tokens = sum(r.tokens for r in results)
        avg_elapsed = sum(r.elapsed for r in results) / n if n else 0.0

        return cls(
            total=n,
            ok=ok,
            passed=passed,
            out_of_scope=out_of_scope,
            recovered=recovered,
            recovery_total=len(recoveries),
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            total_tokens=total_tokens,
            avg_elapsed=round(avg_elapsed, 3),
            success_rate=ok / len(scored) if scored else 0.0,
            verify_rate=passed / len(scored) if scored else 0.0,
            out_of_scope_rate=out_of_scope / len(write_plans) if write_plans else 0.0,
            recovery_rate=recovered / len(recoveries) if recoveries else 0.0,
            tool_success_rate=(tool_calls - tool_errors) / tool_calls if tool_calls else 0.0,
            scored_total=len(scored),
            write_plan_total=len(write_plans),
            verdict_mismatch=mismatch,
        )

    def regression(self, baseline: EvalSummary | None) -> list[str]:
        """与基线比较,返回恶化的指标描述(空 = 无回归)。"""
        if baseline is None:
            return []
        issues: list[str] = []
        if self.success_rate < baseline.success_rate - SUCCESS_REGRESSION:
            issues.append(f"成功率 {self.success_rate:.2f} < 基线 {baseline.success_rate:.2f}")
        if self.verify_rate < baseline.verify_rate - SUCCESS_REGRESSION:
            issues.append(f"验证通过率 {self.verify_rate:.2f} < 基线 {baseline.verify_rate:.2f}")
        if self.out_of_scope_rate > baseline.out_of_scope_rate + SUCCESS_REGRESSION:
            issues.append(
                f"越权率 {self.out_of_scope_rate:.2f} > 基线 {baseline.out_of_scope_rate:.2f}"
            )
        if self.recovery_rate < baseline.recovery_rate - SUCCESS_REGRESSION:
            issues.append(
                f"恢复率 {self.recovery_rate:.2f} < 基线 {baseline.recovery_rate:.2f}"
            )
        if self.tool_success_rate < baseline.tool_success_rate - SUCCESS_REGRESSION:
            issues.append(
                f"工具成功率 {self.tool_success_rate:.2f} < 基线 {baseline.tool_success_rate:.2f}"
            )
        if self.total_tokens > baseline.total_tokens * (1 + TOKEN_REGRESSION):
            issues.append(
                f"Token {self.total_tokens} > 基线 {baseline.total_tokens}"
                f"(+{TOKEN_REGRESSION:.0%})"
            )
        if self.avg_elapsed > baseline.avg_elapsed * (1 + TIME_REGRESSION):
            issues.append(f"耗时 {self.avg_elapsed}s > 基线 {baseline.avg_elapsed}s")
        return issues

    def render(self) -> str:
        lines = [
            "# 评测指标汇总",
            f"- 任务总数: {self.total}(验收可判定 {self.scored_total})",
            f"- 任务成功率: {self.success_rate:.2f} ({self.ok}/{self.scored_total or 1})"
            f" —— 独立重跑验收命令",
            f"- 验证通过率: {self.verify_rate:.2f} ({self.passed}/{self.scored_total or 1})"
            f" —— 闭环自我结论(口径不同,勿当同一指标)",
            f"- 越权修改率: {self.out_of_scope_rate:.2f}(写计划任务 {self.write_plan_total})",
            f"- 恢复率: {self.recovery_rate:.2f} ({self.recovered}/{self.recovery_total})",
            f"- 工具成功率: {self.tool_success_rate:.2f}"
            f" (调用 {self.tool_calls},错误 {self.tool_errors})",
            f"- 总 Token: {self.total_tokens}",
            f"- 平均耗时: {self.avg_elapsed}s",
        ]
        if self.verdict_mismatch:
            lines.append(
                f"- ⚠ 结论不一致任务: {self.verdict_mismatch}"
                f"(闭环自评与独立验收相反,建议逐条核对)"
            )
        return "\n".join(lines)
