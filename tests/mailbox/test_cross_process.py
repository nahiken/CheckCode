"""M14 承重测试:真多进程共享同一队列快照。

**为什么必须真起进程**:同进程 `asyncio.gather` 只证明 `asyncio.Lock` 有效,
证明不了**文件锁**有效 —— 文件锁保护的是「另一个进程的临界区」,而单进程里
根本没有另一个进程。本文件用 `multiprocessing.get_context("spawn")` 起真进程
(Windows 必需 spawn),共享同一个快照路径。

覆盖的两类跨进程缺陷:
1. **重复认领**:两进程同时 claim 到同一条任务(没有互斥)。
2. **丢失更新**:每个进程只 submit 自己那一批,但实现是把**整表**序列化写回。
   若「读-改-写」不在锁内,后写者会整体冲掉先写者 —— 最终快照少一批任务。
   (这条在加锁前必然失败;它在单进程测试里**永远测不出来**,因为只有一个内存视图。)
"""

from __future__ import annotations

import multiprocessing
import time
from collections import Counter
from mailbox.store import MailboxStore
from pathlib import Path
from typing import Any

from tests.mailbox._mp import (
    claim_no_ack_loop,
    claim_tasks,
    locked_writer,
    submit_batch,
    unlocked_reader,
)

CTX = multiprocessing.get_context("spawn")

# 每个测试的墙上时钟上限:子进程死锁/互等时不能把整个套件挂住
_WALL_LIMIT = 180.0


def _run_claimers(
    snap: Path,
    workers: list[str],
    timeout: float = 120.0,
    *,
    limit: int | None = None,
    delay: float = 0.0,
    extra: list[tuple[Any, tuple[Any, ...]]] | None = None,
) -> dict[str, list[str]]:
    """起 len(workers) 个真进程同时 claim/ack 同一快照,回传各自领取的 task_id。

    `extra` 可与 claimer 一起起别的入口(如 submitter),模拟两条车道同时动。
    """
    start = CTX.Event()
    results = CTX.Queue()
    jobs: list[tuple[Any, tuple[Any, ...]]] = [
        (claim_tasks, (str(snap), w, start, results, limit, delay)) for w in workers
    ] + list(extra or [])
    procs = [CTX.Process(target=fn, args=args) for fn, args in jobs]
    for p in procs:
        p.start()
    start.set()  # 同时放行 → 最大化真并发争抢
    try:
        out: dict[str, list[str]] = {}
        for _ in range(len(procs)):
            key, payload = results.get(timeout=timeout)
            out[key] = payload
    finally:
        for p in procs:
            p.join(timeout=30)
            if p.is_alive():
                p.terminate()
    return out


def _seed(snap: Path, prefix: str, count: int) -> None:
    """父进程建初始快照并投递一批任务(避免多进程同时「创建」快照)。"""
    import asyncio
    from mailbox.queue import MailboxQueue

    async def _run() -> None:
        q = MailboxQueue(store=MailboxStore(snap))
        try:
            for i in range(count):
                await q.submit(f"{prefix}{i:02d}", {"n": i})
        finally:
            q.close()

    asyncio.run(_run())


class TestCrossProcessClaim:
    def test_no_task_claimed_twice_across_processes(self, tmp_path: Path) -> None:
        """N 个真进程争抢同一批任务:无重复认领、无遗漏、全部 ack 到 done。"""
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})  # 建初始快照,避免多进程同时「创建」
        _seed(snap, "t-", 24)

        workers = ["w-1", "w-2", "w-3", "w-4"]
        claimed = _run_claimers(snap, workers, timeout=_WALL_LIMIT)

        all_ids = [tid for seen in claimed.values() for tid in seen]
        dupes = [tid for tid, n in Counter(all_ids).items() if n > 1]
        assert dupes == [], f"跨进程重复认领: {dupes}"
        assert sorted(all_ids) == [f"t-{i:02d}" for i in range(24)]  # 无遗漏

        # 全部落到 done(每个 worker 领取后立即 ack)
        final = MailboxStore(snap).load()
        assert len(final) == 24
        assert all(rec["status"] == "done" for rec in final.values())

    def test_all_worker_processes_are_used(self, tmp_path: Path) -> None:
        """4 个进程各**恰好**领 10 条 = 40 条全部被真并发地领走。

        这条存在的意义:如果只有一个进程真的跑起来(或其余进程根本抢不到锁),
        「无重复认领」会**因为没人竞争而白白通过** —— 测试是空的。让每个进程
        必须领满 10 条,就要求四个进程都真的参与了争抢。

        不能写成「每个进程都领到 >0」:那是调度运气(快的进程可能一路领到底)。
        """
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})
        _seed(snap, "t-", 40)

        workers = ["w-1", "w-2", "w-3", "w-4"]
        claimed = _run_claimers(snap, workers, timeout=_WALL_LIMIT, limit=10, delay=0.002)

        assert {w: len(v) for w, v in claimed.items()} == {w: 10 for w in workers}
        all_ids = [tid for seen in claimed.values() for tid in seen]
        assert sorted(all_ids) == [f"t-{i:02d}" for i in range(40)]


class TestCrossProcessLostUpdate:
    def test_concurrent_submitters_lose_nothing(self, tmp_path: Path) -> None:
        """4 个进程各 submit 15 条**各自唯一**的 id → 快照里必须是 60 条。

        这是丢失更新的探测器:整表序列化写回若不在锁内,各进程的变更会互相
        覆盖,最终条数 < 60。加了「锁内 reload 再改」后,每一批都留下。
        """
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})

        start = CTX.Event()
        results = CTX.Queue()
        procs = [
            CTX.Process(target=submit_batch, args=(str(snap), f"p{i}", 15, start, results))
            for i in range(4)
        ]
        for p in procs:
            p.start()
        start.set()
        try:
            for _ in procs:
                prefix, status = results.get(timeout=_WALL_LIMIT)
                assert status == "ok", f"{prefix} 提交失败: {status}"
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()

        final = MailboxStore(snap).load()
        assert len(final) == 60, f"丢失更新:期望 60 条,实际 {len(final)}"
        assert sorted(final) == sorted(f"p{i}-{j}" for i in range(4) for j in range(15))

    def test_submitters_and_claimers_share_one_snapshot(self, tmp_path: Path) -> None:
        """3 个投递进程 + 3 个认领进程共用同一快照:最终 30 条,状态自洽。

        比上一个更狠的一点:投递与认领**同时**发生,任何一次「整表写回」若不在
        锁内重新载入,都可能把对方的变更抹掉(投递被抹 = 丢任务;ack 被抹 =
        已完成任务复活成 pending)。
        """
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})

        start = CTX.Event()
        results = CTX.Queue()
        jobs: list[tuple[Any, tuple[Any, ...]]] = [
            (submit_batch, (str(snap), f"q{i}", 10, start, results)) for i in range(3)
        ] + [
            (claim_tasks, (str(snap), f"w{i}", start, results, None, 0.001)) for i in range(3)
        ]
        procs = [CTX.Process(target=fn, args=args) for fn, args in jobs]
        for p in procs:
            p.start()
        start.set()
        try:
            for _ in procs:
                results.get(timeout=_WALL_LIMIT)
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()

        final = MailboxStore(snap).load()
        assert len(final) == 30, f"投递总数不符: {len(final)}"
        statuses = Counter(rec["status"] for rec in final.values())
        assert set(statuses) <= {"done", "pending", "claimed"}
        assert sum(statuses.values()) == 30


class TestWindowsReplaceContention:
    """写者/读者并发下的 `os.replace` 共享冲突(Windows 实测,非理论)。

    Windows 要求改名目标没有其他打开句柄 → 1 个写者 + N 个**无锁**读者时,
    两侧都会抛 `PermissionError: [WinError 5]`。实测(未加重试):写失败 119/150、
    读失败 15/1500。store 两侧各加了退避重试(见 `store.py` 模块 docstring)。

    本测试是**回归哨兵**:去掉任一侧重试,这里就会红。
    - `test_locked_writer_survives_unlocked_readers`:只保写侧重试
    - `test_unlocked_reader_survives_locked_writer`:只保读侧重试
    """

    def test_locked_writer_survives_unlocked_readers(self, tmp_path: Path) -> None:
        """1 个写者(锁内 replace)+ 3 个无锁读者并发:写者零失败。"""
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})

        start = CTX.Event()
        results = CTX.Queue()
        procs = [CTX.Process(target=locked_writer, args=(str(snap), 120, "w", start, results))]
        procs += [
            CTX.Process(target=unlocked_reader, args=(str(snap), 800, start, results))
            for _ in range(3)
        ]
        for p in procs:
            p.start()
        start.set()
        try:
            errs: dict[str, list[str]] = {}
            for _ in procs:
                key, failures = results.get(timeout=_WALL_LIMIT)
                errs[key] = failures
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()

        assert errs["w"] == [], f"写者在并发读下失败: {Counter(errs['w'])}"
        assert len(MailboxStore(snap).load()) == 120  # 每次写都真的落盘了

    def test_unlocked_reader_survives_locked_writer(self, tmp_path: Path) -> None:
        """反方向:读者(无锁 load)在写者持续 replace 时零失败。

        去掉读侧重试会红 —— 「读能扛住写」和「写能扛住读」是两个独立保证,
        不能因为前者通过就假定后者也成立。
        """
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})

        start = CTX.Event()
        results = CTX.Queue()
        procs = [CTX.Process(target=locked_writer, args=(str(snap), 120, "w", start, results))]
        procs += [
            CTX.Process(target=unlocked_reader, args=(str(snap), 800, start, results))
            for _ in range(3)
        ]
        for p in procs:
            p.start()
        start.set()
        try:
            errs: dict[str, list[str]] = {}
            for _ in procs:
                key, failures = results.get(timeout=_WALL_LIMIT)
                errs[key] = failures
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()

        readers = [f for k, f in errs.items() if k != "w"]
        assert all(f == [] for f in readers), f"读者在并发写下失败: {[Counter(f) for f in readers]}"


class TestAttemptsBoundedAcrossProcesses:
    """规格逐字要求承重测试断言「**总 attempts 有界**」。

    审计中-2 发现:原文件对 `attempts` **零提及**,全 `tests/mailbox/` 也只有
    单进程的**等值**断言。而 at-least-once 队列最危险的失效模式正是**毒消息无限
    重投**,唯一守卫就是这个上界 —— 单进程的等值断言证明不了「跨进程重投不会
    绕过 max_deliveries」。
    """

    def test_attempts_bounded_when_workers_crash_repeatedly(self, tmp_path: Path) -> None:
        """真多进程:3 个 worker **只 claim 不 ack**(领完即崩),父进程不断 recover。

        租约设为 1 秒(远小于测试时长),子进程领完不 ack 就等下一轮 —— 租约必然
        过期,父进程每轮 recover 把它放回 pending,任务被反复投递。若上界在多进程
        下失效,`attempts` 会无限增长;有界则应停在 `max_deliveries` 且不再回 pending。
        """
        snap = tmp_path / "mailbox.json"
        MailboxStore(snap).save({})
        _seed(snap, "t-", 6)

        max_deliveries = 3
        start = CTX.Event()
        results = CTX.Queue()
        procs = [
            CTX.Process(
                target=claim_no_ack_loop, args=(str(snap), f"w-{i}", 25, start, results)
            )
            for i in range(3)
        ]
        # 父进程不断回收过期租约(真实时钟;租约 1s → 每轮只需略等)
        import asyncio
        from mailbox.queue import MailboxQueue

        async def _recover_loop() -> None:
            q = MailboxQueue(
                store=MailboxStore(snap),
                lease_seconds=1,
                max_deliveries=max_deliveries,
            )
            try:
                for _ in range(60):
                    # 时钟取**远未来** → 任何已被 claim 的记录都判定为租约过期。
                    # 不用真实时钟:子进程 claim 的瞬间与父进程 recover 的瞬间谁先
                    # 谁后是竞态,用未来时钟把「必过期」变成确定性事实。
                    await q.recover_deliveries(now=int(time.time()) + 3600)
                    await asyncio.sleep(0.05)
            finally:
                q.close()

        for p in procs:
            p.start()
        start.set()
        try:
            asyncio.run(_recover_loop())
            peaks = {w: peak for w, peak in (results.get(timeout=_WALL_LIMIT) for _ in procs)}
        finally:
            for p in procs:
                p.join(timeout=30)
                if p.is_alive():
                    p.terminate()

        # 子进程收工后由**父进程**做最后一轮回收:此时没有任何 claimer 在跑,
        # 每个已被领走的记录都必被判过期 → 达上限的进 dead、未达的回 pending 后
        # 立刻被(已结束的)claimer 之外无人接手 —— 故终态只可能是 dead。
        async def _final_recover() -> None:
            q = MailboxQueue(
                store=MailboxStore(snap), lease_seconds=1, max_deliveries=max_deliveries
            )
            try:
                await q.recover_deliveries(now=int(time.time()) + 3600)
            finally:
                q.close()

        asyncio.run(_final_recover())

        final = MailboxStore(snap).load()
        attempts = [rec["attempts"] for rec in final.values()]
        assert max(attempts) <= max_deliveries, (
            f"attempts 越过上界 {max_deliveries}: 实测 {max(attempts)};"
            f"子进程观测到的峰值 {max(peaks.values())}"
        )
        # 反复崩溃的毒消息终归 dead —— 且 **不留 pending**(pending 意味着还会再投
        # 一次,即无界)。父进程的最后一轮 recover 已排空所有 claimed。
        statuses = Counter(rec["status"] for rec in final.values())
        assert set(statuses) == {"dead"}, statuses
