"""M15 审计修复的回归测试(第二轮 code review 的 10 项问题)。

每个测试对应 `code-review` 报告里的一条 finding;命名带编号便于对应。
"""

from __future__ import annotations

from mailbox.models import TaskRecord, TaskStatus
from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore
from pathlib import Path
from typing import Any

from assembly.worker import MailboxWorker

# === 审计 C3:自己给自己的消息不能形成无限回执循环 ===


async def test_self_addressed_message_does_not_loop_forever(tmp_path: Path) -> None:
    """worker 认领到「自己发的」广播时**不得回执** —— 否则回执又变成发给自己、可认领的
    消息 → 认领→回执→再认领无限循环、快照无界增长(实测 6 轮永不终止)。
    """
    queue = MailboxQueue(store=MailboxStore(tmp_path / "mb.json"))
    await queue.send_message("m-1", {"text": "hi"}, sender="agent-a")

    async def deliver(record: TaskRecord) -> dict[str, Any] | None:
        return {"ok": True}  # 恒要求回执

    worker = MailboxWorker(queue, handler=_noop, worker_id="agent-a")
    results = [await worker.process_inbox(deliver) for _ in range(6)]

    assert results[0].claimed and results[0].message_id == "m-1"
    assert not results[0].replied  # 自己发的 → 不回执
    # 之后收件箱为空:没有生成任何 :reply(修好前这里会一路 m-1:reply:reply…)
    assert all(not r.claimed for r in results[1:])
    # 快照里只有那一条记录,且已 ack
    assert len(queue._tasks) == 1  # noqa: SLF001 - 测试探针
    assert queue._tasks["m-1"].status == TaskStatus.DONE  # noqa: SLF001


async def test_reply_to_other_sender_still_works(tmp_path: Path) -> None:
    """对照:发件人不是我 → 照常回执(修复不能把正常回执也堵掉)。"""
    queue = MailboxQueue(store=MailboxStore(tmp_path / "mb.json"))
    await queue.send_message("m-1", {"text": "hi"}, sender="alice")

    async def deliver(record: TaskRecord) -> dict[str, Any] | None:
        return {"ok": True}

    worker = MailboxWorker(queue, handler=_noop, worker_id="bob")
    result = await worker.process_inbox(deliver)

    assert result.claimed and result.replied
    assert "m-1:reply" in queue._tasks  # noqa: SLF001
    assert queue._tasks["m-1:reply"].recipient == "alice"  # noqa: SLF001


async def _noop(payload: dict[str, Any]) -> str | None:
    return None


# === 审计 C5:普通 nack 不得清空定向(只有「收件人疑似消失」才回广播池)===


async def test_nack_keeps_addressing_for_live_recipient(tmp_path: Path) -> None:
    """活跃 worker 的处理失败(nack)→ 定向**保留**,不能被无关 agent 领走。"""
    queue = MailboxQueue(store=MailboxStore(tmp_path / "mb.json"))
    await queue.send_message("dm-1", {"text": "secret"}, sender="alice", recipient="bob")

    await queue.claim_inbox("bob")
    await queue.nack("bob", "dm-1", error="transient")

    rec = queue._tasks["dm-1"]  # noqa: SLF001
    assert rec.status == TaskStatus.PENDING
    assert rec.recipient == "bob"  # 定向保留
    # 无关 agent 领不到
    assert await queue.claim_inbox("carol") is None
    # 原收件人仍可领到(重投不丢消息)
    assert (await queue.claim_inbox("bob")) is not None


async def test_lease_expiry_releases_addressing(tmp_path: Path) -> None:
    """对照:租约过期(收件人疑似崩了)→ 定向释放回广播池,避免永久卡死。"""
    queue = MailboxQueue(store=MailboxStore(tmp_path / "mb.json"))
    await queue.send_message("dm-2", {"text": "x"}, sender="alice", recipient="bob")
    await queue.claim_inbox("bob")

    requeued = await queue.recover_deliveries(now=queue._now() + 10_000)  # noqa: SLF001

    assert requeued == 1
    assert queue._tasks["dm-2"].recipient is None  # noqa: SLF001
    assert (await queue.claim_inbox("carol")) is not None  # 在世的 agent 能接手
