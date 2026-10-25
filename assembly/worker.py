"""assembly/worker.py —— Mailbox worker:认领任务 → 处理 → ack/nack。

任务 payload 契约(payload = VerifiedLoop 调用规格):
    {
      "task": str,            # 任务描述(NL / 修改指令)
      "repo": str | None,     # 目标仓库(默认复制 demo_repo)
      "config": str | None,   # whycode.toml(装配 MCP/Skills)
      "strict": bool,         # 严格模式
      "auto_approve": bool,   # 审批自动放行
    }
worker 单步(process_once):claim 一条 → handler(装配 + VerifiedLoop.run)→
成功 → ack;失败/异常 → nack(记错误,防毒消息无限重投由队列 max_deliveries 兜底)。
恢复(recover_expired):回收超租约的已认领投递 —— worker 崩溃后无人 ack 的任务
由在世 worker 归还队列重投(由调用方在空转时驱动,见 cli/main.py)。

本模块只编排;具体 VerifiedLoop/LLM 由 handler(装配层工厂)注入,便于测试。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from mailbox.models import TaskRecord
from mailbox.queue import MailboxQueue
from typing import Any

# worker 处理一条任务的可调用:入参 payload,产出错误消息(None = 成功)
TaskHandler = Callable[[dict[str, Any]], Awaitable[str | None]]
# agent 收到一条消息时的回执钩子:入参消息记录,产出给发件人的回复正文
MessageHandler = Callable[[TaskRecord], Awaitable[dict[str, Any] | None]]


@dataclass
class WorkerResult:
    """单步 process_once 的结果。"""

    claimed: bool = False  # 是否认领到任务
    task_id: str = ""
    acked: bool = False  # 成功确认
    nacked: bool = False  # 失败/异常(回队或 dead)
    error: str = ""


@dataclass
class InboxResult:
    """单步 process_inbox 的结果(M14:agent 间消息)。"""

    claimed: bool = False  # 是否认领到消息
    message_id: str = ""
    acked: bool = False
    nacked: bool = False
    error: str = ""
    replied: bool = False  # 是否已回执(deliver 之后)


class MailboxWorker:
    """单 worker:claim 一条 → handler → ack/nack。worker_id 参与归属校验。

    handler 抛异常 → nack(记错误),不悬挂。max_deliveries 由队列兜底(毒消息转 dead)。
    """

    def __init__(
        self,
        queue: MailboxQueue,
        handler: TaskHandler,
        *,
        worker_id: str = "worker-1",
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.worker_id = worker_id

    async def process_once(self) -> WorkerResult:
        """认领一条任务并处理,返回结果。无任务可认领 → claimed=False(不报错)。"""
        claimed = await self.queue.claim(self.worker_id)
        if claimed is None:
            return WorkerResult()
        task_id, record = claimed
        try:
            error = await self.handler(record.payload)
        except Exception as exc:  # noqa: BLE001 - handler 异常 → nack(不悬挂)
            error = f"worker 处理异常: {exc}"
        if error is None:
            acked = await self.queue.ack(self.worker_id, task_id)
            return WorkerResult(
                claimed=True, task_id=task_id, acked=acked,
                nacked=not acked, error="" if acked else "ack 失败(归属/状态不符)",
            )
        await self.queue.nack(self.worker_id, task_id, error=error)
        return WorkerResult(claimed=True, task_id=task_id, acked=False, nacked=True, error=error)

    async def recover_expired(self) -> int:
        """回收超租约的已认领投递(崩溃/超时 worker 的任务),返回重投条数。

        为什么由 worker **循环**负责:租约过期的判定需要一个「还有人活着在看队列」
        的节拍。进程内没有别的调度器会去扫(队列本身不跑定时器),而 worker 循环
        恰恰是那个节拍。所以在队列抽干(idle)时顺手回收 —— 见 cli/main.py 的
        `_drain`。

        语义:claim 后崩溃的 worker 不会自己 ack/nack,任务会永久停在 claimed;
        本方法把超租约者归还队列(pending)让在世的 worker 接手。attempts 达上限
        → dead(与 nack 同一条判定,见 `MailboxQueue._release_and_maybe_dead`)。

        没有这层接线时,「崩溃恢复 + 租约重投」只是队列库的能力而非产品行为
        (审计 M10–M15 记录的边界)。
        """
        return await self.queue.recover_deliveries()

    # ---- M14:agent 间消息车道 ----

    async def process_inbox(
        self, deliver: MessageHandler | None = None
    ) -> InboxResult:
        """认领一条发给我的消息(或广播)→ 可选回执 → ack。无消息 → claimed=False。

        `deliver` 返回 None 表示**不需要回执**(只收不回);返回 dict 则把该 dict
        作为回复正文回给原发件人(走 `queue.reply`,与父消息共享 corr_id)。

        **不给自己回执**(`record.sender == self.worker_id`):否则回执会变成一条
        发给我自己、我可认领的消息,认领→回执→再认领无限循环(审计 C3)。

        与 `process_once` 一样:handler 抛异常 → nack 回队重投,不悬挂。
        """
        claimed = await self.queue.claim_inbox(self.worker_id)
        if claimed is None:
            return InboxResult()
        message_id, record = claimed
        reply_payload: dict[str, Any] | None = None
        try:
            if deliver is not None:
                reply_payload = await deliver(record)
        except Exception as exc:  # noqa: BLE001 - deliver 异常 → nack(不悬挂)
            error = f"消息处理异常: {exc}"
            await self.queue.nack(self.worker_id, message_id, error=error)
            return InboxResult(claimed=True, message_id=message_id, nacked=True, error=error)

        replied = False
        # **不给自己的消息回执**:回执的收件人是父消息的 sender,若那就是我自己,
        # 这条回执本身就是一条发给我、且我可认领的消息 → 认领→回执→再认领…无限循环,
        # 快照无界增长且 worker 热转(审计 M10–M15 C3,实测 6 轮产出
        # m-1 / m-1:reply / m-1:reply:reply … 永不终止)。
        # 定向消息在两个活跃 worker 之间同样会乒乓 —— 根因都是「回执可被再认领」。
        if reply_payload is not None and record.sender != self.worker_id:
            try:
                await self.queue.reply(
                    message_id, f"{message_id}:reply", reply_payload, sender=self.worker_id
                )
                replied = True
            except ValueError as exc:
                # 防御性:父消息不该缺 sender(claim 到的记录必有),但若快照被外部
                # 改坏,不能让异常穿出 process_inbox —— 那会留下无 nack 的 claimed。
                error = f"回执失败: {exc}"
                await self.queue.nack(self.worker_id, message_id, error=error)
                return InboxResult(
                    claimed=True, message_id=message_id, nacked=True, error=error
                )
        acked = await self.queue.ack(self.worker_id, message_id)
        return InboxResult(
            claimed=True,
            message_id=message_id,
            acked=acked,
            nacked=not acked,
            error="" if acked else "ack 失败(归属/状态不符)",
            replied=replied,
        )
