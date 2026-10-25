"""mailbox/store.py —— 原子快照持久化。

与 M4 会话 JSONL 事件日志的本质区别(要点):
- 事件日志:追加写,记录「发生了什么」,读回需重放 → 适合会话/审计(要历史)。
- 原子快照:os.replace 整文件替换,记录「现在是什么」,读回一次 JSON → 适合队列(要一致视图)。

崩溃安全:写临时文件 → fsync → os.replace 原子改名。读到的永远是
「旧完整快照」或「新完整快照」之一,绝不出现半写状态。

Windows 上 `os.replace` 的共享冲突(实测发现,不是理论担忧):
  POSIX 允许 rename 与 open 任意交错;Windows 则要求删除/改名目标时没有其他
  句柄打开着它。于是**一个不加锁的读者**(哪怕只是打开一瞬)就能让写者的
  `os.replace` 抛 `PermissionError: [WinError 5]`。实测:1 个写者 + 3 个无锁读者
  并发时,150 次写里失败 119 次 —— 而 store 的契约是「原子替换绝不半写」,
  在这种环境下会表现为「写直接失败」,对队列是致命的(任务提交不上)。
  **反向也成立**:写者的 replace 会让读者的 open/read 失败。实测 1 写 + 3 读,
  加写侧重试后写失败降到 17/150,但读者开始失败 15/1500 —— 冲突是**双向**的。

  对策是**两侧都退避重试**(`_replace_with_retry` / `_read_with_retry`):把
  「另一进程恰好持有一瞬句柄」这段窗口让过去。重试上限内不成功才抛错 ——
  不掩盖持续失败。读者不加互斥锁(读-读仍可并行),只重试,避免为读引入串行化。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from mailbox.locking import DEFAULT_LOCK_TIMEOUT, file_lock
from pathlib import Path
from typing import IO, Any

# os.replace 被并发打开的读者挡住时的重试预算(秒)
_REPLACE_RETRY_SECONDS = 5.0
_REPLACE_RETRY_POLL = 0.005


def _replace_with_retry(src: str, dst: Path) -> None:
    """`os.replace`,但在 Windows 上容忍「被并发读句柄短暂挡住」。

    POSIX 上第一次就成功(rename 与 open 不互斥),重试循环对它是空转零成本 ——
    因此不需要按平台分支。持续失败(如目标被永久占用/权限错误)仍会抛错。
    """
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            # Windows 专属的瞬时冲突;POSIX 上 PermissionError 是真实权限问题,
            # 不该靠重试掩盖 —— 直接抛。
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_POLL)


def _read_with_retry(path: Path) -> str:
    """读快照文本,容忍 Windows 上「写者正在 os.replace」造成的瞬时 PermissionError。

    与 `_replace_with_retry` 是一对:冲突是双向的,两侧都得让一步。
    POSIX 上 PermissionError 是真权限问题 → 直接抛,不靠重试掩盖。
    """
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_POLL)


def _fsync_dir(path: Path) -> None:
    """best-effort fsync 父目录:确保 os.replace 的改名本身持久化。

    Windows 无法以目录打开 fsync,os.open 会抛 OSError → 静默跳过。
    目标平台尽量持久化 rename,无法支持时降级为普通原子替换。
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class MailboxStore:
    """任务快照的原子读写。

    save 接收 task_id → 记录 dict 的映射;load 返回同样结构的映射。
    记录本身交给 MailboxQueue 用 TaskRecord 建模,存储层只负责原子持久化。
    """

    def __init__(self, path: Path, *, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.path = Path(path)
        # 锁文件与快照**同目录但不同文件**:快照靠 os.replace 换 inode,锁不能跟着换
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_timeout = lock_timeout
        self._lock_fh: IO[bytes] | None = None
        self._lock_depth = 0  # >0 = 本实例正持有锁

    # ---- 跨进程互斥 ----

    @property
    def locked(self) -> bool:
        """是否已经打开过锁句柄(仅供测试断言/诊断)。"""
        return self._lock_fh is not None

    @contextmanager
    def lock(self) -> Iterator[None]:
        """取跨进程独占锁的上下文管理器(延迟打开句柄 + 退出即释放)。

        句柄懒打开并**常驻**:同一 `MailboxStore` 反复取锁复用同一 fd,避免
        每次变更都 open/close。OS 在进程退出(含崩溃)时关闭句柄 → 锁自动释放。

        **不可重入**:OS 咨询锁不认识「同一进程」,同实例嵌套取锁会一直等自己,
        直到 `lock_timeout` 才报超时 —— 一个「为什么偶尔卡 30 秒」的谜题。
        这里显式记深度并在嵌套时**立刻**抛错,把沉默的等待变成可见的编程错误。
        """
        if self._lock_depth:
            raise RuntimeError(
                "MailboxStore.lock() 不可重入:同一实例已在临界区内(嵌套取锁会自死锁)"
            )
        with file_lock(self._ensure_handle(), timeout=self._lock_timeout):
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1

    def _ensure_handle(self) -> IO[bytes]:
        if self._lock_fh is None:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock_fh = open(self.lock_path, "a+b")  # noqa: SIM115 - 常驻句柄,close() 显式释放
        return self._lock_fh

    def close(self) -> None:
        """关闭锁句柄(进程退出前调用;不调用也由 OS 兜底释放)。"""
        if self._lock_fh is not None:
            self._lock_fh.close()
            self._lock_fh = None

    def save(self, records: dict[str, dict[str, Any]]) -> None:
        """原子写快照。先写同目录临时文件,再 os.replace 原子替换。

        并发读者可能让 `os.replace` 在 Windows 上瞬时失败 → `_replace_with_retry`。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp_name, self.path)
            _fsync_dir(self.path.parent)
        except BaseException:
            # 临时文件残留清理(正常路径 os.replace 后已无)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def load(self) -> dict[str, dict[str, Any]]:
        """读回完整快照。文件不存在 → 空 dict。损坏 → 抛 ValueError(不静默)。"""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(_read_with_retry(self.path))
        except json.JSONDecodeError as exc:
            raise ValueError(f"mailbox snapshot corrupted: {self.path}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"mailbox snapshot not a dict: {self.path}")
        out: dict[str, dict[str, Any]] = {}
        for key, val in raw.items():
            if not isinstance(val, dict):
                raise ValueError(f"mailbox snapshot record malformed: {key}")
            out[key] = val
        return out
