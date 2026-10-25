"""mcp_client.retry —— 指数退避 + 可重试判定 + 调用包装。

可靠性语义(要点):
- **连接层失败可重试**:连接/握手等「必然未执行业务」的失败,幂等安全,指数退避重试。
- **业务调用超时不可重试**:call_tool 超时意味着「服务端可能已执行」,自动重放会重复副作用
  ——与 M4 恢复语义(不重放已完成副作用)一脉相承。超时直接抛错,由上层(Agent)决定后续。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class ConnectionFailed(RuntimeError):
    """连接层失败:握手/连接/临时网络错误。幂等安全,可重试。"""


class McpCallTimeout(RuntimeError):
    """业务调用超时:可能已执行,不可自动重放。"""


class RetryPolicy:
    """指数退避策略。max_attempts:最大尝试次数(第 N 次后放弃)。"""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.05,
        max_delay: float = 0.5,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 >= 1")
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def should_retry(self, attempt: int) -> bool:
        """attempt 是已失败次数(0 起)。返回是否值得再试一次。"""
        return attempt < self.max_attempts - 1

    def backoff_delay(self, attempt: int) -> float:
        """第 attempt 次失败后的等待时长:base * 2^attempt,封顶 max_delay。"""
        delay = self.base_delay * float(2**attempt)
        return min(delay, self.max_delay)


def is_retryable(exc: BaseException) -> bool:
    """该异常是否值得重试(连接层幂等失败)。超时/业务错误不可重试。"""
    return isinstance(exc, ConnectionFailed)


def is_timeout(exc: BaseException) -> bool:
    """业务调用超时(McpCallTimeout / 原生 TimeoutError)。"""
    return isinstance(exc, (McpCallTimeout, TimeoutError))


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    retry_timeouts: bool = False,
) -> T:
    """执行可重试的调用:连接层失败按指数退避重试,其余异常直接上抛。

    retry_timeouts=True:仅用于**连接阶段**(握手/发现)——此时超时意味着进程
    未响应/握手慢,无业务副作用,幂等安全,可退避重试。业务调用(call_tool)的
    超时永远不在此重放——调用方必须自行判断副作用是否已发生。
    """
    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:
            retryable = is_retryable(exc) or (retry_timeouts and is_timeout(exc))
            if not retryable or not policy.should_retry(attempt):
                raise
            delay = policy.backoff_delay(attempt)
            await asyncio.sleep(delay)
            attempt += 1


async def with_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    """给业务调用包一层超时;超时抛 McpCallTimeout(可重试判定为不可重试)。"""
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError as exc:
        raise McpCallTimeout(f"MCP 工具调用超时({timeout:.1f}s),可能已执行,不自动重放") from exc
