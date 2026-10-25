"""M5 单元测试:MCP 工具桥接(参数模型 / read_only / path_key)+ 多 server 注册表。"""

from __future__ import annotations

import pytest

from mcp_client.bridge import McpTool, MCPToolRegistry, build_args_model

MCP_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path"],
}


def _fake_mcp_tool(name: str, server: str, input_schema: dict[str, object] | None = None) -> object:
    return type(
        "_Fake",
        (),
        {
            "name": name,
            "description": f"{server}:{name} 的说明",
            "input_schema": input_schema or {"type": "object", "properties": {}},
        },
    )()


class TestBuildArgsModel:
    def test_required_fields_propagate(self) -> None:
        model = build_args_model(MCP_INPUT_SCHEMA)
        assert model is not None
        fields = model.model_fields
        assert "path" in fields
        assert "content" in fields
        # path 是 required → 无默认值
        assert fields["path"].is_required() is True
        assert fields["content"].is_required() is False
    def test_empty_schema_ok(self) -> None:
        model = build_args_model({"type": "object", "properties": {}})
        assert model is not None
        assert model.model_fields == {}


class TestMcpTool:
    def test_defaults_non_readonly_no_path_key(self) -> None:
        tool = McpTool(
            name="search",
            description="搜索",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            server_label="filesystem",
        )
        assert tool.read_only is False  # MCP 工具默认非只读(保守不可重放)
        assert tool.path_key is None

    def test_path_key_inferred_from_path_property(self) -> None:
        tool = McpTool(
            name="write_echo",
            description="写",
            input_schema=MCP_INPUT_SCHEMA,
            server_label="filesystem",
        )
        # 规范 schema 带 path 属性 → 受路径沙箱约束
        assert tool.path_key == "path"

    def test_requires_server_label(self) -> None:
        with pytest.raises(ValueError):
            McpTool(
                name="x",
                description="x",
                input_schema={"type": "object", "properties": {}},
                server_label="",
            )


class TestMCPToolRegistry:
    def test_merges_multiple_servers(self) -> None:
        registry = MCPToolRegistry()
        registry.add("filesystem", [_fake_mcp_tool("read_echo", "filesystem")])
        registry.add("math", [_fake_mcp_tool("add", "math")])
        tools = registry.build()
        assert set(tools) == {"read_echo", "add"}
        assert tools["read_echo"].server_label == "filesystem"
        assert tools["add"].server_label == "math"

    def test_duplicate_name_rejected(self) -> None:
        registry = MCPToolRegistry()
        registry.add("a", [_fake_mcp_tool("dup", "a")])
        with pytest.raises(ValueError, match="duplicate"):
            registry.add("b", [_fake_mcp_tool("dup", "b")])
