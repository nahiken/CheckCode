"""M6 集成测试:崩溃恢复 + 消息重投(原子快照 + lease)。

模拟 worker 崩溃:claim 后不 ack,进程「重启」(重建 queue),磁盘状态重建
→ recover_deliveries 把超租约任务回 pending → 另一个 worker 接手 → 全部最终 done。

核心语义:at-least-once——崩溃前可能已处理,重投允许重复,靠消费者幂等吸收。
"""

from __future__ import annotations

from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore
from pathlib import Path

from tests.mailbox._helpers import claim_or_fail


def _rebuild(path: Path, *, now: int) -> MailboxQueue:
    """模拟进程重启:从磁盘重建队列。"""
    store = MailboxStore(path)
    return MailboxQueue(now_fn=lambda: now, store=store)



class TestPersistenceRecovery:
    async def test_rebuild_keeps_pending_tasks(self, tmp_path: Path) -> None:
        """提交一批 → save → 重建:任务还在(pending)。"""
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        await q.submit("t-1", {"cmd": "a"})
        await q.submit("t-2", {"cmd": "b"})

        rebuilt = _rebuild(path, now=1000)
        state = rebuilt.state()
        assert set(state) == {"t-1", "t-2"}
        assert all(rec["status"] == "pending" for rec in state.values())

    async def test_crash_after_claim_rethrows_after_recover(self, tmp_path: Path) -> None:
        """claim 后崩溃(不 ack)→ 重建 + lease 过期 recover → 可再次 claim。

        at-least-once:允许重复投递——这正是崩溃恢复的可靠性保证。
        """
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        await q.submit("t-1", {})
        await claim_or_fail(q, "w-1")  # lease_until = 1030,未 ack 即「崩溃」

        # 重启后时间已过租约
        rebuilt = _rebuild(path, now=2000)
        await rebuilt.recover_deliveries(now=2000)
        assert rebuilt.state()["t-1"]["status"] == "pending"

        tid, rec = await claim_or_fail(rebuilt, "w-2")  # 另一 worker 接手
        assert tid == "t-1"
        assert rec.attempts == 2  # 第二次投递

    async def test_acked_task_not_redelivered(self, tmp_path: Path) -> None:
        """ack 后重建:done 保持,不重投。"""
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        await q.submit("t-1", {})
        await claim_or_fail(q, "w-1")
        await q.ack("w-1", "t-1")

        rebuilt = _rebuild(path, now=5000)  # 很久之后重启
        await rebuilt.recover_deliveries(now=5000)
        assert rebuilt.state()["t-1"]["status"] == "done"
        assert await rebuilt.claim("w-2") is None  # done 不再投递

    async def test_rebuild_claimed_within_lease_not_rethrown(self, tmp_path: Path) -> None:
        """重建时 claimed 未过期:不立即重投;等 lease 过期后再 recover → 重投。"""
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        await q.submit("t-1", {})
        await claim_or_fail(q, "w-1")  # lease_until = 1030

        rebuilt = _rebuild(path, now=1010)  # 租约内重启
        await rebuilt.recover_deliveries(now=1010)
        assert rebuilt.state()["t-1"]["status"] == "claimed"  # 不重投

        await rebuilt.recover_deliveries(now=2000)  # 租约到期
        assert rebuilt.state()["t-1"]["status"] == "pending"

    async def test_interrupted_worker_handover_completes_all(self, tmp_path: Path) -> None:
        """端到端:worker A 崩溃,B 接手 → 全部任务最终 done(中断恢复,不丢任务)。"""
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        for i in range(4):
            await q.submit(f"t-{i}", {"n": i})

        # Worker A:领了 t-0 与 t-1,处理完 t-0(ack)后崩溃(t-1 未 ack)
        await claim_or_fail(q, "w-a")
        await q.ack("w-a", "t-0")
        await claim_or_fail(q, "w-a")  # 领走 t-1,未 ack → 崩溃

        # 很久之后重启,Worker B 接手
        rebuilt = _rebuild(path, now=5000)
        await rebuilt.recover_deliveries(now=5000)  # t-1 重投回 pending

        while True:
            claimed = await rebuilt.claim("w-b")
            if claimed is None:
                break
            tid, _ = claimed
            await rebuilt.ack("w-b", tid)

        state = rebuilt.state()
        assert all(rec["status"] == "done" for rec in state.values())  # 无丢失,全部完成
