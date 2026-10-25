"""store/effect.py —— 副作用判定器:恢复重放边界的唯一来源。

恢复时「已完成副作用操作不重放」依赖对工具的分类:
- read_only=True  → 只读,可安全重放(重新喂给模型,无副作用)。
- read_only=False → 可能产生副作用(写文件/跑命令),**禁止重放**。

单一来源:`tools/registry.py build_tools` 是工具注册表,每个工具在类上声明
read_only 字段(M2 review 确立「分类字段单一来源,避免硬编码工具名集合」)。
本模块从注册表派生只读集合,不二次硬编码——工具新增/改分类自动生效。

read_only 是工具类字段,不依赖 workdir(实例化用占位路径即可)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from tools.registry import build_tools, tools_spec_from

# 占位 workdir:仅用于读取工具类字段(read_only 与 workdir 无关)
_DUMMY_WORKDIR = Path(".")


@lru_cache(maxsize=1)
def _read_only_set() -> frozenset[str]:
    """从工具注册表派生只读工具集合(单一来源)。"""
    tools = build_tools(_DUMMY_WORKDIR)
    return frozenset(
        name for name, spec in tools_spec_from(tools).items() if spec.get("read_only")
    )


def is_read_only(tool_name: str) -> bool:
    """该工具是否只读(重放安全)。未知工具按保守处理:不可重放。"""
    return tool_name in _read_only_set()
