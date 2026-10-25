"""M14 单元测试:跨进程文件锁(mailbox/locking.py + MailboxStore.lock)。

分两层验:
- **本进程内**的语义:互斥、超时抛错、异常路径释放、锁的是 `.lock` 而非快照本身。
- **真跨进程**的语义:两个进程同时抢同一把锁,后者必须等(或超时)——
  同进程内「加了锁」不能证明文件锁有效,必须真起进程。
"""

from __future__ import annotations

import multiprocessing
import time
from mailbox.locking import MailboxLockTimeout, file_lock
from mailbox.store import MailboxStore
from pathlib import Path

import pytest

from tests.mailbox._mp import hold_lock

# Windows 必须 spawn(fork 不存在);显式取 context 而不是依赖默认
CTX = multiprocessing.get_context("spawn")


@pytest.fixture()
def store(tmp_path: Path) -> MailboxStore:
    s = MailboxStore(tmp_path / "mailbox.json")
    yield s
    s.close()


class TestFileLockInProcess:
    def test_lock_is_exclusive_within_process(self, tmp_path: Path) -> None:
        """同进程内第二次取锁必须等第一次释放 —— 锁必须是排他的。

        这条会**挂死**如果锁不排他(第二次 `with` 直接进去,不会超时),故用
        timeout 把「不排他」变成「抛 MailboxLockTimeout」这个可断言的结果。
        """
        store = MailboxStore(tmp_path / "m.json")
        handle = store._ensure_handle()
        try:
            with store.lock(), pytest.raises(MailboxLockTimeout):
                # 嵌套是刻意的:外层持锁,内层必须**拿不到**(不排他则不会抛)
                file_lock(handle, timeout=0.2).__enter__()
        finally:
            store.close()

    def test_timeout_is_reported(self, tmp_path: Path) -> None:
        """拿不到锁 → 抛 MailboxLockTimeout(绝不无限悬挂)。"""
        store = MailboxStore(tmp_path / "m.json")
        handle = store._ensure_handle()
        try:
            with store.lock(), pytest.raises(MailboxLockTimeout):
                file_lock(handle, timeout=0.1).__enter__()
        finally:
            store.close()

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        """临界区内抛异常 → 锁仍被释放(否则一次失败会永久卡死队列)。"""
        store = MailboxStore(tmp_path / "m.json")
        try:
            with pytest.raises(RuntimeError), store.lock():
                raise RuntimeError("boom")
            # 能再次取到 → 证明上一步异常路径也释放了
            with store.lock():
                pass
        finally:
            store.close()

    def test_lock_file_is_separate_from_snapshot(self, tmp_path: Path) -> None:
        """锁的是独立的 `.lock` 文件:快照被 os.replace 换 inode 也不影响锁。"""
        store = MailboxStore(tmp_path / "m.json")
        try:
            with store.lock():
                store.save({"t-1": {"task_id": "t-1"}})  # 换掉快照 inode
            assert store.lock_path.name == "m.json.lock"
            assert store.lock_path != store.path
        finally:
            store.close()

    def test_handle_is_reused(self, tmp_path: Path) -> None:
        """锁句柄常驻复用(不每次 open/close);close() 后归还。"""
        store = MailboxStore(tmp_path / "m.json")
        store.close()
        assert not store.locked
        with store.lock():
            assert store.locked
        store.close()
        assert not store.locked

    def test_nested_lock_fails_fast(self, tmp_path: Path) -> None:
        """同实例嵌套取锁 → **立刻** RuntimeError(不是等满 30s 才超时)。

        审计轻-5:OS 咨询锁不认识「同一进程」,嵌套会一直等自己 → 表现为
        「偶尔卡 30 秒」。显式记深度把沉默的等待变成可见的编程错误。
        """
        store = MailboxStore(tmp_path / "m.json", lock_timeout=30.0)
        try:
            t0 = time.monotonic()
            with store.lock(), pytest.raises(RuntimeError, match="不可重入"):
                store.lock().__enter__()  # 嵌套是刻意的:必须立刻报错而非等 30s
            assert time.monotonic() - t0 < 1.0  # 远小于 lock_timeout
        finally:
            store.close()

    def test_lock_reusable_after_nested_error(self, tmp_path: Path) -> None:
        """嵌套报错后本实例仍可用(深度计数没有泄漏)。"""
        store = MailboxStore(tmp_path / "m.json")
        try:
            with store.lock(), pytest.raises(RuntimeError):
                store.lock().__enter__()
            with store.lock():  # 深度已归零 → 正常
                pass
        finally:
            store.close()


class TestLockAcrossProcesses:
    def test_second_process_blocks_until_release(self, tmp_path: Path) -> None:
        """真跨进程:子进程持锁 0.8s 期间,父进程取锁必须失败(超时)。"""
        snap = tmp_path / "mailbox.json"
        started = CTX.Event()
        results = CTX.Queue()
        proc = CTX.Process(target=hold_lock, args=(str(snap), 0.8, started, results))
        proc.start()
        try:
            assert started.wait(timeout=30), "子进程未在 30s 内取得锁"

            store = MailboxStore(snap)
            handle = store._ensure_handle()
            try:
                t0 = time.monotonic()
                with pytest.raises(MailboxLockTimeout):
                    file_lock(handle, timeout=0.2).__enter__()
                # 确实等满了 timeout(而不是立刻返回)—— 证明是被**另一个进程**挡住
                assert time.monotonic() - t0 >= 0.2
            finally:
                store.close()

            # 子进程释放后,父进程应能取到锁(锁不是「一次拿走后永久失效」)
            proc.join(timeout=30)
            assert results.get(timeout=10) == "relocked"
            store2 = MailboxStore(snap)
            try:
                with store2.lock():
                    pass
            finally:
                store2.close()
        finally:
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
