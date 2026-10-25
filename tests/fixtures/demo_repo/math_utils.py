"""供 Agent 修改的样例模块。

当前含两个已知缺陷,供「修复类任务」评测:
1. `divide` 在除数为 0 时抛出原始 ZeroDivisionError(应改为 raise ValueError)。
2. `fib` 对负数参数返回负值(应为 -1 或抛错)。
"""

from __future__ import annotations


def add(a: int, b: int) -> int:
    return a + b


def subtract(a: int, b: int) -> int:
    return a - b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b


def fib(n: int) -> int:
    if n < 0:
        return n
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
