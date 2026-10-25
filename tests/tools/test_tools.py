"""M2 单元测试:文件工具与 run_command。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.base import truncate_output
from tools.commands import RunCommandTool
from tools.files import ListDirTool, ReadFileTool, WriteFileTool


def test_truncate_output() -> None:
    content, truncated = truncate_output("x" * 100, limit=50)
    assert truncated is True
    assert len(content) < 100
    assert "已截断" in content
    content2, truncated2 = truncate_output("short", limit=50)
    assert truncated2 is False
    assert content2 == "short"


# === read_file ===

def test_read_file_ok(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    tool = ReadFileTool(workdir=tmp_path)
    result = tool.run({"path": "a.py"})
    assert not result.is_error
    assert result.content == "print(1)\n"


def test_read_file_not_found(tmp_path: Path) -> None:
    tool = ReadFileTool(workdir=tmp_path)
    result = tool.run({"path": "missing.py"})
    assert result.is_error


def test_read_file_escape_rejected(tmp_path: Path) -> None:
    tool = ReadFileTool(workdir=tmp_path)
    with pytest.raises(ValueError, match="越界"):
        tool.run({"path": "../secret.txt"})


def test_read_file_empty_args_rejected(tmp_path: Path) -> None:
    tool = ReadFileTool(workdir=tmp_path)
    with pytest.raises(ValueError, match="非空字符串"):
        tool.run({})


# === write_file ===

def test_write_file_creates_nested(tmp_path: Path) -> None:
    tool = WriteFileTool(workdir=tmp_path)
    result = tool.run({"path": "src/a.py", "content": "# new"})
    assert not result.is_error
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "# new"


def test_write_file_content_non_string(tmp_path: Path) -> None:
    tool = WriteFileTool(workdir=tmp_path)
    result = tool.run({"path": "a.py", "content": 123})
    assert result.is_error


# === list_dir ===

def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    tool = ListDirTool(workdir=tmp_path)
    result = tool.run({"path": "."})
    assert not result.is_error
    assert "文件  a.py" in result.content
    assert "目录  sub" in result.content
    assert "2 项" in result.content


def test_list_dir_not_dir(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("", encoding="utf-8")
    tool = ListDirTool(workdir=tmp_path)
    assert tool.run({"path": "f.py"}).is_error


# === run_command ===

def test_run_command_ok(tmp_path: Path) -> None:
    tool = RunCommandTool(workdir=tmp_path, tool_timeout=30.0)
    result = tool.run({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


def test_run_command_failure_returncode(tmp_path: Path) -> None:
    tool = RunCommandTool(workdir=tmp_path, tool_timeout=30.0)
    result = tool.run({"command": "echo boom && exit 3"})
    assert result.is_error
    assert "退出码 3" in result.content


def test_run_command_empty_rejected(tmp_path: Path) -> None:
    tool = RunCommandTool(workdir=tmp_path, tool_timeout=30.0)
    result = tool.run({"command": "   "})
    assert result.is_error


def test_run_command_timeout(tmp_path: Path) -> None:
    tool = RunCommandTool(workdir=tmp_path, tool_timeout=0.1)
    result = tool.run({"command": "python -c \"import time; time.sleep(2)\""})
    assert result.is_error
    assert "超时" in result.content


def test_run_command_works_in_workdir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("", encoding="utf-8")
    tool = RunCommandTool(workdir=tmp_path, tool_timeout=30.0)
    result = tool.run({"command": "ls"})
    assert "marker.txt" in result.content
