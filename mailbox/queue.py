"""mailbox/queue.py —— 任务队列状态机(claim/ack/nack + lease 重投)+ Agent 间消息。

语义设计(要点):
- at-least-once:claim 把任务置 claimed 并给租约;worker 在租约内 ack 才算成功。
  lease 过期未 ack(崩溃/超时)→ recover_deliveries 重投。可能重复处理 → 由
  消费者以 task_id 为幂等锚点吸收(exactly-once = at-least-once + 幂等消费者)。
- claim 原子性:**两层锁**。进程内 `asyncio.Lock`,跨进程 `MailboxStore.lock()`
  (OS 咨询锁)。缺任一层,另一层都保护不到(同进程并发任务 / 多进程共享快照)。
- 毒消息防护:每任务 max_deliveries 上限。nack **和** lease 崩溃重投两条路径
  都检查 attempts,达限 → dead,不无限重投。
- 归属校验:ack/nack 校验 claimed_by == worker,防误确认他人持有的任务。

M14 跨进程正确性(这是本模块最容易写错的地方):
  只做「原子快照」**不够**。两个进程各自把内存 dict 整个序列化写回时,后写者会把
  先写者的变更**整体冲掉**(丢失更新),即使每次写本身是原子的。修法是让
  「重新载入磁盘 → 改 → 写回」整段发生在**锁内**:见 `_critical`。把 reload 放进锁里,
  使「忘记同步」这件事**不可能发生**——调用方想绕过都绕不过。

持久化:store 是保存回调(可选)。每次状态变更后在锁内 reload + save,进程崩溃从磁盘重建。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from mailbox.models import TaskKind, TaskRecord, TaskStatus
from mailbox.store import MailboxStore
from typing import Any

# 默认租约时长(秒):worker 必须在此时限内 ack,否则视为崩溃可重投
DEFAULT_LEASE_SECONDS = 30
# 单任务最大投递次数:nack/重投超限 → dead
DEFAULT_MAX_DELIVERIES = 3


def _reject_kind_clash(record: TaskRecord, expected: TaskKind) -> None:
    """幂等命中但**车道不符** → 抛错(不静默返回)。

    共用一个 id 命名空间就必须守住它:否则 `submit("X")` 在 X 已是消息时
    会「成功返回」却什么都不创建 —— 任务永远领不到,且调用方看不出异常。
    """
    if record.kind != expected:
        raise ValueError(
            f"id {record.task_id!r} 已被另一条车道占用:"
            f"现有 kind={record.kind.value},请求 kind={expected.value}"
        )


class MailboxQueue:
    """可跨进程共享的任务队列。

    注入项:
      now_fn    —— 时钟来源(默认 time.time 整数秒;测试注入确定值)。
      store     —— MailboxStore;给定时每次状态变更在**跨进程锁内** reload + 原子落盘。
      lease_seconds / max_deliveries —— 可配语义旋钮。
    """

    def __init__(
        self,
        *,
        now_fn: Callable[[], int] | None = None,
        store: MailboxStore | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
    ) -> None:
        import time

        self._now = now_fn or (lambda: int(time.time()))
        self._store = store
        self._lease_seconds = lease_seconds
        self._max_deliveries = max_deliveries
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()
        if store is not None:
            self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        """从磁盘快照重建(进程重启)。存在记录 → 恢复,否则空队列起步。

        整表替换而非 update:快照是「现在是什么」的权威视图,被删掉的记录
        不该因为内存里还留着而复活。
        """
        assert self._store is not None
        loaded: dict[str, TaskRecord] = {}
        for task_id, raw in self._store.load().items():
            loaded[task_id] = TaskRecord.model_validate(raw)
        self._tasks = loaded

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save(
                {tid: rec.model_dump() for tid, rec in self._tasks.items()}
            )

    @asynccontextmanager
    async def _critical(self) -> AsyncIterator[None]:
        """状态变更临界区:跨进程锁 → **重新载入磁盘** → 交给调用方改 → 写出。

        `_load()` 必须在这里(锁内),不能在 `__init__` 里「载一次就用」:
        别的进程在我们持有内存期间可能已经改过快照。每次变更前重读,才能把
        「读-改-写」变成真正的原子操作(否则就是丢失更新)。

        锁内是**同步**文件 I/O(取锁 + 读快照 + 原子写)。快照是本地小文件,
        且工作台规模下单次操作为毫秒级 —— 简单可靠优先,不引入线程池绕行。
        """
        if self._store is None:
            yield  # 无持久化 = 单进程内存队列,无跨进程语义要守
            return
        with self._store.lock():
            self._load()
            yield
            self._persist()

    def close(self) -> None:
        """释放锁句柄(进程退出前调用;不调用由 OS 兜底)。"""
        if self._store is not None:
            self._store.close()

    # ---- 公共查询 ----

    def state(self) -> dict[str, dict[str, Any]]:
        """当前全部任务现状(外部读/审计用)。"""
        return {tid: rec.model_dump() for tid, rec in self._tasks.items()}

    def _next_pending(self) -> str | None:
        """FIFO:按 created_at 取最早一条 pending **任务**。

        必须过滤 `kind == TASK`:否则 worker 会把 agent 间消息当成作业领走
        (把一条「帮我看看这个」的留言当任务跑),收件人永远等不到它。
        """
        pending = sorted(
            (rec.created_at, tid)
            for tid, rec in self._tasks.items()
            if rec.status == TaskStatus.PENDING and rec.kind == TaskKind.TASK
        )
        if not pending:
            return None
        return pending[0][1]

    # ---- M14:消息车道(claim_inbox 用;与 claim 的任务车道互不串味)----

    def _is_inbox_candidate(self, rec: TaskRecord) -> bool:
        """只判**类别**(是不是 agent 间消息);寻址判定见 `_next_inbox`。

        刻意不带 `recipient` 判断:本函数不含 `agent_id`,无法知道「给谁」。
        把它与寻址拆开,是让「类别」与「归属」两个概念各自可单独断言。
        """
        return rec.kind != TaskKind.TASK

    def _next_inbox(self, agent_id: str) -> str | None:
        """FIFO 取最早一条 pending 消息。

        - **广播**(`recipient is None`)进公共池:任一 agent 都可领(只领一次)。
        - **定向**(`recipient == agent_id`)只有收件人能领;别人的定向消息**看不见**,
          也不会像广播那样被先来者「顺走」——所以定向消息不会丢。
        - 另有一条兜底:收件人已不在场时,超租约重投会把定向消息退回公共池
          (见 `_release_and_maybe_dead`),否则它会永远卡在 claimed。
        """
        candidates = sorted(
            (rec.created_at, tid)
            for tid, rec in self._tasks.items()
            if rec.status == TaskStatus.PENDING
            and self._is_inbox_candidate(rec)
            and (rec.recipient is None or rec.recipient == agent_id)
        )
        if not candidates:
            return None
        return candidates[0][1]

    # ---- 同步内核(被 async 包装调用,锁内执行)----

    def _release_and_maybe_dead(
        self, rec: TaskRecord, *, error: str, recipient_gone: bool = False
    ) -> bool:
        """归还任务:attempts 未超限 → 回 pending 重投;超限 → dead。

        nack 与 lease 崩溃重投共用此判定,保证两条重投路径对
        max_deliveries 的语义一致(防无限重投毒消息)。

        `recipient_gone`:调用方判定「原收件人已不在场」(lease 过期)时置 True →
        定向消息**失去定向**、回到广播池。理由:收件人崩了,继续保持定向的话它退回
        公共池之前**谁也领不到** → 永远卡死。lease 过期多半正是它崩了。

        ⚠️ **活跃 worker 的普通 nack 不适用**(故默认 False):那只是这次处理失败,
        收件人还在场 —— 此时把定向消息广播出去,等于**一次可恢复的失败就把私信
        泄漏给所有 agent**(审计 M10–M15 C5,实测 bob nack 后 carol 立刻领到了
        只发给 bob 的消息)。所以去定向**只发生在「收件人疑似消失」时**。

        返回是否已重投(pending)。调用方据此统计,避免突变后对
        status 做二次比较(类型收窄失效)。
        """
        rec.claimed_by = None
        rec.last_error = error
        if rec.attempts >= self._max_deliveries:
            rec.status = TaskStatus.DEAD
            return False
        if recipient_gone and rec.kind != TaskKind.TASK and rec.recipient is not None:
            rec.recipient = None  # 收件人疑似已崩 → 回广播池,避免永久卡死
        rec.status = TaskStatus.PENDING  # 回队重投
        return True

    def _submit(self, task_id: str, payload: dict[str, Any]) -> TaskRecord:
        """幂等键:task_id 已存在 → 返回既有记录(不重复入队)。

        `kind` 冲突 → `ValueError`:两条车道共用一个 id 命名空间,而 `_next_pending`
        只认 TASK、`_next_inbox` 只认非 TASK。若允许同 id 跨车道「幂等命中」,
        第二次调用会**什么都不创建却返回成功**(任务永远领不到),是静默数据丢失。
        宁可在写入点报错。
        """
        existing = self._tasks.get(task_id)
        if existing is not None:
            _reject_kind_clash(existing, TaskKind.TASK)
            return existing.model_copy(deep=True)
        rec = TaskRecord(
            task_id=task_id,
            payload=payload,
            status=TaskStatus.PENDING,
            created_at=self._now(),
        )
        self._tasks[task_id] = rec
        return rec.model_copy(deep=True)

    def _claim(self, agent_id: str, *, inbox: bool = False) -> tuple[str, TaskRecord] | None:
        tid = self._next_inbox(agent_id) if inbox else self._next_pending()
        if tid is None:
            return None
        rec = self._tasks[tid]
        rec.status = TaskStatus.CLAIMED
        rec.claimed_by = agent_id
        rec.attempts += 1
        rec.lease_until = self._now() + self._lease_seconds
        return tid, rec.model_copy(deep=True)  # 快照隔离:调用方改不到内部态

    def _ack(self, worker_id: str, task_id: str) -> bool:
        rec = self._tasks.get(task_id)
        if rec is None or rec.status != TaskStatus.CLAIMED or rec.claimed_by != worker_id:
            return False
        rec.status = TaskStatus.DONE
        rec.claimed_by = None
        rec.done_at = self._now()
        return True

    def _nack(self, worker_id: str, task_id: str, *, error: str) -> bool:
        rec = self._tasks.get(task_id)
        if rec is None or rec.status != TaskStatus.CLAIMED or rec.claimed_by != worker_id:
            return False
        self._release_and_maybe_dead(rec, error=error)
        return True

    def _recover_deliveries(self, now: int) -> int:
        """扫描超租约的 claimed → 回 pending(消息重投核心)。返回重投条数。

        attempts 已达 max_deliveries 的任务 → dead(与 nack 同语义,
        崩溃型毒消息不会因「worker 反复崩溃、从不 nack」而无限重投)。
        """
        requeued = 0
        for rec in self._tasks.values():
            if (
                rec.status == TaskStatus.CLAIMED
                and rec.lease_until is not None
                and now > rec.lease_until
                and self._release_and_maybe_dead(
                    rec, error="lease expired", recipient_gone=True
                )
            ):
                requeued += 1
        return requeued

    # ---- async 公开接口(带锁 + 持久化)----

    async def submit(self, task_id: str, payload: dict[str, Any] | None = None) -> TaskRecord:
        async with self._lock, self._critical():
            return self._submit(task_id, payload or {})

    async def claim(self, worker_id: str) -> tuple[str, TaskRecord] | None:
        async with self._lock, self._critical():
            return self._claim(worker_id)

    async def ack(self, worker_id: str, task_id: str) -> bool:
        async with self._lock, self._critical():
            return self._ack(worker_id, task_id)

    async def nack(self, worker_id: str, task_id: str, *, error: str = "") -> bool:
        async with self._lock, self._critical():
            return self._nack(worker_id, task_id, error=error)

    async def recover_deliveries(self, now: int | None = None) -> int:
        async with self._lock, self._critical():
            return self._recover_deliveries(now if now is not None else self._now())

    # ---- M14:Agent 间寻址消息 ----

    async def send_message(
        self,
        message_id: str,
        payload: dict[str, Any],
        *,
        sender: str,
        recipient: str | None = None,
        corr_id: str | None = None,
        kind: TaskKind = TaskKind.MESSAGE,
    ) -> TaskRecord:
        """投递一条 agent 间消息。幂等键与 `submit` 同:`message_id` 已存在 → 不重复入队。

        `recipient=None` 表示**广播**(任一 agent 可 `claim_inbox` 领取,只领一次)。
        消息与任务共用同一条快照/租约/重投状态机,不 fork 一套并行状态机。
        """
        async with self._lock, self._critical():
            existing = self._tasks.get(message_id)
            if existing is not None:
                _reject_kind_clash(existing, kind)
                return existing.model_copy(deep=True)
            rec = TaskRecord(
                task_id=message_id,
                payload=payload,
                status=TaskStatus.PENDING,
                created_at=self._now(),
                kind=kind,
                sender=sender,
                recipient=recipient,
                corr_id=corr_id if corr_id is not None else message_id,
            )
            self._tasks[message_id] = rec
            return rec.model_copy(deep=True)

    async def reply(
        self, parent_id: str, reply_id: str, payload: dict[str, Any], *, sender: str
    ) -> TaskRecord:
        """回执一条消息:收件人取自父消息的 `sender`,并共享其 `corr_id`。

        幂等(与 `_submit`/`send_message` 同构):`reply_id` 已存在 → 返回既有记录,
        **绝不覆写**。这是正确性要求而非优化 —— 父消息可能被 lease 重投并再次交付
        (bob 在 reply 与 ack 之间崩溃),若允许覆写,一条**已 ack 的 DONE 回执**会被
        打回 PENDING、`attempts` 归零、`done_at` 清空,发件人第二次收到同一回执。
        一条父消息的回执只生成一次。

        父消息不存在 / 无 sender → `ValueError`(不静默造一条无主回复)。
        """
        async with self._lock, self._critical():
            existing = self._tasks.get(reply_id)
            if existing is not None:
                _reject_kind_clash(existing, TaskKind.REPLY)
                return existing.model_copy(deep=True)
            parent = self._tasks.get(parent_id)
            if parent is None:
                raise ValueError(f"unknown message to reply to: {parent_id}")
            if parent.sender is None:
                raise ValueError(f"message has no sender to reply to: {parent_id}")
            rec = TaskRecord(
                task_id=reply_id,
                payload=payload,
                status=TaskStatus.PENDING,
                created_at=self._now(),
                kind=TaskKind.REPLY,
                sender=sender,
                recipient=parent.sender,
                corr_id=parent.corr_id or parent_id,
            )
            self._tasks[reply_id] = rec
            return rec.model_copy(deep=True)

    async def claim_inbox(self, agent_id: str) -> tuple[str, TaskRecord] | None:
        """认领一条发给我的消息(定向)或任一条广播。FIFO;无消息 → None。

        与 `claim` 是两条**互不干扰**的车道:worker 的 `claim` 只拿任务,
        agent 的 `claim_inbox` 只拿消息 —— 否则忙的 worker 会把别人的消息吃掉。
        """
        async with self._lock, self._critical():
            return self._claim(agent_id, inbox=True)
