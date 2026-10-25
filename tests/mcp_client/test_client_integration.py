"""M5 集成测试:真实 MCP server(stdio 协议)握手 + 工具发现 + 调用 + 超时不重放。

用 mcp 官方 SDK 的 server 端在子进程里跑一个最小 server:
- add: 两数相加(纯函数)
- slow: sleep(server 端计数调用次数)——用于验证「超时不自动重放」
- read_echo: 读取指定 path 文件内容(带 path 参数,供路径沙箱测试)

零网络:stdio 通过管道。真实进程启动/终止。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from mcp_client.client import MCPClient
from mcp_client.retry import ConnectionFailed, McpCallTimeout


def _server_code() -> str:
    """最小 MCP server 的源码(子进程运行,使用 mcp 2.x MCPServer API)。"""
    return r'''
import asyncio
from pathlib import Path
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("m5-test-server")
slow_calls = 0


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@mcp.tool()
def slow(seconds: float = 0.5) -> str:
    """慢调用:睡眠后返回,用于超时不重放验证。"""
    global slow_calls
    slow_calls += 1
    asyncio.run(asyncio.sleep(seconds))
    return f"slow-{slow_calls}"


@mcp.tool()
def read_echo(path: str) -> str:
    """读取文件内容(带 path 参数)。"""
    return Path(path).read_text(encoding="utf-8")


if __name__ == "__main__":
    mcp.run()
'''


def _write_server_file(tmp_path: Path) -> Path:
    server_file = tmp_path / "mcp_server.py"
    server_file.write_text(_server_code(), encoding="utf-8")
    return server_file


@pytest.fixture()
def server_file(tmp_path: Path) -> Path:
    return _write_server_file(tmp_path)


def _stdio_client(server_file: Path) -> MCPClient:
    venv_python = (
        Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "python.exe"
    )
    return MCPClient(
        server_label="m5test",
        transport="stdio",
        command=[str(venv_python), str(server_file)],
    )


class TestStdioConnection:
    async def test_connect_handshake_and_discover(self, server_file: Path) -> None:
        """连接 + initialize 握手 + 工具发现。"""
        client = _stdio_client(server_file)
        try:
            tools = await client.connect()
            names = {t.name for t in tools}
            assert {"add", "slow", "read_echo"} <= names
            # 工具元数据:name/description/input_schema 齐全
            add_tool = next(t for t in tools if t.name == "add")
            assert add_tool.server_label == "m5test"
            assert add_tool.description
            assert add_tool.input_schema.get("type") == "object"
        finally:
            await client.close()

    async def test_call_tool_returns_serialized_text(self, server_file: Path) -> None:
        """调用工具:返回序列化文本。"""
        client = _stdio_client(server_file)
        try:
            await client.connect()
            out = await client.call_tool("add", {"a": 2, "b": 3})
            assert "5" in out
        finally:
            await client.close()

    async def test_health_check_true_when_connected(self, server_file: Path) -> None:
        """健康检查:连接后可 ping。"""
        client = _stdio_client(server_file)
        try:
            await client.connect()
            assert await client.health_check() is True
        finally:
            await client.close()

    async def test_timeout_not_replayed(self, server_file: Path) -> None:
        """超时业务调用不自动重放:server 端调用次数不因超时重复增长。

        超时那次在 server 端已执行(sleep 2s 期间计数 → slow-1),客户端未等到结果。
        若客户端自动重放,本次会变成 slow-3;只出现 slow-2 证明没有重放。
        """
        client = _stdio_client(server_file)
        try:
            await client.connect()
            with pytest.raises(McpCallTimeout):
                await client.call_tool("slow", {"seconds": 2.0}, timeout=0.2)
            # 超时那次未重放 → server 端计数只 +1;本次成功调用得到 slow-2
            out = await client.call_tool("slow", {"seconds": 0.0}, timeout=5.0)
            assert "slow-2" in out
        finally:
            await client.close()

    async def test_close_terminates_process(self, server_file: Path) -> None:
        """close 后连接释放(子进程由 SDK 自管理,close 幂等可重复)。"""
        client = _stdio_client(server_file)
        await client.connect()
        assert client._session is not None
        await client.close()
        assert client._session is None
        await client.close()  # 幂等

    async def test_unreachable_command_raises_connection_failed(self, tmp_path: Path) -> None:
        """命令不存在 → 连接层失败(可重试语义)。"""
        client = MCPClient(
            server_label="ghost",
            transport="stdio",
            command=[tmp_path.as_posix() + "/no-such-python", "x.py"],
        )
        with pytest.raises(ConnectionFailed):
            await client.connect(timeout=2.0)
        await client.close()  # 幂等


# ---------- Streamable HTTP transport ----------

HTTP_SERVER_CODE = r'''
import asyncio
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("http-test")
slow_calls = 0


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@mcp.tool()
def slow(seconds: float = 1.0) -> str:
    """慢调用:用于超时不重放验证。"""
    global slow_calls
    slow_calls += 1
    asyncio.run(asyncio.sleep(seconds))
    return f"slow-{slow_calls}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=__PORT__)
'''


@pytest.fixture()
async def http_server(
    tmp_path: Path,
) -> AsyncGenerator[tuple[str, asyncio.subprocess.Process], None]:
    """在随机端口启动 streamable-http MCP server,返回 (url, proc)。"""
    import asyncio

    # 动态端口:让内核分配
    sock_server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = sock_server.sockets[0].getsockname()[1]
    sock_server.close()
    await sock_server.wait_closed()

    server_file = tmp_path / "http_server.py"
    server_file.write_text(
        HTTP_SERVER_CODE.replace("__PORT__", str(port)), encoding="utf-8"
    )
    venv_python = (
        Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "python.exe"
    )
    proc = await asyncio.create_subprocess_exec(str(venv_python), str(server_file))
    # 等待端口就绪
    for _ in range(50):
        try:
            reader, _ = await asyncio.open_connection("127.0.0.1", port)
            reader.feed_eof()
            break
        except OSError:
            await asyncio.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp", proc
    proc.terminate()
    await asyncio.wait_for(proc.wait(), timeout=5)


class TestHttpConnection:
    async def test_connect_handshake_and_call(
        self, http_server: tuple[str, asyncio.subprocess.Process]
    ) -> None:
        """HTTP 传输:连接 + 握手 + 发现 + 调用。"""
        url, proc = http_server
        client = MCPClient(server_label="http", transport="http", url=url)
        try:
            tools = await client.connect(timeout=10)
            assert {"add", "slow"} <= {t.name for t in tools}
            out = await client.call_tool("add", {"a": 1, "b": 2})
            assert "3" in out
            assert await client.health_check() is True
        finally:
            await client.close()

    async def test_http_timeout_not_replayed(
        self, http_server: tuple[str, asyncio.subprocess.Process]
    ) -> None:
        """HTTP 超时业务调用不自动重放:抛 McpCallTimeout,不重试。

        超时那次在 server 端已执行(计数 +1 → slow-1);若客户端自动重放本次会
        slow-3。只出现 slow-2 证明未重放。
        """
        url, proc = http_server
        client = MCPClient(server_label="http", transport="http", url=url)
        try:
            await client.connect(timeout=10)
            with pytest.raises(McpCallTimeout):
                await client.call_tool("slow", {"seconds": 2.0}, timeout=0.2)
            out = await client.call_tool("slow", {"seconds": 0.0}, timeout=5.0)
            assert "slow-2" in out
        finally:
            await client.close()
