"""asyncio 并发基础 demo(阶段 1 学习脚本,非交付功能)。

演示三种 asyncio 模式,是理解 AgentLoop 并发工具调度(partition + gather)的入门:
1. await 串行:顺序等待,慢任务阻塞后续。
2. gather 并发:并行执行,总耗时≈最慢任务。
3. asyncio.Queue:生产者/消费者,Worker 池模式(工具并发的雏形)。

运行:
    uv run python scripts/demo_asyncio.py
"""

from __future__ import annotations

import asyncio
import time


async def fetch(name: str, delay: float) -> str:
    """模拟一次异步 IO(如 LLM 请求/工具调用)。"""
    await asyncio.sleep(delay)
    return f"{name} done in {delay}s"


async def demo_await_serial() -> None:
    """模式 1:await 串行 —— 总耗时 = 各任务之和。"""
    start = time.monotonic()
    r1 = await fetch("a", 0.3)
    r2 = await fetch("b", 0.3)
    elapsed = time.monotonic() - start
    print(f"[串行] {r1}, {r2} — 耗时 {elapsed:.2f}s(预期 ~0.6s)")


async def demo_gather_concurrent() -> None:
    """模式 2:gather 并发 —— 总耗时 ≈ 最慢任务(与 execute_partitions 同型)。"""
    start = time.monotonic()
    results = await asyncio.gather(fetch("a", 0.3), fetch("b", 0.3))
    elapsed = time.monotonic() - start
    print(f"[并发] {', '.join(results)} — 耗时 {elapsed:.2f}s(预期 ~0.3s)")


async def demo_queue_workers() -> None:
    """模式 3:Queue + Worker 池 —— 工具调度并发上限的雏形。

    多个任务放入队列,N 个 worker 并发消费。任务完成顺序不确定,
    但结果按入队顺序收集(对应"按模型调用顺序回灌")。
    """
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(5):
        await queue.put(i)

    async def worker(wid: int) -> tuple[int, str]:
        item = await queue.get()
        await asyncio.sleep(0.2)
        queue.task_done()
        return item, f"worker{wid} 处理了任务 {item}"

    # 2 个 worker 并发消费 5 个任务
    results = await asyncio.gather(*[worker(i) for i in range(2)])
    await queue.join()
    for _, msg in sorted(results):
        print(f"[Queue] {msg}")


async def main() -> None:
    print("=== asyncio 模式 demo ===\n")
    await demo_await_serial()
    await demo_gather_concurrent()
    await demo_queue_workers()


if __name__ == "__main__":
    asyncio.run(main())
