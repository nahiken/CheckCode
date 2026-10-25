"""M5 端到端测试:MCP 工具接入 ToolRunner + AgentLoop 全链路。

用真实 stdio MCP server(与 test_client_integration 共用最小 server):
- write_echo(写):把内容写入 server 端文件(带 path 参数,非只读)
- read_echo(读):读取指定文件

验证:
1. ToolRunner 合并 MCP 工具 → tools_spec 含之。
2. 权限链:MCP 写工具(strict + 计划命中)→ permit + server 端落地 + 审计落盘;
   strict + 未命中计划 → deny,server 端未执行。
3. AgentLoop:mock 模型发起 MCP 工具调用 → 结果回灌 → 模型看到结果。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_client.bridge import MCPToolRegistry
from mcp_client.client import MCPClient
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from tools.runner import ToolRunner

SERVER_CODE = r'''
import asyncio
from pathlib import Path
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("m5-e2e-server")
_echo_calls = 0


@mcp.tool()
def read_echo(path: str) -> str:
    """读取文件内容。"""
    return Path(path).read_text(encoding="utf-8")


@mcp.tool()
def write_echo(path: str, content: str) -> str:
    """把内容写入文件(副作用操作)。"""
    global _echo_calls
    _echo_calls += 1
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")
    return f"written:{_echo_calls}"


if __name__ == "__main__":
    mcp.run()
'''


@pytest.fixture()
async def mcp_tools(tmp_path: Path, sample_dir: Path) -> Any:
    """启动真实 MCP server(cwd=sample_dir,server 端文件操作落在沙箱内)。"""
    server_file = tmp_path / "e2e_server.py"
    server_file.write_text(SERVER_CODE, encoding="utf-8")
    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "python.exe"

    client = MCPClient(
        server_label="e2e",
        transport="stdio",
        command=[str(venv_python), str(server_file)],
        cwd=str(sample_dir),
    )
    try:
        metas = await client.connect()
        registry = MCPToolRegistry()
        registry.add("e2e", metas)
        tools = registry.build()
        # 注入 handler:经真实 client 调用
        for tool in tools.values():
            tool._handler = lambda name, args: client.call_tool(name, args)
        yield {"tools": tools, "client": client}
    finally:
        await client.close()


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    d = tmp_path / "srv"
    d.mkdir()
    return d


async def test_runner_merges_mcp_tools(mcp_tools: dict[str, Any]) -> None:
    """ToolRunner 合并 MCP 工具 → tools_spec 可见。"""
    tools = mcp_tools["tools"]
    runner = ToolRunner(Path("."), extra_tools=tools)
    spec = runner.tools_spec
    assert "read_echo" in spec
    assert "write_echo" in spec
    # MCP 工具默认非只读
    assert spec["write_echo"]["read_only"] is False


async def test_plan_hit_writes_and_audits(
    mcp_tools: dict[str, Any], sample_dir: Path
) -> None:
    """normal + 计划命中 → MCP 写工具放行执行,审计落盘。"""
    tools = mcp_tools["tools"]
    plan = Plan(files=["out.txt"])
    runner = ToolRunner(
        sample_dir, mode="normal", plan=plan, extra_tools=tools,
        audit=None,  # 默认审计到 workdir/.whycode/
    )
    result = await runner.dispatch("write_echo", {"path": "out.txt", "content": "hello"})
    assert result.is_error is False
    assert "written:" in result.content
    # server 端真实落地
    assert (sample_dir / "out.txt").read_text(encoding="utf-8") == "hello"
    # 审计落盘(granted=True)
    audit_file = sample_dir / ".whycode" / "audit.jsonl"
    assert audit_file.exists()
    assert '"granted":true' in audit_file.read_text(encoding="utf-8")


async def test_strict_plan_miss_denied(
    mcp_tools: dict[str, Any], sample_dir: Path
) -> None:
    """strict + 计划未命中 → MCP 写工具被拒,server 端未执行。"""
    tools = mcp_tools["tools"]
    plan = Plan(files=["allowed.txt"])
    runner = ToolRunner(
        sample_dir, mode="strict", plan=plan, extra_tools=tools,
    )
    result = await runner.dispatch("write_echo", {"path": "other.txt", "content": "x"})
    assert result.is_error is True
    assert "deny" in result.content.lower() or "拒绝" in result.content
    assert not (sample_dir / "other.txt").exists()


async def test_agent_loop_full_chain(mcp_tools: dict[str, Any], sample_dir: Path) -> None:
    """AgentLoop + MCP 工具:模型发起调用 → server 执行 → 结果回灌给模型。"""
    tools = mcp_tools["tools"]
    plan = Plan(files=["out.txt"])
    # read_echo 是只读?不——MCP 工具默认非只读。用 normal + 计划命中放行。
    runner = ToolRunner(sample_dir, mode="normal", plan=plan, extra_tools=tools)

    # mock 模型:第一轮调用 read_echo,第二轮文本结束
    args = json.dumps({"path": "out.txt"})
    requests = [
        tool_delta(0, id_="call_1", name="read_echo", args=args) + finish("tool_calls") + usage_block(),
        text_delta("文件内容是:hello") + finish() + usage_block(),
    ]
    request_iter = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    from agent.config import LLMConfig
    from agent.loop import build_agent

    config = LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=5)
    loop = build_agent(
        config, runner.tools_spec, runner.dispatch,
        system_prompt="测试", transport=httpx.MockTransport(handler),
    )
    messages = await loop.run("读取 out.txt 内容")
    final = messages[-1]
    assert final.role == "assistant"
    assert "hello" in final.content
    # 工具调用被记录到消息历史
    assert any(m.role == "tool" for m in messages)
