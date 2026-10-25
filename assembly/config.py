"""assembly/config.py —— 装配配置(whycode.toml):MCP servers + skill 选择。

用 stdlib tomllib(3.11+)解析,纯离线(无 IO 副作用)。缺文件 → 空装配。

示例 whycode.toml:

    [mcp.servers.filesystem]          # stdio server
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

    [mcp.servers.grep]                # streamable http server
    url = "http://127.0.0.1:8931/mcp"

    [skills]
    allow = ["explain"]               # 启用白名单(缺省 = 只用内置可见技能)

字段语义(要点):
- 每个 MCP server 一个子节;stdio 用 command(+args),http 用 url,互斥。
- 未识别的 transport 字段 → 装配阶段报错(拒绝静默忽略)。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class McpServerConfig:
    """一个 MCP server 的声明。label 即 [mcp.servers.<label>] 键。"""

    label: str
    transport: str  # "stdio" | "http"
    command: list[str] = field(default_factory=list)  # stdio: 可执行 + 参数
    url: str = ""  # http


@dataclass
class AssemblyConfig:
    """whycode.toml 解析结果。缺省全部为空(MCP 不连、skill 不选)。"""

    mcp_servers: list[McpServerConfig] = field(default_factory=list)
    skills_allow: list[str] = field(default_factory=list)

    @property
    def has_assembly(self) -> bool:
        return bool(self.mcp_servers or self.skills_allow)


def load_config(path: Path | None) -> AssemblyConfig:
    """解析 whycode.toml。path 为 None 或文件不存在 → 空装配(不报错)。"""
    if path is None or not path.exists():
        return AssemblyConfig()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return _parse(data, source=str(path))


def _parse(data: dict[str, Any], *, source: str) -> AssemblyConfig:
    servers: list[McpServerConfig] = []
    mcp = data.get("mcp", {})
    servers_raw = mcp.get("servers", {})
    if not isinstance(servers_raw, dict):
        raise ValueError(f"{source}: [mcp.servers] 必须是表")
    for label, spec_raw in servers_raw.items():
        if not isinstance(spec_raw, dict):
            raise ValueError(f"{source}: mcp server {label!r} 必须是表")
        spec: dict[str, Any] = spec_raw
        has_command = bool(spec.get("command"))
        has_url = bool(spec.get("url"))
        if has_command and has_url:
            raise ValueError(f"{source}: mcp server {label!r} command 与 url 互斥")
        if has_command:
            cmd = spec.get("command")
            args = [str(a) for a in spec.get("args", [])]
            # 字符串 command → 可执行文件 + args;列表形态 → 整体执行(None 已排除)
            command = (
                [cmd] + args if isinstance(cmd, str) else [str(c) for c in cmd or []]
            )
            servers.append(
                McpServerConfig(label=str(label), transport="stdio", command=command)
            )
        elif has_url:
            servers.append(
                McpServerConfig(label=str(label), transport="http", url=str(spec.get("url")))
            )
        else:
            raise ValueError(f"{source}: mcp server {label!r} 需声明 command 或 url")
    skills_raw = data.get("skills", {})
    allow_raw = skills_raw.get("allow", []) if isinstance(skills_raw, dict) else []
    allow = [str(a) for a in allow_raw] if isinstance(allow_raw, list) else []
    return AssemblyConfig(mcp_servers=servers, skills_allow=allow)
