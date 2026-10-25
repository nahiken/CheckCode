"""mcp_client.client —— MCP 客户端(官方 SDK 封装,Stdio + Streamable HTTP 双协议)。

职责:
- 连接:建立传输(stdio 子进程 / streamable http)→ initialize 握手(携带 client_info)→
  工具发现(list_tools → McpTool 元数据)。
- 调用:call_tool → 序列化结果为文本(截断)。
- 可靠性:健康检查(握手后 ping)、连接层失败按指数退避重试(call_with_retry)、
  **超时业务调用不自动重放**(with_timeout 抛 McpCallTimeout,不可重试)。

生命周期:connect() 建立会话;close() 释放(terminate 子进程 / 关闭连接)。幂等。
"""

from __future__ import annotations

import contextlib
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal

from mcp import ClientSession, Implementation, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from mcp_client.retry import (
    ConnectionFailed,
    McpCallTimeout,
    RetryPolicy,
    call_with_retry,
    with_timeout,
)
from tools.base import MAX_OUTPUT_CHARS, truncate_output

TransportName = Literal["stdio", "http"]
MAX_MCP_OUTPUT_CHARS = MAX_OUTPUT_CHARS
_DEFAULT_TIMEOUT = 30.0


@dataclass
class McpToolMeta:
    """MCP 工具元数据(发现结果,供注册表构建 McpTool)。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    server_label: str


def _serialize_result(result: CallToolResult) -> str:
    """把 MCP CallToolResult 序列化为文本。

    处理 text / image / structured / 其它 content 类型;统一为 UTF-8 文本。
    """
    parts: list[str] = []
    for item in result.content or []:
        try:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
        except Exception:
            pass
        try:
            parts.append(item.model_dump_json())
        except Exception:
            parts.append(str(item))
    if not parts:
        parts.append("(无输出)")
    return "\n".join(parts)


class MCPClient:
    """一个 MCP server 的客户端。transport: stdio | http。"""

    def __init__(
        self,
        *,
        server_label: str,
        transport: TransportName,
        command: list[str] | None = None,
        url: str | None = None,
        cwd: str | None = None,
        client_name: str = "whycode",
        client_version: str = "0.1.0",
        retry_policy: RetryPolicy | None = None,
        default_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if transport not in ("stdio", "http"):
            raise ValueError(f"未知 transport: {transport!r}")
        if transport == "stdio" and not command:
            raise ValueError("stdio transport 必须提供 command")
        if transport == "http" and not url:
            raise ValueError("http transport 必须提供 url")
        self.server_label = server_label
        self.transport = transport
        self.command = command
        self.url = url
        self.cwd = cwd
        self.default_timeout = default_timeout
        self.retry_policy = retry_policy or RetryPolicy()
        self._client_info = Implementation(name=client_name, version=client_version)
        self._session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()

    # ---- 连接与发现 ----

    async def connect(self, *, timeout: float | None = None) -> list[McpToolMeta]:
        """建立连接 + initialize 握手 + 工具发现。返回工具元数据列表。

        连接层失败(进程启动/握手超时)按指数退避重试——连接阶段无业务副作用,
        握手超时也幂等安全(与「业务调用超时不重放」的边界不同)。返回前已完成发现。
        生命周期:connect 后必须 close() 释放(幂等)。
        """
        effective = timeout or self.default_timeout
        return await call_with_retry(
            lambda: self._connect_once(effective),
            policy=self.retry_policy,
            retry_timeouts=True,
        )

    async def _connect_once(self, timeout: float) -> list[McpToolMeta]:
        try:
            if self.transport == "stdio":
                command = self.command
                assert command is not None  # __init__ 已校验
                params = StdioServerParameters(
                    command=command[0],
                    args=command[1:],
                    cwd=self.cwd,
                )
                read, write = await self._exit_stack.enter_async_context(
                    stdio_client(params)
                )
            else:
                url = self.url
                assert url is not None  # __init__ 已校验
                ctx = streamable_http_client(url)
                read, write = await self._exit_stack.enter_async_context(ctx)
            # mcp 2.x ClientSession 必须进入(启动 dispatcher run)才能收发请求
            session = await self._exit_stack.enter_async_context(
                ClientSession(read, write, client_info=self._client_info)
            )
            await with_timeout(session.initialize(), timeout)
            self._session = session
            result = await with_timeout(session.list_tools(), timeout)
            return [
                McpToolMeta(
                    name=t.name,
                    description=getattr(t, "description", "") or "",
                    # mcp 2.x Tool 字段名是 input_schema(Python 风格,非 JSON 的 inputSchema)
                    input_schema=dict(getattr(t, "input_schema", {}) or {}),
                    server_label=self.server_label,
                )
                for t in result.tools
            ]
        except (TimeoutError, McpCallTimeout, ConnectionFailed):
            raise
        except Exception as exc:
            raise ConnectionFailed(
                f"[{self.server_label}] MCP 连接失败({self.transport}): {exc}"
            ) from exc

    # ---- 调用 ----

    async def call_tool(
        self, name: str, args: dict[str, Any], *, timeout: float | None = None
    ) -> str:
        """调用 MCP 工具。返回序列化文本(截断)。超时不自动重放。"""
        if self._session is None:
            raise ConnectionFailed(f"[{self.server_label}] 未连接,无法调用 {name}")
        effective = timeout or self.default_timeout
        result = await with_timeout(self._session.call_tool(name, args), effective)
        content = _serialize_result(result)
        truncated, _ = truncate_output(content, limit=MAX_MCP_OUTPUT_CHARS)
        return truncated

    # ---- 可靠性 ----

    async def health_check(self) -> bool:
        """健康检查:已连接且可 ping。异常/超时 → False(不抛出)。"""
        if self._session is None:
            return False
        try:
            # mcp 2.x:ping 是 send_ping
            await with_timeout(self._session.send_ping(), timeout=min(5.0, self.default_timeout))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """释放资源(幂等):关闭 session 与传输上下文(子进程随之终止)。"""
        self._session = None
        with contextlib.suppress(Exception):
            await self._exit_stack.aclose()
