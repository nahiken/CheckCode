"""assembly/mcp 单元测试:连接装配 + handler 绑定 + 健康探测(M9 W3 / C 包接线)。

CI 全离线,不 spawn 真实 MCP server:用假 client 工厂(monkeypatch MCPAssembly
的 client_factory)注入「返回假 metas 的 client」→ 只测装配/绑定/关闭/探测逻辑。
"""

from __future__ import annotations

from typing import Any

import pytest

from assembly.config import McpServerConfig
from assembly.mcp import MCPAssembly
from mcp_client.client import McpToolMeta


class FakeMCPClient:
    """假 MCP client:connect 返回工具 metas,call_tool 记录调用,close 标记。"""

    def __init__(
        self, label: str, metas: list[McpToolMeta], *, healthy: bool = True
    ) -> None:
        self.server_label = label
        self._metas = metas
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.healthy = healthy

    async def connect(self) -> list[McpToolMeta]:
        return list(self._metas)

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        return f"[{self.server_label}] {name}:ok"

    async def health_check(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        self.closed = True


def _meta(name: str, *, desc: str = "", path: bool = False) -> McpToolMeta:
    props = {"type": "object", "properties": {}}
    if path:
        props["properties"]["path"] = {"type": "string"}
    return McpToolMeta(name=name, description=desc, input_schema=props, server_label="fake")


def _stdio(label: str) -> McpServerConfig:
    return McpServerConfig(label=label, transport="stdio", command=["echo"])


async def _call(tool: Any, args: dict[str, Any]) -> str:
    """经 Tool 的 async_run 执行(与 ToolRunner 相同路径)。"""
    result = await tool.async_run(args)
    return result.content


@pytest.mark.asyncio
async def test_assembles_tools_with_handler_bound() -> None:
    """连接装配:工具进 registry,handler 绑定后调用转发到 client.call_tool。"""
    fakes: dict[str, FakeMCPClient] = {}
    fake = FakeMCPClient("fs", [_meta("read_path", path=True), _meta("write")])
    fakes["fs"] = fake

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        return fakes[cfg.label]

    async with MCPAssembly([_stdio("fs")], client_factory=factory) as asm:
        extra = asm.build_extra_tools()
        assert set(extra) == {"read_path", "write"}
        # handler 已绑定:调用转发到 client
        await _call(extra["read_path"], {"path": "/tmp/a"})
        assert fake.calls == [("read_path", {"path": "/tmp/a"})]
        # path 属性 → 自动受路径沙箱约束(path_key 推导,桥接既有语义)
        assert extra["read_path"].path_key == "path"


@pytest.mark.asyncio
async def test_exit_closes_all_clients() -> None:
    """async with 退出 → 所有 client.close() 被调用(防子进程泄漏)。"""
    fakes = {
        "a": FakeMCPClient("a", [_meta("tool_a")]),
        "b": FakeMCPClient("b", [_meta("tool_b")]),
    }

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        return fakes[cfg.label]

    async with MCPAssembly(
        [_stdio("a"), _stdio("b")], client_factory=factory
    ) as asm:
        assert asm.tool_count == 2
    assert fakes["a"].closed and fakes["b"].closed


@pytest.mark.asyncio
async def test_connect_failure_raises_and_closes_prior() -> None:
    """某 server 连接失败 → 抛 ConnectionError,已连的也关闭(不泄漏)。"""
    fakes: dict[str, FakeMCPClient] = {
        "good": FakeMCPClient("good", [_meta("t")]),
    }

    class BoomClient(FakeMCPClient):
        async def connect(self) -> list[McpToolMeta]:
            raise RuntimeError("server down")

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        if cfg.label == "boom":
            return BoomClient("boom", [])
        return fakes[cfg.label]

    with pytest.raises(ConnectionError, match="boom"):
        async with MCPAssembly(
            [_stdio("good"), _stdio("boom")], client_factory=factory
        ):
            pass
    # 失败时入口 exit stack 已关闭 good
    assert fakes["good"].closed


@pytest.mark.asyncio
async def test_duplicate_tool_across_servers_rejected() -> None:
    """两 server 暴露同名工具 → 装配抛错(拒绝跨 server 歧义)。"""
    a = FakeMCPClient("a", [_meta("dup")])
    b = FakeMCPClient("b", [_meta("dup")])

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        return a if cfg.label == "a" else b

    with pytest.raises(ValueError, match="duplicate"):
        async with MCPAssembly([_stdio("a"), _stdio("b")], client_factory=factory):
            pass


@pytest.mark.asyncio
async def test_empty_servers_builds_nothing() -> None:
    """无 server → 空工具表(不抛错)。"""
    async with MCPAssembly([]) as asm:
        assert asm.build_extra_tools() == {}
        assert asm.tool_count == 0
    assert await asm.health_report() == {}  # 无 server → 空报告(不是 None)


@pytest.mark.asyncio
async def test_health_report_per_server() -> None:
    """逐 server 健康探测:label → 可用?

    C 包接线:`health_check` 此前只被测试调用,装配层从不探测 —— 「服务挂了
    还照常派活」是静默故障。本测试锁住探测本身可用且**不摘除**工具。
    """
    a = FakeMCPClient("a", [_meta("t_a")], healthy=True)
    b = FakeMCPClient("b", [_meta("t_b")], healthy=False)

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        return a if cfg.label == "a" else b

    async with MCPAssembly([_stdio("a"), _stdio("b")], client_factory=factory) as asm:
        report = await asm.health_report()
        assert report == {"a": True, "b": False}
        # 不健康**不移除**工具:移除会让「本该报错」变成「工具凭空消失」
        assert set(asm.build_extra_tools()) == {"t_a", "t_b"}


@pytest.mark.asyncio
async def test_health_report_swallows_probe_exception() -> None:
    """单个 server 探测抛异常 → 记 False,不影响其它 server,不拖垮装配。"""
    a = FakeMCPClient("a", [_meta("t_a")])
    b = FakeMCPClient("b", [_meta("t_b")])

    async def boom() -> bool:
        raise RuntimeError("probe exploded")

    b.health_check = boom  # type: ignore[method-assign]

    def factory(cfg: McpServerConfig) -> FakeMCPClient:
        return a if cfg.label == "a" else b

    async with MCPAssembly([_stdio("a"), _stdio("b")], client_factory=factory) as asm:
        assert await asm.health_report() == {"a": True, "b": False}
