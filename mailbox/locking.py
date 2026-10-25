"""mailbox/locking.py —— 跨进程文件锁(OS 咨询锁,stdlib only)。

为什么用 OS 咨询锁而不是锁文件(M14 设计取舍):
- 进程崩溃时,**OS 在句柄关闭时自动释放**锁 → 无陈旧锁。锁文件方案(`O_EXCL` 创建)
  在 worker 崩溃后会留下陈旧锁,需要心跳/PID 启发式「偷锁」——对一个**专门做崩溃恢复 +
  lease 重投**的队列而言,这等于反向引入一个新的崩溃风险。
- 不为单一调用点引入运行时依赖(`filelock`/`portalocker`);项目连 httpx 流式都手写。
- 平台原语各约 3 行:`fcntl.flock`(POSIX)/ `msvcrt.locking`(Windows)都是**全进程可见**的
  咨询锁,不区分线程 —— 这正是「跨进程互斥」要的语义。

⚠️ 锁的是**独立的 `.lock` 文件**,绝不锁快照文件本身:`store.save` 用 `os.replace`
原子改名会**换掉 inode**,锁在一个已被替换的 inode 上等于没锁。

⚠️ 这是**单机多进程**原语(同一台机器的文件系统语义)。不是分布式锁,不要跨机器/跨网络盘用。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any

# 两个平台模块各只在一个分支里 import:运行期只有一个是真模块。
# 未导入的那个显式标注为 Any —— mypy 在 Windows 上会把 `else` 分支判为不可达
# 且拒绝解析 `fcntl`,标注后两个平台下 strict 都能通过(实测 win32 / linux)。
if sys.platform == "win32":
    import msvcrt

    fcntl: Any = None
else:
    import fcntl

    msvcrt: Any = None

# 拿不到锁时的默认等待上限(秒):超过即抛错,绝不无限悬挂(测试/CI 不能挂死)
DEFAULT_LOCK_TIMEOUT = 30.0
# 轮询间隔(秒):40ms 量级对「秒级任务」的队列足够,又不至于空转烧 CPU
DEFAULT_LOCK_POLL = 0.01


class MailboxLockTimeout(RuntimeError):
    """在 timeout 内没有拿到文件锁。"""


def _try_lock(fh: IO[bytes]) -> bool:
    """非阻塞地尝试独占锁。返回是否拿到。"""
    if sys.platform == "win32":
        # msvcrt 的锁是「相对当前文件位置」的字节区间锁 → 必须先回到同一位置
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    """释放锁。位置必须与加锁时一致(Windows 区间锁按位置匹配)。"""
    if sys.platform == "win32":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(
    fh: IO[bytes],
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll: float = DEFAULT_LOCK_POLL,
) -> Iterator[None]:
    """在已打开的句柄上取独占锁,退出时释放(异常路径也释放)。

    两平台都用「非阻塞尝试 + 轮询」而非各自的阻塞原语(`fcntl.LOCK_EX` /
    `msvcrt.LK_LOCK`):阻塞版在 Windows 上最多重试 10 次后**抛错**而不是继续等,
    语义不一致;轮询版两平台行为统一,且能给出明确的超时错误。
    """
    deadline = time.monotonic() + timeout
    while True:
        if _try_lock(fh):
            break
        if time.monotonic() >= deadline:
            raise MailboxLockTimeout(f"未能在 {timeout}s 内取得文件锁")
        time.sleep(poll)
    try:
        yield
    finally:
        _unlock(fh)
