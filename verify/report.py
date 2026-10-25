"""RiskReport:修改文件 / 测试命令 / 验证结果 / 剩余风险 + Diff。

风险分级规则(要点:不是拍脑袋,而是可复现的推导):
1. 最后一轮测试通过      → 低:所有验证命令通过。
2. 达到修复轮次上限仍失败 → 高:第 N 轮后测试仍失败(附输出尾部)。
3. 修改了计划外文件      → 附加「中:修改了计划外文件」——越权修改风险。
4. 验证超时             → 高:验证命令超时。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from policy.models import Plan
from verify.runner import TestResult

# 剩余风险推导中,附带的验证输出尾部长度
_RISK_OUTPUT_TAIL = 200


def last_result(rounds: list[TestResult]) -> TestResult | None:
    """最后一轮验证结果(空列表 → None)。"""
    return rounds[-1] if rounds else None


def derive_remaining_risk(
    *,
    rounds: list[TestResult],
    changed_files: list[str],
    plan: Plan,
    max_fix_rounds: int,
) -> str:
    """按上述规则推导剩余风险描述(顺序敏感:测试通过优先)。

    参数只收闭环上下文(rounds/plan/changed_files/max_fix_rounds),
    passed/rounds_used/last 都从 rounds 投影,不再单独传入。
    """
    risks: list[str] = []
    last = last_result(rounds)
    passed = last is not None and last.passed

    if last is not None and last.timed_out:
        risks.append("高:验证命令超时")
    elif not passed:
        tail = last.output[-_RISK_OUTPUT_TAIL:] if last else ""
        risks.append(f"高:第 {len(rounds)} 轮后测试仍失败:{tail}")
    else:
        risks.append("低:所有验证命令通过")

    # 计划外文件修改(越权风险)—— 需要 plan 声明过文件范围(复用 Plan.allows,避免重复实现)
    if plan.files:
        extra = [f for f in changed_files if not plan.allows(f)]
        if extra:
            risks.append(f"中:修改了计划外文件 {extra}")

    return "；".join(risks)


class RiskReport(BaseModel):
    """VerifiedLoop 的完整产出:修改文件/验证结果/剩余风险 + Diff。"""

    repo: Path
    base_commit: str
    branch: str
    changed_files: list[str]
    test_command: str
    rounds: list[TestResult] = Field(default_factory=list)
    rounds_used: int = 0
    remaining_risk: str = ""
    diff: str = ""
    readonly: bool = False
    readonly_reply: str = ""  # M9 W6:只读分析 pass 的模型最终回答(CLI/前端可展示)
    # M10:计划审批门拒绝时置 True —— 闭环在任何 worktree/写入之前提前返回。
    plan_rejected: bool = False
    plan: Plan | None = None  # 本次闭环采用的计划(注入或 Planner 生成;审批门输入)
    # M12:本次运行标识(与 session 记录里的 run_id 一致,供 Trace/摘要关联)
    run_id: str = ""

    @property
    def passed(self) -> bool:
        """最后一轮是否通过(从 rounds 投影,单一来源)。

        只读分析 pass(W6):无验证轮次、无写入 → 恒真。只读任务没有
        「验收通过」的失败面——模型要么完成了分析(有内容),要么没产出;
        即便后者也不应视为编码闭环的失败(没有改动可验收)。

        计划被拒(M10):未做任何修改 → 无验收面,恒真(拒绝不算任务失败,
        是用户主动的安全决策)。
        """
        if self.readonly or self.plan_rejected:
            return True
        last = self.last_round
        return last is not None and last.passed

    @property
    def last_round(self) -> TestResult | None:
        return last_result(self.rounds)

    def render(self) -> str:
        """人类可读的 markdown 报告(CLI 打印/前端展示)。"""
        if self.readonly:
            verify_state = "只读分析(无文件改动)"
        elif self.plan_rejected:
            verify_state = "计划被拒绝(未做修改)"
        else:
            verify_state = "通过" if self.passed else "失败"
        lines = [
            "# 风险报告",
            f"- 仓库: {self.repo}",
            f"- 基线提交: {self.base_commit}",
            f"- 分支: {self.branch}",
            f"- 修改文件({len(self.changed_files)}): {', '.join(self.changed_files) or '(无)'}",
            f"- 验证命令: {self.test_command}",
            f"- 验证结果: {verify_state}",
            f"- 修复轮次: {self.rounds_used}",
            f"- 剩余风险: {self.remaining_risk}",
            "",
        ]
        if self.plan_rejected:
            lines.append("## 计划被拒绝")
            lines.append("用户在写阶段开始前拒绝了计划 → 未创建 worktree/分支、未做任何修改。")
            if self.plan is not None:
                lines.append("")
                lines.append(f"- 计划文件: {', '.join(self.plan.files) or '(无)'}")
                lines.append(f"- 计划命令: {', '.join(self.plan.commands) or '(无)'}")
            lines.append("")
            return "\n".join(lines)
        if self.readonly:
            lines.append("## 只读分析")
            lines.append("Planner 未产出修改计划(任务无需写文件)→ 按只读分析处理:")
            lines.append("不创建修改分支、不跑验证、不改动任何文件。Agent 只读探索即可作答。")
            if self.readonly_reply:
                lines.append("")
                lines.append("### Agent 分析回答")
                lines.append(self.readonly_reply)
            lines.append("")
            return "\n".join(lines)
        lines.append("## 验证记录")
        for i, r in enumerate(self.rounds, 1):
            lines.append(
                f"- 第{i}轮: {'通过' if r.passed else '失败'}"
                f"(退出码 {r.exit_code}, {r.duration_ms}ms"
                + (", 超时" if r.timed_out else "")
                + ")"
            )
        lines.append("")
        lines.append("## Diff")
        lines.append(self.diff or "(无修改)")
        return "\n".join(lines)
