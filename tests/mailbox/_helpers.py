"""M6 测试共享工具:claim 断言辅助(两个测试文件复用)。"""

from __future__ import annotations

from mailbox.models import TaskRecord
from mailbox.queue import MailboxQueue


async def claim_or_fail(q: MailboxQueue, worker: str) -> tuple[str, TaskRecord]:
    """claim 并断言非空(测试中任务必然可领)。"""
    claimed = await q.claim(worker)
    assert claimed is not None
    return claimed
