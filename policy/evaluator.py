"""分层权限评估器。

判定链(优先级从高到低,先命中先返回):
1. 路径沙箱   —— 带 path 的工具越界 → deny(硬边界,任何模式/计划不可绕过)
2. 高危命令   —— run_command 命中黑名单 → deny(硬边界)
3. Skill 工具白名单 —— 声明了 allowedTools 的技能只在**它列出的**本地工具内工作
                  → 未列出 → deny(layer `skill`;收窄层,不可反向绕过上面两道硬边界)
4. 权限规则   —— 用户显式规则(首个命中):deny / ask / permit
5. 计划白名单 —— 写操作命中已审批计划:normal → permit;strict → ask
6. 运行模式兜底 —— 读放行;写:normal → ask(越权确认),strict → deny(不在计划)

说明:
- 判定顺序与 LEARNING_PLAN 字面(白名单→黑名单→沙箱)相反:沙箱/黑名单是
  **不可绕过的硬边界**,必须先于任何"允许"语义判定,否则计划/规则会绕过它们。
- skill 白名单位置的理由:它**收窄**可用工具,不是授权来源,故必须排在规则/计划
  这些"放行语义"之**上**(否则 permit 规则能绕过它),但排在硬边界之**下**
  (否则收窄会成为逃逸通道)。`tools_allow is None` = 未装配技能 → 完全不限制。
- 工具分类(read_only / path_key)由 tools_spec 声明读取,新增工具无需改本模块。
- run_command 在 strict 模式下是否可执行,由 Plan.commands 审批(见 evaluate 第 5 步)。
"""

from __future__ import annotations

from pathlib import Path

from policy.models import PermissionDecision, Plan, Rule, RunMode
from policy.rules import check_high_risk
from policy.sandbox import resolve_in_sandbox


def _command_of(args: dict[str, object]) -> str:
    return str(args.get("command", ""))


class PolicyEvaluator:
    """分层权限评估器。mode / plan / rules / tools_spec 由构造参数注入。"""

    def __init__(
        self,
        workdir: Path,
        *,
        mode: RunMode = "normal",
        plan: Plan | None = None,
        rules: list[Rule] | None = None,
        tools_spec: dict[str, dict[str, object]] | None = None,
        tools_allow: set[str] | None = None,
    ) -> None:
        self.workdir = workdir
        self.mode = mode
        self._plan = plan
        self._rules = rules or []
        # 工具分类表:{name: {"read_only": bool, "path_key": str|None}}
        # 由 Tool 声明生成;缺省按保守处理(未知工具视为写、无 path 沙箱)。
        self._tools = tools_spec or {}
        # M15:Skill 声明的本地工具白名单。None = 未装配技能 → 不限制(兼容纪律);
        # set(含空集)= 收窄到该集合(空集合法,表示「本技能不用本地工具」)。
        self._tools_allow = tools_allow

    def _tool_kind(self, tool_name: str) -> tuple[bool, str | None]:
        spec = self._tools.get(tool_name, {})
        read_only = bool(spec.get("read_only", False))
        path_key = spec.get("path_key")
        return read_only, (str(path_key) if path_key is not None else None)

    def evaluate(self, tool_name: str, args: dict[str, object]) -> PermissionDecision:
        read_only, path_key = self._tool_kind(tool_name)

        # 1. 路径沙箱(硬边界)
        if path_key is not None:
            try:
                resolve_in_sandbox(self.workdir, args.get(path_key))
            except ValueError as e:
                return PermissionDecision(decision="deny", layer="sandbox", reason=str(e))

        # 2. 高危命令黑名单(硬边界)
        if tool_name == "run_command":
            hit = check_high_risk(_command_of(args))
            if hit is not None:
                return PermissionDecision(
                    decision="deny", layer="blacklist", reason=f"高危命令命中黑名单: {hit}"
                )

        # 3. Skill 工具白名单(M15:收窄层)。上面两道硬边界先判 —— 白名单不能让
        #    沙箱/黑名单失效;下面所有放行语义后判 —— permit 规则/计划也绕不过它。
        if self._tools_allow is not None and tool_name not in self._tools_allow:
            return PermissionDecision(
                decision="deny",
                layer="skill",
                reason=f"技能白名单未包含工具 {tool_name}(allowedTools 限制)",
            )

        # 4. 用户规则(显式配置优先于计划)
        for rule in self._rules:
            if rule.matches(tool_name, args):
                if rule.effect == "deny":
                    return PermissionDecision(
                        decision="deny", layer="rules", reason=f"规则拒绝: {rule.tool}"
                    )
                if rule.effect == "ask":
                    return PermissionDecision(decision="ask", layer="rules", reason="规则要求确认")
                return PermissionDecision(decision="permit", layer="rules", reason="规则允许")

        # 5. 计划白名单 + 6. 运行模式兜底
        if read_only:
            # 只读工具:放行(只读 Planner 的核心——读无需审批)
            return PermissionDecision(decision="permit", layer="mode", reason="只读工具放行")

        # 写工具:文件路径命中计划,或 run_command 命中计划命令
        in_plan = False
        plan = self._plan
        if plan is not None:
            if path_key is not None:
                in_plan = plan.allows(str(args.get(path_key, "")))
            elif tool_name == "run_command":
                in_plan = plan.allows_command(_command_of(args))

        if self.mode == "strict":
            if not in_plan:
                return PermissionDecision(
                    decision="deny", layer="plan", reason="严格模式:写操作必须命中已审批计划"
                )
            return PermissionDecision(
                decision="ask", layer="mode", reason="严格模式:写操作需人工审批"
            )

        # normal:命中计划放行;未命中/无计划 → 越权,需确认
        if in_plan:
            return PermissionDecision(decision="permit", layer="plan", reason="命中计划白名单")
        return PermissionDecision(decision="ask", layer="mode", reason="越权写操作,需要确认")
