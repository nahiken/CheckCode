"""assembly/mcp.py —— MCP 装配:连接 servers、绑 handler、聚合工具、退出关闭。

生产路径(mewcode run --config):把 whycode.toml 声明的 MCP server 逐个连上,
工具发现后注册进 MCPToolRegistry,并把每个 server 的 call_tool 绑成 handler;
聚合出的工具表(extra_tools)注入 ToolRunner.extra_tools → 走统一安全链。

生命周期:
    async with MCPAssembly(servers) as asm:   # 全部连上(单个失败 → 抛错,整体关闭)
        extra = asm.build_extra_tools()
    # 退出时 AsyncExitStack 逆序关闭所有连接(stdio 子进程终止/HTTP 关闭)

测试策略:CI 全离线,不 spawn 真实 MCP server。装配层测试用「假 client 工厂」
(monkeypatch MCPAssembly._connect_server)注入伪 client → 只测装配/绑定逻辑。
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from assembly.config import McpServerConfig
from mcp_client.bridge import McpTool, MCPToolRegistry
from mcp_client.client import MCPClient

# MCP 工具装配的 handler 签名(与 McpTool._handler / MCPClient.call_tool 对齐)
McpHandler = Callable[[str, dict[str, Any]], Awaitable[str]]


class MCPAssembly:
    """连接一组 MCP server,聚合其工具为 extra_tools(带 handler)。

    clients:label → MCPClient(已连)。close() 幂等;async with 用法见模块 docstring。
    """

    def __init__(
        self,
        servers: list[McpServerConfig],
        *,
        client_factory: Callable[[McpServerConfig], MCPClient] | None = None,
    ) -> None:
        self._servers = servers
        # 可注入工厂(测试):默认按声明建真实 client(连接才 spawn)
        self._client_factory = client_factory or MCPAssembly._default_client
        self._exit_stack = AsyncExitStack()
        self.clients: dict[str, MCPClient] = {}
        self.registry = MCPToolRegistry()

    @staticmethod
    def _default_client(cfg: McpServerConfig) -> MCPClient:
        if cfg.transport == "stdio":
            return MCPClient(server_label=cfg.label, transport="stdio", command=cfg.command)
        if cfg.transport == "http":
            return MCPClient(server_label=cfg.label, transport="http", url=cfg.url)
        raise ValueError(f"未知 MCP transport: {cfg.transport!r}")

    async def __aenter__(self) -> MCPAssembly:
        try:
            for cfg in self._servers:
                client = self._client_factory(cfg)
                try:
                    # connect:握手 + 工具发现;失败 → 抛错(整体装配失败,不静默跳过)
                    metas = await client.connect()
                except Exception as exc:
                    # 连接失败:先关闭当前 client,再清空 exit_stack 里已连的
                    # (已 push 的 close 守卫会逐个执行),再向上抛。
                    with contextlib.suppress(Exception):
                        await client.close()
                    await self.aclose()
                    raise ConnectionError(
                        f"[{cfg.label}] MCP 连接失败,装配中止: {exc}"
                    ) from exc
                self._exit_stack.push_async_callback(_make_close_guard(client))
                self.clients[cfg.label] = client
                # 注册工具 + 绑 handler(client.call_tool 是 async 调用入口)
                self.registry.add(cfg.label, metas, handler=_bind(client))
        except BaseException:
            # 中途失败(连接错/工具重名/其它)→ 关闭已连的,防泄漏
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """关闭全部连接(幂等)。"""
        with _suppress():
            await self._exit_stack.aclose()
        self.clients.clear()

    async def health_report(self) -> dict[str, bool]:
        """逐 server 健康探测:label → 可用?

        接线意义:`MCPClient.health_check` 此前只在测试里被调用,装配/worker 从不
        探测 —— 「服务挂了还照常派活」是个静默故障。此处把探测暴露给调用方,由
        装配层在返回工具表**之前**跑一次,把不健康的 server 外显成告警(沿用
        `asm.errors` 通道),让模型/用户知道「这些工具这一轮别指望」。

        只探测不摘除:连接层失败会被 MCPClient 的调用路径自行抛出并转成错误
        Observation,故这里探测失败**不**移出工具表 —— 移出会让「本该报错」变成
        「工具凭空消失」。探测的意义是**告警**,不是路由。

        吞异常(单个 server 探测失败不影响其它 server,也不阻断装配)。
        """
        report: dict[str, bool] = {}
        for label, client in self.clients.items():
            try:
                report[label] = await client.health_check()
            except Exception:  # noqa: BLE001 - 探测自身绝不抛出(否则装配被它拖垮)
                report[label] = False
        return report

    def build_extra_tools(self) -> dict[str, McpTool]:
        """聚合工具表(供 ToolRunner.extra_tools)。未连接(空) → {}。"""
        return self.registry.build()

    @property
    def tool_count(self) -> int:
        return len(self.registry.build())


def _bind(client: MCPClient) -> McpHandler:
    """把一个 MCP client 的 call_tool 绑成 McpTool handler(闭包持有 client)。"""

    async def handler(name: str, args: dict[str, Any]) -> str:
        return await client.call_tool(name, args)

    return handler


def _make_close_guard(client: MCPClient) -> Callable[[], Awaitable[None]]:
    """构造 async 关闭守卫(async 回调,AsyncExitStack 退出时自动 await)。

    client.close() 幂等;异常被吞(退出路径不容许因为关闭失败而抛)。
    """

    async def _close() -> None:
        with contextlib.suppress(Exception):
            await client.close()

    return _close


def _suppress() -> contextlib.AbstractContextManager[Any]:
    """吞异常上下文(contextlib.suppress(Exception) 的薄别名)。"""
    return contextlib.suppress(Exception)


