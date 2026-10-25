"""高危命令黑名单。

要点:黑名单**不可靠**——同义命令、大小写、编码(base64/hex)、环境变量拼接
都能绕过。它是"最后防线"而非主防线;真正的边界是路径沙箱 + 严格模式审批。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _HighRiskPattern:
    name: str
    pattern: re.Pattern[str]


# 覆盖 Unix / Windows 常见破坏性命令。基于原始命令行字符串检测。
# rm 采用"相对安全路径"策略:明确加 `--` 或 `./` 前缀的目标视为项目内安全;
# 其余任何被删除路径一律拒绝(尽力缓解 rm 误删,但它本质不可靠——见模块 docstring)。
_HIGH_RISK_PATTERNS: list[_HighRiskPattern] = [
    _HighRiskPattern(
        "删除系统路径",
        re.compile(r"\brm\s+(?:-+\w+\s+)*((?:/|~|/tmp/?)[^ ]*)", re.IGNORECASE),
    ),
    _HighRiskPattern(
        "删除(带安全前缀的)未标记路径",
        re.compile(r"\brm\s+(?:-\w+\s+)?(?!(-|--|\.?/))(\S+)", re.IGNORECASE),
    ),
    _HighRiskPattern("格式化文件系统", re.compile(r"\bmkfs\b", re.IGNORECASE)),
    _HighRiskPattern("fork 炸弹", re.compile(r"\(\)\s*\{[^}]*\|", re.IGNORECASE)),
    _HighRiskPattern("写入块设备", re.compile(r"\bdd\b[^|;]*\bof=\s*/dev/", re.IGNORECASE)),
    _HighRiskPattern("递归改根目录权限", re.compile(r"\bchmod\b.*\s/(\s|$)", re.IGNORECASE)),
    _HighRiskPattern(
        "重定向到系统文件",
        re.compile(r">\s*(/etc/|/dev/|/var/|/boot/)", re.IGNORECASE),
    ),
    _HighRiskPattern(
        "关机/重启",
        re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
    ),
    _HighRiskPattern(
        "下载并执行",
        re.compile(r"\b(curl|wget)\b.*\|\s*\b(sh|bash|zsh|python|python3)\b", re.IGNORECASE),
    ),
    _HighRiskPattern("Windows 格式化磁盘", re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE)),
    _HighRiskPattern(
        "Windows 递归删除盘符",
        re.compile(r"\b(rmdir|del)\b[^;|]*[a-zA-Z]:\\", re.IGNORECASE),
    ),
]


def check_high_risk(command: str) -> str | None:
    """检测高危命令。命中返回模式描述,否则返回 None。"""
    for p in _HIGH_RISK_PATTERNS:
        if p.pattern.search(command):
            return p.name
    return None
