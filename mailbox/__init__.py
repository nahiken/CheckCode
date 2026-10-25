"""Multi-Agent Mailbox 共享任务队列(M6)+ 跨进程锁与 Agent 间消息(M14)。"""

from mailbox.locking import MailboxLockTimeout, file_lock
from mailbox.models import TaskKind, TaskRecord, TaskStatus
from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore

__all__ = [
    "MailboxLockTimeout",
    "MailboxQueue",
    "MailboxStore",
    "TaskKind",
    "TaskRecord",
    "TaskStatus",
    "file_lock",
]
