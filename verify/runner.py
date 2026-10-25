"""确定性测试执行:验证命令的最终裁决(编排侧,不经过权限/审批)。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from tools.base import truncate_output

# 验证输出截断上限(含 stderr 拼接后),避免报告/上下文被刷爆
MAX_VERIFY_OUTPUT = 4000


class TestResult(BaseModel):
    """一次验证命令的完整结果。"""

    __test__ = False  # 防止 pytest 把它当作测试类收集

    command: str
    passed: bool
    exit_code: int | None
    output: str  # 截断后的 stdout+stderr
    duration_ms: float | None
    timed_out: bool = False


class TestRunner:
    """在指定工作目录确定性执行验证命令。"""

    __test__ = False  # 防止 pytest 把它当作测试类收集

    def __init__(self, cwd: Path, *, timeout: float = 120.0) -> None:
        self.cwd = cwd
        self.timeout = timeout

    def run(self, command: str) -> TestResult:
        """执行 command 并返回结构化结果。永不抛异常(超时/异常 → timed_out/失败)。"""
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.cwd),
                capture_output=True,
                text=False,  # 原始字节,显式 UTF-8 解码(Windows 控制台 GBK)
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                command=command,
                passed=False,
                exit_code=None,
                output="(验证命令超时)",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 - 验证不能冒泡
            return TestResult(
                command=command,
                passed=False,
                exit_code=None,
                output=f"(验证执行异常: {exc})",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        # stdout 优先;为空时回退 stderr;两者都空给占位
        if stdout:
            output = stdout
            if stderr:
                output = f"{stdout}\n--- stderr ---\n{stderr}"
        else:
            output = stderr or "(无输出)"
        output, _ = truncate_output(output, limit=MAX_VERIFY_OUTPUT)
        return TestResult(
            command=command,
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output=output,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
