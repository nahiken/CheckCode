"""工具注册表:统一构建工具集 + 生成模型可读的 tools_spec。

说明:registry 与 runner 相互导入。runner 导入 registry(构建工具集 + spec);
registry 不导入 runner,保持单向依赖(避免环)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.commands import RunCommandTool
from tools.files import ListDirTool, ReadFileTool, WriteFileTool


def build_tools(workdir: Path, tool_timeout: float = 120.0) -> dict[str, Tool]:
    """构建正式工具集(显式映射工具名 → 实例,避免名称派生魔法)。"""
    return {
        "read_file": ReadFileTool(workdir=workdir),
        "write_file": WriteFileTool(workdir=workdir),
        "list_dir": ListDirTool(workdir=workdir),
        "run_command": RunCommandTool(workdir=workdir, tool_timeout=tool_timeout),
    }


def tools_spec_from(tools: dict[str, Tool]) -> dict[str, dict[str, Any]]:
    """从工具实例生成模型可读的 {name: {description, parameters}} 规格。

    同时携带 read_only / path_key / parallel_safe 分类字段(权限层与
    分区的唯一分类来源,避免 evaluator/loop 硬编码工具名集合)。
    """
    spec: dict[str, dict[str, Any]] = {}
    for name, tool in tools.items():
        spec[name] = {
            "description": tool.description,
            "parameters": tool.schema_dict,
            "read_only": tool.read_only,
            "path_key": tool.path_key,
            "parallel_safe": tool.parallel_safe,
        }
    return spec
