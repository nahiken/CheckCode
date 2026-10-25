"""demo_repo 的测试(由 Agent 或人工触发验证)。"""

from __future__ import annotations

import pytest

from math_utils import add, divide, fib, subtract


def test_add() -> None:
    assert add(1, 2) == 3


def test_subtract() -> None:
    assert subtract(5, 3) == 2


def test_divide() -> None:
    assert divide(10, 2) == 5.0


def test_divide_by_zero_raises_value_error() -> None:
    # 已知缺陷:当前抛 ZeroDivisionError,应抛 ValueError
    with pytest.raises(ValueError):
        divide(1, 0)


def test_fib() -> None:
    assert fib(10) == 55


def test_fib_negative_returns_minus_one() -> None:
    # 已知缺陷:当前对负数返回负数,应返回 -1
    assert fib(-5) == -1
