"""文件工具: read_file / write_file / list_dir。

每个工具内部都做一次路径沙箱校验(纵深防护):即使外层 runner 被绕过,
工具本身也不会读写沙箱之外的文件。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from policy.sandbox import resolve_in_sandbox
from tools.base import Tool, ToolResult, truncate_output


class ReadFileArgs(BaseModel):
    path: str = Field(description="项目内文件路径(相对 workdir)")


class WriteFileArgs(BaseModel):
    path: str = Field(description="项目内文件路径(相对 workdir)")
    content: str = Field(description="完整的新文件内容")


class ListDirArgs(BaseModel):
    path: str = Field(description="目录路径(相对 workdir)", default=".")


class ReadFileTool(Tool):
    """读取文件内容(UTF-8)。"""

    read_only: bool = True
    path_key: str | None = "path"
    # M9 W4:只读文件读取互不干扰,同轮批量可并发
    parallel_safe: bool = True

    @property
    def args_model(self) -> type[BaseModel] | None:
        return ReadFileArgs

    def run(self, args: dict[str, Any]) -> ToolResult:
        path_arg = args.get("path")
        target = resolve_in_sandbox(self.workdir, path_arg)
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ToolResult(f"文件不存在: {path_arg}", is_error=True)
        except IsADirectoryError:
            return ToolResult(f"路径是目录: {path_arg}", is_error=True)
        content, truncated = truncate_output(content)
        return ToolResult(content, truncated=truncated)


class WriteFileTool(Tool):
    """写入文件内容(覆盖)。"""

    read_only: bool = False
    path_key: str | None = "path"

    @property
    def args_model(self) -> type[BaseModel] | None:
        return WriteFileArgs

    def run(self, args: dict[str, Any]) -> ToolResult:
        path_arg = args.get("path")
        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult("content 必须是字符串", is_error=True)
        target = resolve_in_sandbox(self.workdir, path_arg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(f"写入成功: {path_arg}")


class ListDirTool(Tool):
    """列出目录内容(含名称与类型,不含子目录递归)。"""

    read_only: bool = True
    path_key: str | None = "path"
    # M9 W4:目录列举只读且独立,可并发
    parallel_safe: bool = True

    @property
    def args_model(self) -> type[BaseModel] | None:
        return ListDirArgs

    def run(self, args: dict[str, Any]) -> ToolResult:
        path_arg = str(args.get("path", "."))
        target = resolve_in_sandbox(self.workdir, path_arg)
        if not target.is_dir():
            return ToolResult(f"路径不是目录: {path_arg}", is_error=True)
        entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
        lines = [f"{'目录' if p.is_dir() else '文件'}  {p.name}" for p in entries]
        content = f"目录 {path_arg}:共 {len(lines)} 项\n" + "\n".join(lines)
        content, truncated = truncate_output(content)
        return ToolResult(content, truncated=truncated)
