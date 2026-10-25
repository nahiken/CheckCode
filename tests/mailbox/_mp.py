"""M14 测试共享:跨进程子入口(spawn 只能 pickle **模块级**函数,故单独成模块)。

`multiprocessing.get_context("spawn")` 下,子进程按「模块名 + 函数名」重新 import
目标函数,因此这些入口不能是闭包/局部函数,也不能依赖父进程的 monkeypatch。
每个入口自己建队列(自带 store + 真实时钟),把结果经 `results` 队列回传。
"""

from __future__ import annotations

import asyncio
import time
from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore
from pathlib import Path
from typing import Any


def build_queue(path: str) -> MailboxQueue:
    """子进程内的队列构造:真实时钟 + 持久化(跨进程共享的就是这个快照文件)。"""
    return MailboxQueue(
        store=MailboxStore(Path(path)), now_fn=lambda: int(time.time())
    )


def claim_tasks(
    path: str,
    worker: str,
    start: Any,
    results: Any,
    limit: int | None = None,
    delay: float = 0.0,
) -> None:
    """子进程入口:等放行信号 → claim/ack → 回传本进程领取过的 task_id。

    `limit=None` = 一直领到队列空;`limit=N` = **恰好领 N 条**(不足则提前结束)。
    后者用于让「N 个进程都分到活」变成确定性事实,而不是靠调度运气。
    `delay` 给每次 claim 之间加一点停顿,降低单进程一路领到底的概率。
    """
    seen: list[str] = []

    async def _run() -> None:
        queue = build_queue(path)
        try:
            if not start.wait(timeout=60):
                results.put((worker, ["START-TIMEOUT"]))
                return
            while limit is None or len(seen) < limit:
                claimed = await queue.claim(worker)
                if claimed is None:
                    break
                task_id, _ = claimed
                seen.append(task_id)
                await queue.ack(worker, task_id)
                if delay:
                    await asyncio.sleep(delay)
            results.put((worker, seen))
        finally:
            queue.close()

    asyncio.run(_run())


def submit_batch(
    path: str, prefix: str, count: int, start: Any, results: Any
) -> None:
    """子进程入口:等放行信号 → 连续 submit `count` 条**各自唯一**的 id。

    这是「丢失更新」的探针:每个进程写的是自己那一批,但当前实现每次变更都会
    把**整表**序列化写回 —— 若不在锁内重新载入,后写者会把先写者的整批冲掉。
    """

    async def _run() -> None:
        queue = build_queue(path)
        try:
            if not start.wait(timeout=60):
                results.put((prefix, "START-TIMEOUT"))
                return
            for i in range(count):
                await queue.submit(f"{prefix}-{i}", {"n": i})
            results.put((prefix, "ok"))
        finally:
            queue.close()

    asyncio.run(_run())


def unlocked_reader(
    path: str, count: int, start: Any, results: Any, delay: float = 0.0
) -> None:
    """子进程入口:**不加锁**反复读快照(模拟 `mewcode inbox` 这类只读路径)。

    用于证明「写者的 os.replace 不会把并发读打挂」——Windows 上这对操作互斥,
    两侧都得有重试兜底。回传失败次数(0 = 健康)。
    """
    from mailbox.store import MailboxStore as Store

    errs: list[str] = []
    if not start.wait(timeout=60):
        results.put((path, ["START-TIMEOUT"]))
        return
    store = Store(Path(path))
    for _ in range(count):
        try:
            store.load()
        except Exception as exc:  # noqa: BLE001 - 统计失败类型即可
            errs.append(type(exc).__name__)
        if delay:
            time.sleep(delay)
    results.put((path, errs))


def locked_writer(
    path: str, count: int, prefix: str, start: Any, results: Any
) -> None:
    """子进程入口:反复 submit(每次变更都在跨进程锁内 replace 快照)。"""
    errs: list[str] = []

    async def _run() -> None:
        queue = build_queue(path)
        try:
            if not start.wait(timeout=60):
                results.put((prefix, ["START-TIMEOUT"]))
                return
            for i in range(count):
                try:
                    await queue.submit(f"{prefix}-{i}", {"n": i})
                except Exception as exc:  # noqa: BLE001
                    errs.append(type(exc).__name__)
            results.put((prefix, errs))
        finally:
            queue.close()

    asyncio.run(_run())


def claim_no_ack_loop(
    path: str, worker: str, rounds: int, start: Any, results: Any, pause: float = 0.05
) -> None:
    """子进程入口:**只 claim 不 ack**(模拟领取后立刻崩溃的 worker),反复 `rounds` 轮。

    每轮之间暂停一下,给父进程留出 `recover_deliveries` 的窗口 —— 于是任务被
    反复投递。用来在多进程下验证 `attempts` 上界(毒消息不会无限重投)。
    回传 (attempts 最大值, 终态计数)。
    """
    peak = 0

    async def _run() -> None:
        nonlocal peak
        queue = build_queue(path)
        try:
            if not start.wait(timeout=60):
                results.put((worker, "START-TIMEOUT"))
                return
            for _ in range(rounds):
                # 不止一层嵌套:一轮内把所有可领的都领完(都**不 ack**)。
                while True:
                    claimed = await queue.claim(worker)
                    if claimed is None:
                        break
                    peak = max(peak, claimed[1].attempts)
                await asyncio.sleep(pause)  # 等租约过期 + 父进程 recover
            results.put((worker, peak))
        finally:
            queue.close()

    asyncio.run(_run())


def hold_lock(
    path: str, seconds: float, started: Any, results: Any, timeout: float = 30.0
) -> None:
    """子进程入口:取锁 → 通知父进程 → 持锁 `seconds` 秒 → 释放。"""
    from mailbox.store import MailboxStore as Store

    store = Store(Path(path))
    try:
        with store.lock():
            started.set()
            time.sleep(seconds)
        # 释放后**再取一次**:证明锁可重入获取(不是「一次拿走后永久失效」)
        with store.lock():
            results.put("relocked")
    finally:
        store.close()
