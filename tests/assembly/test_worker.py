"""assembly/worker 单元测试:Mailbox worker 单步(claim→handle→ack/nack)(M9 W3)。"""

from __future__ import annotations

from mailbox.models import TaskStatus
from mailbox.queue import MailboxQueue
from mailbox.store import MailboxStore
from pathlib import Path

from assembly.worker import MailboxWorker


async def _submit_and_claim(queue: MailboxQueue) -> None:
    """helper:入队一条任务(测试用固定 task_id)。"""
    await queue.submit("t1", {"task": "修复 divide"})


def _memory_queue() -> MailboxQueue:
    return MailboxQueue(now_fn=lambda: 1000)


async def test_claim_ack_flow(tmp_path) -> None:
    """正常处理 → ack,任务转 done。"""
    queue = _memory_queue()
    await queue.submit("t1", {"task": "ok"})
    calls: list[dict] = []

    async def handler(payload: dict) -> str | None:
        calls.append(payload)
        return None  # 成功

    worker = MailboxWorker(queue, handler)
    result = await worker.process_once()
    assert result.claimed and result.acked and not result.nacked
    assert calls == [{"task": "ok"}]
    # 任务 done
    rec = queue.state()["t1"]
    assert rec["status"] == TaskStatus.DONE.value


async def test_claim_nack_on_failure(tmp_path) -> None:
    """handler 返回错误 → nack(回队重投,非 dead 直到超限)。"""
    queue = _memory_queue()
    await queue.submit("t1", {"task": "boom"})

    async def handler(payload: dict) -> str | None:
        return "任务失败"  # 失败

    worker = MailboxWorker(queue, handler)
    result = await worker.process_once()
    assert result.claimed and result.nacked and not result.acked
    assert "任务失败" in result.error
    # nack 未超限 → 回 pending(attempts=1)
    rec = queue.state()["t1"]
    assert rec["status"] == TaskStatus.PENDING.value
    assert rec["attempts"] == 1


async def test_handler_exception_becomes_nack(tmp_path) -> None:
    """handler 抛异常 → nack(不悬挂),错误被记录。"""
    queue = _memory_queue()
    await queue.submit("t1", {"task": "x"})

    async def handler(payload: dict) -> str | None:
        raise RuntimeError("handler crash")

    worker = MailboxWorker(queue, handler)
    result = await worker.process_once()
    assert result.nacked
    assert "handler crash" in result.error
    rec = queue.state()["t1"]
    assert rec["last_error"] == "worker 处理异常: handler crash"


async def test_idle_when_queue_empty() -> None:
    """队列空 → claimed=False(不报错)。"""

    async def never(payload: dict) -> str | None:
        return None  # 不会被调用

    worker = MailboxWorker(_memory_queue(), never)
    result = await worker.process_once()
    assert not result.claimed and not result.acked


async def test_persisted_ack_lands_snapshot(tmp_path: Path) -> None:
    """worker 用带 store 的队列:ack 后快照落盘(done 状态持久)。"""
    store_path = tmp_path / "queue.json"
    queue = MailboxQueue(
        store=MailboxStore(store_path),
        now_fn=lambda: 1000,
    )
    await queue.submit("t1", {"task": "ok"})

    async def ok_handler(payload: dict) -> str | None:
        return None

    worker = MailboxWorker(queue, ok_handler)
    result = await worker.process_once()
    assert result.acked
    # 快照持久:重开 store 读回 → t1 已 done
    loaded = MailboxStore(store_path).load()
    assert loaded["t1"]["status"] == TaskStatus.DONE.value


# === C 包接线:租约回收由 worker 编排(崩溃恢复真正可达) ===


async def test_recover_expired_requeues_crashed_claim() -> None:
    """崩溃 worker 留下的超租约 claimed → 被回收重投,任务不再永久卡死。

    承重:接线前 worker 循环从不调用 recover_deliveries,claim 后崩溃的任务会
    **永久停在 claimed**(审计 M10–M15 留档的边界)。本测试锁住「回收可达」。
    """
    clock = {"t": 1000}
    queue = MailboxQueue(now_fn=lambda: clock["t"], lease_seconds=30)

    async def never(payload: dict) -> str | None:
        return None

    # worker-A 认领后「崩溃」(不 ack 不 nack),租约 30s 内到期
    await queue.submit("t1", {"task": "x"})
    crashed = MailboxWorker(queue, never, worker_id="worker-a")
    claimed = await queue.claim("worker-a")
    assert claimed is not None and claimed[0] == "t1"

    survivor = MailboxWorker(queue, never, worker_id="worker-b")
    assert await survivor.recover_expired() == 0  # 租约未过期 → 不动
    clock["t"] = 2000  # 远超租约
    assert await survivor.recover_expired() == 1  # 回收
    assert queue.state()["t1"]["status"] == TaskStatus.PENDING.value
    assert crashed.worker_id != survivor.worker_id  # 确实是另一个 worker 接的手


async def test_recovered_task_can_be_finished_by_survivor() -> None:
    """回收后由在世 worker 正常领走并完成 → 终态 done(端到端的「崩溃恢复」)。"""
    clock = {"t": 1000}
    queue = MailboxQueue(now_fn=lambda: clock["t"], lease_seconds=30)
    await queue.submit("t1", {"task": "x"})
    await queue.claim("worker-a")  # 崩溃:不 ack

    clock["t"] = 2000
    done: list[dict] = []

    async def handler(payload: dict) -> str | None:
        done.append(payload)
        return None

    survivor = MailboxWorker(queue, handler, worker_id="worker-b")
    assert await survivor.recover_expired() == 1
    result = await survivor.process_once()
    assert result.acked and result.task_id == "t1"
    assert done == [{"task": "x"}]
    assert queue.state()["t1"]["status"] == TaskStatus.DONE.value


async def test_recover_expired_stops_at_max_deliveries() -> None:
    """崩溃型毒消息:反复「认领→崩溃→回收」达上限 → dead,不无限重投。

    nack 路径的 attempts 判定早在 M6 F1 修过;这里锁住**回收路径**共用同一判定
    (两条重投路径语义一致,见 `MailboxQueue._release_and_maybe_dead`)。
    """
    clock = {"t": 1000}
    queue = MailboxQueue(now_fn=lambda: clock["t"], lease_seconds=30, max_deliveries=2)
    await queue.submit("t1", {"task": "poison"})

    async def never(payload: dict) -> str | None:
        return None

    worker = MailboxWorker(queue, never, worker_id="worker-a")
    for _ in range(3):  # 超上限次数
        await queue.claim("worker-a")  # 认领后崩溃
        clock["t"] += 1000
        await worker.recover_expired()

    rec = queue.state()["t1"]
    assert rec["status"] == TaskStatus.DEAD.value
    assert rec["attempts"] <= 2, rec

