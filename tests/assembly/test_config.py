"""assembly/config 单元测试:whycode.toml 解析(M9 W3)。

纯离线(tomllib);覆盖 stdio/http/command+args、缺文件空、非法声明报错。
"""

from __future__ import annotations

import pytest

from assembly.config import AssemblyConfig, load_config


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_empty_when_no_path(tmp_path) -> None:
    """无路径 / 文件不存在 → 空装配(不报错)。"""
    assert load_config(None) == AssemblyConfig()
    assert load_config(tmp_path / "missing.toml") == AssemblyConfig()


def test_stdio_and_http_servers(tmp_path) -> None:
    """stdio(command+args)与 http(url)server 都解析。"""
    cfg_path = tmp_path / "whycode.toml"
    _write(
        cfg_path,
        """
[mcp.servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]

[mcp.servers.grep]
url = "http://127.0.0.1:8931/mcp"
""",
    )
    cfg = load_config(cfg_path)
    assert [s.label for s in cfg.mcp_servers] == ["filesystem", "grep"]
    fs = cfg.mcp_servers[0]
    assert fs.transport == "stdio"
    assert fs.command == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]
    assert fs.url == ""
    gr = cfg.mcp_servers[1]
    assert gr.transport == "http"
    assert gr.url == "http://127.0.0.1:8931/mcp"


def test_skills_allow(tmp_path) -> None:
    """[skills].allow 列表解析。"""
    cfg_path = tmp_path / "whycode.toml"
    _write(cfg_path, '[skills]\nallow = ["explain", "review"]\n')
    cfg = load_config(cfg_path)
    assert cfg.skills_allow == ["explain", "review"]


def test_command_and_url_mutually_exclusive(tmp_path) -> None:
    """command 与 url 同给 → 报错(拒绝歧义 transport)。"""
    cfg_path = tmp_path / "whycode.toml"
    _write(
        cfg_path,
        """
[mcp.servers.bad]
command = "npx"
url = "http://x"
""",
    )
    with pytest.raises(ValueError, match="互斥"):
        load_config(cfg_path)


def test_server_needs_command_or_url(tmp_path) -> None:
    """server 缺 command/url → 报错。"""
    cfg_path = tmp_path / "whycode.toml"
    _write(cfg_path, "[mcp.servers.empty]\n")
    with pytest.raises(ValueError, match="command 或 url"):
        load_config(cfg_path)


def test_invalid_servers_table(tmp_path) -> None:
    """[mcp.servers] 非表 → 报错。"""
    cfg_path = tmp_path / "whycode.toml"
    _write(cfg_path, "mcp = { servers = 3 }\n")
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_shipped_example_is_parseable() -> None:
    """随仓库分发的 whycode.toml.example 必须**真能被解析**。

    配置样例是用户的第一手文档:若它写得像 TOML 但解析不了、或字段名与 `_parse`
    脱节,用户拷贝后拿到的是一个静默失效的配置。故把「样例 = 可解析输入」钉成契约。
    """
    from pathlib import Path as _Path

    example = _Path(__file__).resolve().parents[2] / "whycode.toml.example"
    assert example.exists(), "配置样例缺失(交付物承诺提供可拷贝样例)"
    cfg = load_config(example)  # 不抛异常即通过
    # 样例如含**未注释**的 server 声明,应解析出对应条目(否则样例在示范什么?)
    if cfg.mcp_servers:
        assert all(s.transport in ("stdio", "http") for s in cfg.mcp_servers)
