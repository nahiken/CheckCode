"""M14 单元测试:MailboxWorker 的消息车道(process_inbox)。

worker 一旦同时服务两条车道,就必须证明:
- 消息被**回执**(reply 回到发件人,corr_id 贯穿),而不是默默吃掉;
- 不需要回执时(deliver 返回 None)也 ack,不无限重投;
- deliver 抛异常 → nack 回队(与 process_once 同语义),不悬挂;
- 忙任务车道**不影响**收件箱:process_once 拿不到的消息留给 process_inbox。
"""

from __future__ import annotations

from mailbox.models import TaskKind, TaskStatus
from mailbox.queue import MailboxQueue
from pathlib import Path

from assembly.worker import MailboxWorker


def _memory_queue() -> MailboxQueue:
    return MailboxQueue(now_fn=lambda: 1000)


async def _noop_handler(payload: dict) -> str | None:
    return None


class TestProcessInbox:
    async def test_message_delivered_and_acked(self) -> None:
        """deliver 返回 None(只收不回)→ 消息 ack 转 done,不回执。"""
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "你好"}, sender="alice", recipient="bob")
        seen: list[str] = []

        async def deliver(record) -> dict | None:
            seen.append(record.payload["text"])
            return None

        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")
        result = await worker.process_inbox(deliver)

        assert result.claimed and result.acked and not result.nacked
        assert result.message_id == "m-1"
        assert result.replied is False
        assert seen == ["你好"]
        assert queue.state()["m-1"]["status"] == TaskStatus.DONE.value

    async def test_deliver_return_value_becomes_reply(self) -> None:
        """deliver 返回 dict → 回执给原发件人,corr_id 与父消息一致。"""
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "帮我看看"}, sender="alice", recipient="bob")

        async def deliver(record) -> dict | None:
            return {"ok": True, "answer": "看过了"}

        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")
        result = await worker.process_inbox(deliver)

        assert result.acked and result.replied
        replies = [r for r in queue.state().values() if r["kind"] == TaskKind.REPLY.value]
        assert len(replies) == 1
        assert replies[0]["recipient"] == "alice"  # 回到发件人
        assert replies[0]["sender"] == "bob"
        assert replies[0]["corr_id"] == "m-1"  # 关联 id 贯穿
        assert replies[0]["payload"] == {"ok": True, "answer": "看过了"}

        # 发件人真能领到这条回执
        claimed = await queue.claim_inbox("alice")
        assert claimed is not None and claimed[0] == "m-1:reply"

    async def test_no_deliver_just_acks(self) -> None:
        """不传 deliver → 单纯收下并 ack(不产生回执)。"""
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "通知"}, sender="alice", recipient="bob")
        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")

        result = await worker.process_inbox()
        assert result.acked and not result.replied
        assert not [r for r in queue.state().values() if r["kind"] == TaskKind.REPLY.value]

    async def test_idle_when_inbox_empty(self) -> None:
        """收件箱空 → claimed=False(不报错)。"""
        worker = MailboxWorker(_memory_queue(), _noop_handler, worker_id="bob")
        result = await worker.process_inbox()
        assert not result.claimed

    async def test_deliver_exception_becomes_nack(self) -> None:
        """deliver 抛异常 → nack 回队重投(不悬挂),错误被记录。"""
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "x"}, sender="alice", recipient="bob")

        async def deliver(record) -> dict | None:
            raise RuntimeError("deliver crash")

        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")
        result = await worker.process_inbox(deliver)

        assert result.nacked and "deliver crash" in result.error
        rec = queue.state()["m-1"]
        assert rec["status"] == TaskStatus.PENDING.value  # 回队可重投
        assert rec["last_error"] == "消息处理异常: deliver crash"

    async def test_reply_lands_in_snapshot(self, tmp_path: Path) -> None:
        """回执经快照持久化:重开 store 仍能看到(跨进程场景的必要条件)。"""
        from mailbox.store import MailboxStore

        snap = tmp_path / "queue.json"
        queue = MailboxQueue(store=MailboxStore(snap), now_fn=lambda: 1000)
        await queue.send_message("m-1", {"text": "在吗"}, sender="alice", recipient="bob")

        async def deliver(record) -> dict | None:
            return {"answer": "在"}

        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")
        await worker.process_inbox(deliver)
        queue.close()

        loaded = MailboxStore(snap).load()
        assert loaded["m-1"]["status"] == TaskStatus.DONE.value
        assert loaded["m-1:reply"]["recipient"] == "alice"


    async def test_reply_failure_nacks_instead_of_escaping(self) -> None:
        """回执失败(父消息缺 sender)→ nack,异常**不穿出** process_inbox。

        审计存疑-4:原实现把 `queue.reply(...)` 放在 try 之外,若它抛错,
        消息会留在 claimed 且没有 nack 记录(不悬挂原则被破坏)。
        当前不可达(send_message 的 sender 是必填),这是防御性覆盖。
        """
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "x"}, sender="alice", recipient="bob")
        # 手工把 sender 抹掉,制造 reply 会抛 ValueError 的现场
        queue._tasks["m-1"].sender = None

        async def deliver(record) -> dict | None:
            return {"ok": True}

        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")
        result = await worker.process_inbox(deliver)  # 不抛异常

        assert result.nacked and "回执失败" in result.error
        assert queue.state()["m-1"]["status"] == TaskStatus.PENDING.value


class TestLaneInteraction:
    async def test_task_lane_does_not_consume_messages(self) -> None:
        """worker 先跑任务车道:消息原封不动留给收件箱(不被当作任务吃掉)。"""
        queue = _memory_queue()
        await queue.send_message("m-1", {"text": "消息"}, sender="alice", recipient="bob")
        worker = MailboxWorker(queue, _noop_handler, worker_id="bob")

        task_result = await worker.process_once()
        assert not task_result.claimed  # 消息不是任务

        inbox_result = await worker.process_inbox()
        assert inbox_result.claimed and inbox_result.message_id == "m-1"

    async def test_task_and_message_both_processed(self) -> None:
        """两条车道各取所需:任务被 handler 处理,消息被 deliver 处理。"""
        queue = _memory_queue()
        await queue.submit("t-1", {"task": "干活"})
        await queue.send_message("m-1", {"text": "留言"}, sender="alice", recipient="bob")

        handled: list[dict] = []
        delivered: list[str] = []

        async def handler(payload: dict) -> str | None:
            handled.append(payload)
            return None

        async def deliver(record) -> dict | None:
            delivered.append(record.payload["text"])
            return None

        worker = MailboxWorker(queue, handler, worker_id="bob")
        assert (await worker.process_once()).acked
        assert (await worker.process_inbox(deliver)).acked

        assert handled == [{"task": "干活"}]
        assert delivered == ["留言"]
