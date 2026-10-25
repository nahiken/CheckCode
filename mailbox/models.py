"""mailbox/models.py —— Multi-Agent Mailbox 数据模型。

任务状态机(要点:状态机幂等设计):
    pending → claimed → done
    claimed → pending(lease 过期重投 / nack 未超限)
    claimed → dead(lease 重投 / nack 达 max_deliveries,毒消息不无限重投)

TaskRecord 同时是内存态与磁盘快照的交换格式(JSON 序列化)。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """任务生命周期状态。"""

    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    DEAD = "dead"


class TaskKind(StrEnum):
    """记录的用途类别(M14:同一条队列既跑任务也送消息)。

    两个「车道」互不串味:`claim` 只领 TASK,`claim_inbox` 只领 MESSAGE/REPLY。
    这样多个 worker 争抢任务时不会把某个 agent 的定向消息吃掉。
    """

    TASK = "task"  # 待执行的作业(worker claim 车道)
    MESSAGE = "message"  # agent → agent 的定向/广播消息(inbox 车道)
    REPLY = "reply"  # 对某条消息的回执(inbox 车道)


class TaskRecord(BaseModel):
    """一条任务的完整状态(内存 + 磁盘快照共用)。

    M14 新增字段**全部有默认值** → 旧快照文件仍可解析(`kind` 缺省即 `task`,
    即「M6 时代写下的记录都是任务」),不需要迁移脚本。
    """

    __test__ = False  # 防止 pytest 收集

    task_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    created_at: int = 0
    claimed_by: str | None = None
    attempts: int = 0
    lease_until: int | None = None
    done_at: int | None = None
    last_error: str | None = None

    # ---- M14:消息与寻址 ----
    kind: TaskKind = TaskKind.TASK
    sender: str | None = None  # 发出者 agent_id(任务为 None)
    recipient: str | None = None  # 收件 agent_id;**None = 广播**(任一 agent 可 claim)
    corr_id: str | None = None  # 关联 id:回复与其父消息共享同一 corr_id
