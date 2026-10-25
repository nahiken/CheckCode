"""M6 集成测试:并发 claim 竞态 + ack/nack 并发一致性。

用 asyncio.gather 模拟多个 worker 同时 claim:必须保证一条任务
同一时刻至多被一个 worker 持有(claim 原子性),且最终无任务丢失。
"""

from __future__ import annotations

import asyncio
import time
from mailbox.queue import MailboxQueue

import pytest

WORKERS = ["w-1", "w-2", "w-3", "w-4"]


@pytest.fixture()
def queue() -> MailboxQueue:
    """真实 clock + 无持久化。"""
    return MailboxQueue(now_fn=lambda: int(time.time()))


async def _claim_all_concurrently(
    queue: MailboxQueue, worker_ids: list[str]
) -> list[tuple[str, str]]:
    """所有 worker 同时争抢 claim,直到拿不到为止。返回 [(worker, task_id)]。"""

    async def worker(worker_id: str) -> list[tuple[str, str]]:
        got: list[tuple[str, str]] = []
        while True:
            claimed = await queue.claim(worker_id)
            if claimed is None:
                break
            got.append((worker_id, claimed[0]))
        return got

    results = await asyncio.gather(*[worker(w) for w in worker_ids])
    return [item for sub in results for item in sub]


class TestConcurrentClaim:
    async def test_each_task_claimed_exactly_once(self, queue: MailboxQueue) -> None:
        """N 个 worker 并发 claim N 条任务:每任务恰好一个 worker,无重复。"""
        for i in range(6):
            await queue.submit(f"t-{i}", {"n": i})
        claimed = await _claim_all_concurrently(queue, WORKERS)
        task_ids = [tid for _, tid in claimed]
        assert len(task_ids) == 6
        assert len(set(task_ids)) == 6  # 无重复领取
        workers = [w for w, _ in claimed]
        assert set(workers) <= set(WORKERS)

    async def test_claim_beyond_count_returns_none(self, queue: MailboxQueue) -> None:
        """并发 claim 超任务数:多余 claim 拿不到(返回 None,不阻塞)。"""
        await queue.submit("t-1", {})
        await queue.submit("t-2", {})
        claimed = await _claim_all_concurrently(queue, WORKERS)
        assert len(claimed) == 2  # 只有 2 条任务,4 个 worker 也只领到 2 次

    async def test_reclaim_after_claim_blocks(self, queue: MailboxQueue) -> None:
        """同一任务被 claim 后,并发再 claim 拿不到(原子性)。"""
        await queue.submit("t-1", {})
        claimed = await queue.claim("w-1")
        assert claimed is not None
        tid, _ = claimed
        assert tid == "t-1"
        assert await queue.claim("w-2") is None  # t-1 已被持有

    async def test_concurrent_ack_nack_consistency(self, queue: MailboxQueue) -> None:
        """并发 ack/nack 混合:最终状态一致,任务总数不丢失。"""
        for i in range(8):
            await queue.submit(f"t-{i}", {})

        async def process(worker_id: str) -> None:
            while True:
                claimed = await queue.claim(worker_id)
                if claimed is None:
                    return
                tid, _ = claimed
                if int(tid.split("-")[1]) % 2 == 0:
                    await queue.ack(worker_id, tid)
                else:
                    await queue.nack(worker_id, tid, error="odd")

        await asyncio.gather(*[process(w) for w in WORKERS])
        state = queue.state()
        statuses = [rec["status"] for rec in state.values()]
        assert all(s in ("done", "dead") for s in statuses)  # 全部终结,无 pending/claimed 残留
        assert len(state) == 8  # 无任务丢失

    async def test_two_workers_consume_all_without_race(self, queue: MailboxQueue) -> None:
        """两个 worker 独立消费全部任务并 ack:共享状态无竞态。"""
        for i in range(10):
            await queue.submit(f"t-{i}", {})

        async def process(worker_id: str) -> set[str]:
            done: set[str] = set()
            while True:
                claimed = await queue.claim(worker_id)
                if claimed is None:
                    return done
                tid, _ = claimed
                assert tid not in done  # 同一 worker 不会重复拿同一条
                done.add(tid)
                await queue.ack(worker_id, tid)
            return done

        results = await asyncio.gather(process("w-1"), process("w-2"))
        all_done = results[0] | results[1]
        assert len(all_done) == 10  # 全部任务完成
        assert len(results[0] & results[1]) == 0  # 无重叠(无竞态重复处理)
