"""M14 单元测试:Agent 间寻址消息(send_message / claim_inbox / reply)。

核心语义:
- **车道隔离**:任务只被 `claim` 领,消息只被 `claim_inbox` 领 —— 忙的 worker
  不会把别人的消息吃掉,agent 也不会误领一条要跑很久的任务。
- **寻址**:`recipient` 定向往返;`None` = 广播(任一 agent 可领,只领一次)。
- **不回执就卡死**:定向消息若收件人崩了,租约重投会把它**放回广播池**
  (否则它永远卡在 claimed,谁也领不到)。
- 消息复用任务同一套 lease/max_deliveries 状态机,不 fork 并行状态机。
"""

from __future__ import annotations

from mailbox.models import TaskKind, TaskStatus
from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore
from pathlib import Path

import pytest


class _Clock:
    """可控时钟:同 created_at 会让 FIFO 退化成按 id 排序,故测试里显式推进。"""

    def __init__(self, t: int = 1000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, n: int = 1) -> None:
        self.t += n


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


@pytest.fixture()
def queue(clock: _Clock) -> MailboxQueue:
    return MailboxQueue(now_fn=clock, lease_seconds=30)


class TestSendAndClaimInbox:
    async def test_directed_message_only_visible_to_recipient(
        self, queue: MailboxQueue
    ) -> None:
        """定向消息:只有收件人领得到,别的 agent 看不见(不是「先到先得」)。"""
        await queue.send_message("m-1", {"text": "帮我改一下"}, sender="a", recipient="b")

        assert await queue.claim_inbox("c") is None  # 不是给我的
        claimed = await queue.claim_inbox("b")
        assert claimed is not None
        message_id, rec = claimed
        assert message_id == "m-1"
        assert rec.sender == "a" and rec.recipient == "b"
        assert rec.kind == TaskKind.MESSAGE
        assert rec.payload["text"] == "帮我改一下"

    async def test_broadcast_claimable_by_anyone_once(self, queue: MailboxQueue) -> None:
        """广播(recipient=None):任一 agent 可领,且只领一次。"""
        await queue.send_message("m-1", {"text": "通知"}, sender="a")

        first = await queue.claim_inbox("b")
        assert first is not None and first[0] == "m-1"
        assert await queue.claim_inbox("c") is None  # 已被 b 领走

    async def test_fifo_order(self, queue: MailboxQueue, clock: _Clock) -> None:
        """按 created_at FIFO:先发的先领。"""
        await queue.send_message("m-1", {"text": "第一"}, sender="a", recipient="b")
        clock.advance()
        await queue.send_message("m-2", {"text": "第二"}, sender="a", recipient="b")

        first = await queue.claim_inbox("b")
        second = await queue.claim_inbox("b")
        assert first is not None and first[0] == "m-1"
        assert second is not None and second[0] == "m-2"

    async def test_send_message_is_idempotent(self, queue: MailboxQueue) -> None:
        """幂等键同 submit:同 message_id 重复投递 → 返回既有记录,不重复入队。"""
        await queue.send_message("m-1", {"text": "原始"}, sender="a", recipient="b")
        again = await queue.send_message("m-1", {"text": "改过"}, sender="x", recipient="y")
        assert again.payload["text"] == "原始"
        assert again.sender == "a"
        assert len(queue.state()) == 1

    async def test_corr_id_defaults_to_message_id(self, queue: MailboxQueue) -> None:
        rec = await queue.send_message("m-1", {"text": "x"}, sender="a", recipient="b")
        assert rec.corr_id == "m-1"

    async def test_corr_id_explicit_is_kept(self, queue: MailboxQueue) -> None:
        """显式 corr_id 优先(跨越多轮对话挂在同一个关联 id 上)。"""
        rec = await queue.send_message(
            "m-1", {"text": "x"}, sender="a", recipient="b", corr_id="conv-7"
        )
        assert rec.corr_id == "conv-7"


class TestLaneSeparation:
    async def test_claim_does_not_take_messages(self, queue: MailboxQueue) -> None:
        """worker 的 claim 只领任务:消息不会被任务车道吃掉。"""
        await queue.send_message("m-1", {"text": "消息"}, sender="a", recipient="b")
        assert await queue.claim("w-1") is None

    async def test_claim_inbox_does_not_take_tasks(self, queue: MailboxQueue) -> None:
        """agent 的 claim_inbox 只领消息:不会误领一条要跑很久的任务。"""
        await queue.submit("t-1", {"task": "很重的任务"})
        assert await queue.claim_inbox("agent-1") is None

    async def test_both_lanes_work_independently(self, queue: MailboxQueue) -> None:
        """两条车道并存:各自抽干,互不干扰。"""
        await queue.submit("t-1", {"task": "干活"})
        await queue.send_message("m-1", {"text": "消息"}, sender="a", recipient="b")

        task = await queue.claim("w-1")
        msg = await queue.claim_inbox("b")
        assert task is not None and task[0] == "t-1"
        assert msg is not None and msg[0] == "m-1"


class TestReply:
    async def test_reply_goes_back_to_parent_sender(self, queue: MailboxQueue) -> None:
        """回执的收件人取自父消息的 sender,并共享 corr_id。"""
        await queue.send_message("m-1", {"text": "帮我看看"}, sender="alice", recipient="bob")
        reply = await queue.reply("m-1", "m-1:r", {"ok": True}, sender="bob")

        assert reply.kind == TaskKind.REPLY
        assert reply.recipient == "alice"  # 回到原发件人
        assert reply.sender == "bob"
        assert reply.corr_id == "m-1"

        claimed = await queue.claim_inbox("alice")
        assert claimed is not None and claimed[0] == "m-1:r"

    async def test_reply_to_unknown_message_raises(self, queue: MailboxQueue) -> None:
        """回复一条不存在的消息 → ValueError(不静默造无主回复)。"""
        with pytest.raises(ValueError, match="unknown message"):
            await queue.reply("ghost", "r-1", {"ok": True}, sender="bob")

    async def test_reply_to_senderless_record_raises(self, queue: MailboxQueue) -> None:
        """父记录没有 sender(任务没有) → ValueError,不造出收件人为 None 的假回执。"""
        await queue.submit("t-1", {"task": "x"})
        with pytest.raises(ValueError, match="no sender"):
            await queue.reply("t-1", "r-1", {"ok": True}, sender="bob")

    async def test_reply_is_idempotent(self, queue: MailboxQueue) -> None:
        """同一 reply_id 回执两次 → 第二次返回既有记录,**不覆写**。

        审计中-1:初版 `reply` 是本模块唯一不做 `existing` 检查的写方法,直接
        `self._tasks[reply_id] = rec` —— 与 `_submit`/`send_message` 的幂等纪律不同构。
        """
        await queue.send_message("m-1", {"text": "在吗"}, sender="alice", recipient="bob")
        first = await queue.reply("m-1", "m-1:reply", {"answer": "在"}, sender="bob")
        second = await queue.reply("m-1", "m-1:reply", {"answer": "改过"}, sender="bob")

        assert second.payload == {"answer": "在"}  # 首次的正文保留
        assert len(queue.state()) == 2  # 只有 m-1 与一条回执
        assert first.payload == second.payload

    async def test_reply_does_not_resurrect_done_reply(self, queue: MailboxQueue) -> None:
        """父消息被 lease 重投后再次回执 → **已 DONE 的回执不被复活**。

        审计中-1 的完整场景:回执已 ack(DONE)→ 父消息重投 → 收件人再次回执。
        初版会把 DONE 覆写成 PENDING、`attempts` 归零、`done_at` 清空 ——
        状态机倒流 + 发件人第二次收到同一回执。
        """
        await queue.send_message("m-1", {"text": "在吗"}, sender="alice", recipient="bob")
        await queue.claim_inbox("bob")
        await queue.reply("m-1", "m-1:reply", {"answer": "在"}, sender="bob")
        # alice 领走回执并 ack(终态)
        await queue.claim_inbox("alice")
        assert await queue.ack("alice", "m-1:reply") is True
        done = queue.state()["m-1:reply"]
        assert done["status"] == TaskStatus.DONE.value
        assert done["attempts"] == 1

        # bob 在 reply 与 ack 之间「崩溃」→ 父消息租约过期重投 → 再次回执
        assert await queue.recover_deliveries(now=2000) == 1
        await queue.claim_inbox("bob")
        await queue.reply("m-1", "m-1:reply", {"answer": "重复"}, sender="bob")

        after = queue.state()["m-1:reply"]
        assert after["status"] == TaskStatus.DONE.value  # 不复活
        assert after["attempts"] == 1  # 不被归零
        assert after["done_at"] == done["done_at"]  # 审计字段保留
        assert after["payload"] == {"answer": "在"}


class TestKindClash:
    """两条车道共用一个 id 命名空间 → 跨车道同 id 必须**报错**,不能静默 no-op。"""

    async def test_submit_on_message_id_raises(self, queue: MailboxQueue) -> None:
        """id 已是消息时 submit 同 id → ValueError(否则任务永远领不到)。"""
        await queue.send_message("X", {"text": "msg"}, sender="a", recipient="b")
        with pytest.raises(ValueError, match="另一条车道"):
            await queue.submit("X", {"task": "do work"})

    async def test_send_message_on_task_id_raises(self, queue: MailboxQueue) -> None:
        """id 已是任务时 send_message 同 id → ValueError。"""
        await queue.submit("Y", {"task": "do work"})
        with pytest.raises(ValueError, match="另一条车道"):
            await queue.send_message("Y", {"text": "msg"}, sender="a", recipient="b")

    async def test_same_kind_repeat_still_idempotent(self, queue: MailboxQueue) -> None:
        """同车道重复投递仍是幂等(报错只针对**跨**车道)。"""
        await queue.send_message("X", {"text": "1"}, sender="a", recipient="b")
        again = await queue.send_message("X", {"text": "2"}, sender="a", recipient="b")
        assert again.payload == {"text": "1"}


class TestMessageRecovery:
    async def test_directed_message_released_to_pool_on_lease_expiry(
        self, queue: MailboxQueue
    ) -> None:
        """定向消息的收件人崩溃(租约过期)→ 重投时**失去定向**,回到广播池。

        若重投仍保持定向,这条消息就永远卡在 claimed:原收件人已崩,其他 agent
        又「看不见」它。这正是「找不到收件人时不静默丢消息」的兜底。
        """
        await queue.send_message("m-1", {"text": "救命"}, sender="a", recipient="b")
        await queue.claim_inbox("b")  # b 领走,随后崩溃(不 ack)

        assert await queue.recover_deliveries(now=2000) == 1
        rec = queue.state()["m-1"]
        assert rec["status"] == TaskStatus.PENDING.value
        assert rec["recipient"] is None  # 失去定向 → 广播

        # 另一个 agent 接手(而非永远卡死)
        claimed = await queue.claim_inbox("c")
        assert claimed is not None and claimed[0] == "m-1"
        assert claimed[1].attempts == 2

    async def test_broadcast_stays_broadcast_on_requeue(self, queue: MailboxQueue) -> None:
        """广播重投后仍是广播(不因重投被意外指派给某人)。"""
        await queue.send_message("m-1", {"text": "通知"}, sender="a")
        await queue.claim_inbox("b")
        await queue.recover_deliveries(now=2000)
        assert queue.state()["m-1"]["recipient"] is None

    async def test_message_becomes_dead_at_max_deliveries(
        self, queue: MailboxQueue
    ) -> None:
        """毒消息:反复失败达 max_deliveries → dead,不无限重投。"""
        await queue.send_message("m-1", {"text": "毒"}, sender="a", recipient="b")
        for _ in range(3):
            claimed = await queue.claim_inbox("b")
            assert claimed is not None
            await queue.nack("b", "m-1", error="处理失败")
        assert queue.state()["m-1"]["status"] == TaskStatus.DEAD.value


class TestMessagePersistence:
    async def test_message_survives_rebuild(self, tmp_path: Path) -> None:
        """消息经快照持久化:进程重启后仍在,且 kind/sender 等字段完整。"""
        path = tmp_path / "mailbox.json"
        q = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        await q.send_message("m-1", {"text": "持久"}, sender="a", recipient="b")
        q.close()

        rebuilt = MailboxQueue(now_fn=lambda: 1000, store=MailboxStore(path))
        try:
            rec = rebuilt.state()["m-1"]
            assert rec["kind"] == TaskKind.MESSAGE.value
            assert rec["sender"] == "a" and rec["recipient"] == "b"
            assert rec["corr_id"] == "m-1"
        finally:
            rebuilt.close()
