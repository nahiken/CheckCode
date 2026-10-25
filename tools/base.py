"""工具基类与参数模型。

工具职责:参数声明(Pydantic 模型)+ 实际执行。权限判定与审计由
ToolRunner(policy + hooks)统一负责,工具本身不直接接触权限层。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

MAX_OUTPUT_CHARS = 8000
_TRUNC_SUFFIX = "\n...[输出过长,已截断]"


class ToolResult:
    """工具执行结果(轻量值对象,无需 pydantic)。"""

    def __init__(
        self,
        content: str,
        *,
        is_error: bool = False,
        truncated: bool = False,
    ) -> None:
        self.content = content
        self.is_error = is_error
        self.truncated = truncated


def truncate_output(content: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """工具输出截断:超过 limit 字符时裁剪并标记。"""
    if len(content) <= limit:
        return content, False
    return content[:limit] + _TRUNC_SUFFIX, True


class Tool(BaseModel):
    """工具声明。参数由 Pydantic 模型校验(runner 层经 schema 检查)。

    分类字段由权限层/分区读取(单一来源,替代 evaluator 里的硬编码字符串集合):
    - read_only:  只读工具 → 无需审批,直接放行
    - path_key:   带路径参数 → 受路径沙箱约束
    - parallel_safe: 并发安全 → 同轮多个该工具调用可放同一组并发执行。
      M9 W4:保守默认 False(不可并发,串行执行);仅显式声明为 True 的工具
      (如 read_file/list_dir)在同轮批量调用时被归并并发,其余各自独立成组。
    """

    workdir: Path
    # 单次工具执行超时(秒),run_command 等可能阻塞的工具使用
    tool_timeout: float = 120.0
    read_only: bool = False
    path_key: str | None = None
    parallel_safe: bool = False

    @property
    def args_model(self) -> type[BaseModel] | None:
        return None

    @property
    def schema_dict(self) -> dict[str, object]:
        """生成模型可读的 JSON Schema 字典。"""
        model = self.args_model
        if model is None:
            return {"type": "object", "properties": {}}
        return model.model_json_schema()

    @property
    def description(self) -> str:
        return self.__class__.__doc__ or ""

    def run(self, args: dict[str, Any]) -> ToolResult:  # pragma: no cover - 抽象方法
        raise NotImplementedError
