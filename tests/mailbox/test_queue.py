"""M6 单元测试:MailboxQueue 状态机(claim/ack/nack + lease 重投)。

纯内存态 + 注入时钟(now_fn 固定值,确定性)与 store(可选)。不涉及真实并发。
状态机:pending → claimed → done / (重投) pending → dead。
"""

from __future__ import annotations

from mailbox.models import TaskStatus
from mailbox.queue import MailboxQueue

import pytest

from tests.mailbox._helpers import claim_or_fail


@pytest.fixture()
def queue() -> MailboxQueue:
    """无持久化内存队列,固定时钟(created_at=1000,lease_until=1030)。"""

    def now() -> int:
        return 1000

    return MailboxQueue(now_fn=now, lease_seconds=30)



class TestSubmit:
    async def test_submit_sets_pending(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {"cmd": "read_file"})
        rec = queue.state()["t-1"]
        assert rec["status"] == "pending"
        assert rec["attempts"] == 0
        assert rec["created_at"] == 1000
        assert rec["claimed_by"] is None
        assert rec["payload"]["cmd"] == "read_file"

    async def test_submit_duplicate_task_id_returns_existing(
        self, queue: MailboxQueue
    ) -> None:
        """幂等键:重复 submit 同 task_id → 返回既有记录,不重复入队。"""
        first = await queue.submit("t-1", {"cmd": "a"})
        second = await queue.submit("t-1", {"cmd": "b"})
        assert second.task_id == first.task_id
        assert len(queue.state()) == 1  # 未重复入队


class TestClaim:
    async def test_claim_fifo_by_created_at(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        await queue.submit("t-2", {})
        tid, rec = await claim_or_fail(queue, "w-1")
        assert tid == "t-1"  # 先提交的先领
        assert rec.status == "claimed"

    async def test_claim_marks_claimed_with_lease(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        tid, rec = await claim_or_fail(queue, "w-1")
        assert tid == "t-1"
        assert rec.status == "claimed"
        assert rec.claimed_by == "w-1"
        assert rec.attempts == 1  # 领取即消费一次投递
        assert rec.lease_until == 1000 + 30  # now + lease_seconds

    async def test_claim_empty_returns_none(self, queue: MailboxQueue) -> None:
        assert await queue.claim("w-1") is None

    async def test_claim_skips_done_and_dead(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        await queue.submit("t-2", {})
        await queue.submit("t-3", {})
        # t-1 → done
        tid, _ = await claim_or_fail(queue, "w-1")
        await queue.ack("w-1", tid)
        # t-2 → dead:claim+nack 到 max_deliveries
        for _ in range(3):
            tid, _ = await claim_or_fail(queue, "w-1")
            await queue.nack("w-1", tid, error="x")
        assert queue.state()["t-2"]["status"] == "dead"
        # done/dead 不被领,只领到 t-3
        tid, _ = await claim_or_fail(queue, "w-1")
        assert tid == "t-3"

    async def test_claim_after_done_fifo_keeps_order(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        await queue.submit("t-2", {})
        tid, _ = await claim_or_fail(queue, "w-1")
        await queue.ack("w-1", tid)
        tid, _ = await claim_or_fail(queue, "w-1")
        assert tid == "t-2"


class TestAck:
    async def test_ack_marks_done(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        assert await queue.ack("w-1", "t-1") is True
        assert queue.state()["t-1"]["status"] == "done"
        assert queue.state()["t-1"]["done_at"] is not None

    async def test_ack_wrong_worker_rejected(self, queue: MailboxQueue) -> None:
        """非持有 worker ack → False,状态不变(防误确认)。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        assert await queue.ack("w-2", "t-1") is False
        assert queue.state()["t-1"]["status"] == "claimed"

    async def test_ack_pending_rejected(self, queue: MailboxQueue) -> None:
        """未 claim 的任务 ack → False(必须先领)。"""
        await queue.submit("t-1", {})
        assert await queue.ack("w-1", "t-1") is False

    async def test_ack_unknown_task_returns_false(self, queue: MailboxQueue) -> None:
        assert await queue.ack("w-1", "ghost") is False


class TestNack:
    async def test_nack_requeues_to_pending(self, queue: MailboxQueue) -> None:
        """nack 未超限 → 回 pending,可再次 claim,attempts 保留。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        assert await queue.nack("w-1", "t-1", error="failed") is True
        assert queue.state()["t-1"]["status"] == "pending"
        assert queue.state()["t-1"]["claimed_by"] is None
        # 可再次领取(重投)
        tid, rec = await claim_or_fail(queue, "w-1")
        assert tid == "t-1"
        assert rec.attempts == 2

    async def test_nack_to_dead_at_max_deliveries(self, queue: MailboxQueue) -> None:
        """nack 达 max_deliveries → dead(毒消息不无限重投)。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        await queue.nack("w-1", "t-1", error="e1")  # attempts 1 → 回 pending
        await claim_or_fail(queue, "w-1")
        await queue.nack("w-1", "t-1", error="e2")  # attempts 2 → 回 pending
        await claim_or_fail(queue, "w-1")
        await queue.nack("w-1", "t-1", error="e3")  # attempts 3 = max → dead
        assert queue.state()["t-1"]["status"] == "dead"
        assert queue.state()["t-1"]["last_error"] == "e3"

    async def test_nack_wrong_worker_rejected(self, queue: MailboxQueue) -> None:
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        assert await queue.nack("w-2", "t-1", error="x") is False
        assert queue.state()["t-1"]["status"] == "claimed"


class TestRecoverDeliveries:
    async def test_expired_lease_requeued(self, queue: MailboxQueue) -> None:
        """lease 过期未 ack(worker 崩溃)→ recover → 回 pending 重投。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")  # lease_until = 1030
        n = await queue.recover_deliveries(now=2000)  # 远超租约
        assert n == 1
        assert queue.state()["t-1"]["status"] == "pending"

    async def test_unexpired_lease_untouched(self, queue: MailboxQueue) -> None:
        """lease 未过期(worker 仍在处理)→ 不重投。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")  # lease_until = 1030
        n = await queue.recover_deliveries(now=1020)
        assert n == 0
        assert queue.state()["t-1"]["status"] == "claimed"

    async def test_recover_then_claimable_again(self, queue: MailboxQueue) -> None:
        """重投后可再次 claim(at-least-once:允许重复投递)。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        await queue.recover_deliveries(now=2000)
        tid, rec = await claim_or_fail(queue, "w-2")  # 另一个 worker 接手
        assert tid == "t-1"
        assert rec.attempts == 2

    async def test_recover_redelivery_to_dead_at_max_deliveries(self, queue: MailboxQueue) -> None:
        """lease 崩溃重投达 max_deliveries → dead(防无限重投崩溃型毒消息)。

        修复前:_recover_deliveries 不检查 attempts,worker 反复崩溃(claim 后
        从不 nack,全靠租约超时重投)→ 无限重投。修复后两条重投路径(nack /
        lease 重投)共用同一 attempts 判定。
        """
        await queue.submit("t-1", {})
        # 每轮:claim(attempts+1)→ 租约超时(now=2000 > lease_until=1030)→ recover
        for round_no in range(1, 4):
            await claim_or_fail(queue, "w-1")
            n = await queue.recover_deliveries(now=2000)
            if round_no < 3:
                assert n == 1  # 未超限 → 重投
                assert queue.state()["t-1"]["status"] == "pending"
            else:
                assert n == 0  # 第 3 次投递已超限 → dead,不再重投
                assert queue.state()["t-1"]["status"] == "dead"

    async def test_recover_uses_same_max_deliveries_as_nack(self, queue: MailboxQueue) -> None:
        """nack 与 lease 重投共用同一 attempts 计数(单一来源)。"""
        await queue.submit("t-1", {})
        # nack 两次(attempts 1 → 2),均未超限回 pending
        await claim_or_fail(queue, "w-1")
        await queue.nack("w-1", "t-1", error="e1")
        await claim_or_fail(queue, "w-1")
        await queue.nack("w-1", "t-1", error="e2")
        assert queue.state()["t-1"]["status"] == "pending"
        # 第三次投递走 lease 超时路径(attempts 3 = max)→ dead
        await claim_or_fail(queue, "w-1")
        await queue.recover_deliveries(now=2000)
        assert queue.state()["t-1"]["status"] == "dead"


class TestEncapsulation:
    async def test_claim_returns_copy_not_internal(self, queue: MailboxQueue) -> None:
        """claim 返回深拷贝:调用方改写返回的记录不影响内部状态。"""
        await queue.submit("t-1", {})
        _, rec = await claim_or_fail(queue, "w-1")
        rec.status = TaskStatus.DONE  # 篡改返回的快照
        assert queue.state()["t-1"]["status"] == "claimed"  # 内部态不变

    async def test_submit_duplicate_returns_copy(self, queue: MailboxQueue) -> None:
        """重复 submit 返回既有记录的拷贝:改写不影响已入队记录。"""
        await queue.submit("t-1", {"cmd": "a"})
        rec = await queue.submit("t-1", {"cmd": "b"})
        rec.payload["cmd"] = "hacked"
        assert queue.state()["t-1"]["payload"]["cmd"] == "a"  # 原记录未被改


class TestReporting:
    async def test_state_exposes_all_records(self, queue: MailboxQueue) -> None:
        """state() 返回所有任务现状(供外部读/审计)。"""
        await queue.submit("t-1", {})
        await claim_or_fail(queue, "w-1")
        state = queue.state()
        assert len(state) == 1
        assert state["t-1"]["status"] == "claimed"
