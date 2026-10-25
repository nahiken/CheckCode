"""M5 单元测试:指数退避 + 可重试判定 + 超时不自动重放。

可靠性核心(要点):连接层失败(可重试,幂等安全)与业务调用超时(可能已执行,
重试会重复副作用——与 M4 恢复语义衔接)**分开对待**。
"""

from __future__ import annotations

import pytest

from mcp_client.retry import (
    ConnectionFailed,
    RetryPolicy,
    call_with_retry,
    is_retryable,
)


class TestRetryPolicy:
    def test_should_retry_within_limit(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(0) is True
        assert policy.should_retry(1) is True
        assert policy.should_retry(2) is False  # 已达 max_attempts

    def test_should_retry_single_attempt(self) -> None:
        policy = RetryPolicy(max_attempts=1)
        assert policy.should_retry(0) is False  # 仅一次机会,失败即放弃

    def test_backoff_exponential_with_cap(self) -> None:
        policy = RetryPolicy(base_delay=0.05, max_delay=0.5)
        # 指数增长:0.05, 0.1, 0.2, 0.4, 然后封顶 0.5
        delays = [policy.backoff_delay(i) for i in range(6)]
        assert delays[0] == pytest.approx(0.05)
        assert delays[1] == pytest.approx(0.1)
        assert delays[2] == pytest.approx(0.2)
        assert delays[3] == pytest.approx(0.4)
        assert delays[4] == pytest.approx(0.5)  # 封顶
        assert delays[5] == pytest.approx(0.5)


class TestRetryable:
    def test_connection_failed_is_retryable(self) -> None:
        assert is_retryable(ConnectionFailed("连接失败")) is True

    def test_timeout_not_retryable(self) -> None:
        # 业务调用超时:可能已执行,不自动重放
        assert is_retryable(TimeoutError("超时")) is False

    def test_unknown_error_not_retryable(self) -> None:
        assert is_retryable(ValueError("参数错误")) is False


class TestCallWithRetry:
    async def test_succeeds_after_transient_connection_failures(self) -> None:
        """连接失败重试后成功。"""
        attempts = 0

        async def _flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionFailed("暂时连接失败")
            return "ok"

        policy = RetryPolicy(max_attempts=3, base_delay=0.001, max_delay=0.01)
        result = await call_with_retry(_flaky, policy=policy)
        assert result == "ok"
        assert attempts == 3

    async def test_gives_up_after_max_attempts(self) -> None:
        """持续连接失败 → 重试到上限后抛最终错误。"""
        attempts = 0

        async def _always_fail() -> str:
            nonlocal attempts
            attempts += 1
            raise ConnectionFailed("持续失败")

        policy = RetryPolicy(max_attempts=3, base_delay=0.001, max_delay=0.01)
        with pytest.raises(ConnectionFailed):
            await call_with_retry(_always_fail, policy=policy)
        assert attempts == 3

    async def test_timeout_not_retried(self) -> None:
        """业务调用超时 → 不重试,直接抛错(即使配置了重试)。"""
        attempts = 0

        async def _timing_out() -> str:
            nonlocal attempts
            attempts += 1
            raise TimeoutError("工具调用超时,可能已执行")

        policy = RetryPolicy(max_attempts=3, base_delay=0.001, max_delay=0.01)
        with pytest.raises(TimeoutError):
            await call_with_retry(_timing_out, policy=policy)
        assert attempts == 1  # 只试一次,绝不自动重放
