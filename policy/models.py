"""权限判定数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunMode = Literal["normal", "strict"]
DecisionKind = Literal["permit", "deny", "ask"]
# 分层权限的判定层(审计 layer 字段的取值来源;新层加入时在此登记)
DecisionLayer = Literal[
    "sandbox",
    "blacklist",
    "skill",
    "rules",
    "plan",
    "mode",
    "schema",
    "timeout",
    "hook",
    "unknown",
]


def _normalize_path(path: str) -> str:
    """路径归一化:反斜杠转正斜杠,去掉前导 ./ 与尾部 /。"""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


class Plan(BaseModel):
    """已审批的计划:声明允许操作的文件白名单 + 命令白名单。

    - 普通模式:写操作命中计划 → 直接放行;未命中 → 视为越权,需确认。
    - 严格模式:写操作必须命中计划,且仍需人工审批。

    files 用相对 workdir 路径;commands 用命令前缀匹配(命令的精确校验交给黑名单+审批)。
    """

    description: str = ""
    files: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)

    def allows(self, path: str | None) -> bool:
        """path 是否在计划允许的文件范围内(精确匹配或目录前缀)。"""
        if not path:
            return False
        norm = _normalize_path(path)
        for f in self.files:
            base = _normalize_path(f)
            if not base:
                continue
            if norm == base or norm.startswith(base + "/"):
                return True
        return False

    def allows_command(self, command: str) -> bool:
        """command 是否命中计划允许的命令(前缀匹配,保守)。"""
        cmd = command.strip()
        return bool(cmd) and any(cmd.startswith(allowed) for allowed in self.commands)


class Rule(BaseModel):
    """用户配置的权限规则:工具匹配 + 可选参数子串匹配 → 效果。

    按列表顺序判定,首个命中生效(可表达"先允许后拒绝"的覆盖语义)。
    """

    tool: str = "*"
    effect: DecisionKind = "deny"
    pattern: str | None = Field(default=None, description="对 run_command 的 command 做子串匹配")

    def matches(self, tool_name: str, args: dict[str, object]) -> bool:
        if self.tool != "*" and self.tool != tool_name:
            return False
        if self.pattern is None:
            return True
        return self.pattern in str(args.get("command", ""))


class PermissionDecision(BaseModel):
    """分层判定的结论。layer 记录命中的层级,供审计定位。"""

    decision: DecisionKind
    layer: DecisionLayer
    reason: str = ""
