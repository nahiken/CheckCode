"""run_command:在项目内执行命令(受权限/黑名单/Hook 约束)。

安全要点:
- 以 shell 字符串执行(命令的语义由用户给定;权限层负责判定命令是否可执行)
- 显式超时(默认继承 runner 的 tool_timeout),超时返回错误 ToolResult
- 纯 subprocess 接口(不依赖 shell 继承输出),与 asyncio 解耦
"""

from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from tools.base import Tool, ToolResult, truncate_output


class RunCommandArgs(BaseModel):
    command: str = Field(description="要执行的命令行(如: python -m pytest)")
    timeout: float | None = Field(default=None, description="超时秒数,缺省用全局工具超时")


class RunCommandTool(Tool):
    """在项目目录内执行命令行,返回 stdout+stderr。"""

    @property
    def args_model(self) -> type[BaseModel] | None:
        return RunCommandArgs

    def run(self, args: dict[str, Any]) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult("command 不能为空", is_error=True)
        timeout = args.get("timeout")
        timeout_f = float(timeout) if timeout is not None else self.tool_timeout
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workdir),
                capture_output=True,
                text=False,  # Windows 控制台默认 GBK:原始字节,按 UTF-8 解码
                timeout=timeout_f,
            )
        except subprocess.TimeoutExpired:
            msg = f"命令超时({timeout_f:.1f}s): {command}"
            return ToolResult(msg, is_error=True, truncated=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        content = stdout if stdout else (stderr or "(无输出)")
        content, truncated = truncate_output(content)
        if proc.returncode != 0:
            return ToolResult(
                f"命令失败(退出码 {proc.returncode}):\n{content}",
                is_error=True,
                truncated=truncated,
            )
        return ToolResult(content, truncated=truncated)
