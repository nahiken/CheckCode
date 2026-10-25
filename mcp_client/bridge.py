"""mcp_client.bridge —— MCP 工具桥接:把 MCP server 的工具变成 ToolRunner 可调度的 Tool。

- McpTool:参数模型由 MCP input_schema 动态派生(required 字段保留);默认 read_only=False
  (MCP 工具可能产生副作用,保守不可重放,与 M4 恢复语义一致);规范 schema 带 `path`
  属性时推断 path_key → 自动受路径沙箱约束。
- MCPToolRegistry:多 server 工具合并注册(单一来源);重名工具拒绝(避免歧义)。
- McpToolHandler:async 执行 MCP 调用(超时不自动重放)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from mcp_client.retry import McpCallTimeout
from tools.base import Tool, ToolResult

# 描述 content 结果时同时附上 server 来源,避免跨 server 混淆
_MISSING = object()


def _field_from_jsonschema(
    name: str, props: dict[str, Any]
) -> tuple[type[Any], Any]:
    """从 JSON Schema 的单个属性推导 Pydantic 字段类型 + 默认值。"""
    raw_type = props.get("type")
    default = props.get("default", _MISSING)
    if raw_type == "string":
        py_type: type[Any] = str
    elif raw_type in ("integer", "number"):
        py_type = int if raw_type == "integer" else float
    elif raw_type == "boolean":
        py_type = bool
    elif raw_type == "array":
        py_type = list[Any]
    elif raw_type == "object":
        py_type = dict[str, Any]
    else:
        py_type = Any
    if default is not _MISSING:
        return (py_type, default)
    # 无默认值 → 由外层决定是否必填(required 列表),这里返回哨兵
    return (py_type, _MISSING)


def build_args_model(
    input_schema: dict[str, Any], *, name: str = "McpArgs"
) -> type[BaseModel] | None:
    """把 MCP 工具 input_schema 转成 Pydantic 参数模型(required 字段保留必填)。"""
    if not input_schema or input_schema.get("type") != "object":
        return None
    props = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    fields: dict[str, tuple[type[Any], Any]] = {}
    for prop_name, prop in props.items():
        ann, default = _field_from_jsonschema(prop_name, prop)
        fields[prop_name] = (
            (ann, ...)
            if prop_name in required
            else (ann, default if default is not _MISSING else None)
        )
    # `field_definitions` 每个值支持 `tuple[Any, Any]`(annotation, default);mypy 对
    # 动态 **fields 重载提示不友好,这里用显式构造绕过。
    model: type[BaseModel] = create_model(f"{name}Args", **fields)  # type: ignore[call-overload]
    return model


def _infer_path_key(input_schema: dict[str, Any]) -> str | None:
    """规范 schema 里带 `path` 属性 → 该工具受路径沙箱约束(沿用本地工具惯例)。"""
    props = input_schema.get("properties", {})
    if "path" in props:
        return "path"
    return None


class McpTool(Tool):
    """MCP 工具的 Tool 桥接。参数由 MCP input_schema 派生;执行经 handler 走 MCP server。

    server_label / _name / _desc / _input_schema / _handler 是附加实例属性:
    ModelConfig(extra="allow") 让 BaseModel 接受这些未声明字段(不走校验)。
    超时单一来源:runner 级 tool_timeout 兜底(MCP 内部往返由 client.default_timeout 约束)。
    """

    model_config = ConfigDict(extra="allow")

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        server_label: str,
        handler: Callable[[str, dict[str, Any]], Awaitable[str]] | None = None,
    ) -> None:
        if not server_label:
            raise ValueError("MCP 工具必须携带 server_label(跨 server 来源不可省略)")
        # 用占位 workdir:read_only/path_key 与 workdir 无关(与 store/effect 同策略)
        super().__init__(workdir=Path("."))
        # extra="allow" 下直接赋值为附加字段
        self._name = name
        self._desc = description
        self._input_schema = input_schema
        self.server_label = server_label
        self._handler = handler
        self.read_only = False
        self.path_key = _infer_path_key(input_schema)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"[{self.server_label}] {self._desc}"

    @property
    def args_model(self) -> type[BaseModel] | None:
        return build_args_model(self._input_schema)

    def run(self, args: dict[str, Any]) -> ToolResult:
        """同步契约降级路径:无 handler 时明确报错。

        MCP 调用是 async(依赖主事件循环的传输流),必须走 async_run——由
        ToolRunner 在**主事件循环**里直接 await,而非经线程 executor。
        """
        if self._handler is None:
            return ToolResult(
                f"[{self.server_label}] 无执行 handler(MCP server 未连接)", is_error=True
            )
        return ToolResult(
            f"[{self.server_label}] MCP 工具必须在 async 路径执行(见 async_run)", is_error=True
        )

    async def async_run(self, args: dict[str, Any]) -> ToolResult:
        """异步契约:在主事件循环里执行 MCP 调用(与 SDK 传输流同 loop)。

        超时 → 不自动重放,返回错误(与 M4 恢复语义一致)。
        """
        if self._handler is None:
            return ToolResult(
                f"[{self.server_label}] 无执行 handler(MCP server 未连接)", is_error=True
            )
        # 内部超时由 runner 级 tool_timeout 兜底;此处不重复包装(单一来源在 runner)
        try:
            content = await self._handler(self._name, args)
        except (McpCallTimeout, TimeoutError) as exc:
            return ToolResult(
                f"[{self.server_label}] 工具超时,不自动重放: {exc}", is_error=True
            )
        except Exception as exc:
            return ToolResult(f"[{self.server_label}] MCP 调用失败: {exc}", is_error=True)
        return ToolResult(content)


class MCPToolRegistry:
    """多 MCP server 工具注册表:合并为单一 {name: McpTool} 表(单一来源)。"""

    def __init__(self) -> None:
        self._tools: dict[str, McpTool] = {}

    def add(
        self,
        server_label: str,
        mcp_tools: list[Any],
        handler: Callable[[str, dict[str, Any]], Awaitable[str]] | None = None,
    ) -> None:
        """注册一个 server 的工具。handler:该 server 的调用入口(如 client.call_tool)。

        M9 W3 装配:handler 由装配层连接后注入(生产路径),未注入的工具执行时
        返回「无执行 handler」错误(与测试/离线圈一致)。
        """
        for t in mcp_tools:
            if t.name in self._tools:
                raise ValueError(
                    f"MCP tool name duplicate: {t.name!r} (server {server_label} conflicts "
                    f"with {self._tools[t.name].server_label}), rejected"
                )
            self._tools[t.name] = McpTool(
                name=t.name,
                description=getattr(t, "description", "") or "",
                input_schema=getattr(t, "input_schema", None)
                or {"type": "object", "properties": {}},
                server_label=server_label,
                handler=handler,
            )

    def build(self) -> dict[str, McpTool]:
        return dict(self._tools)
