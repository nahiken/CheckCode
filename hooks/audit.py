"""审计日志:JSONL 追加落盘 + 读回 + 执行前后 Hook。

每个工具调用触发两段 Hook:
- before(decision):权限判定后、执行前 —— 记录"即将执行"的现场(可在此二次拦截)
- after(result):执行完成后 —— 记录最终结果与耗时

两段式让审计不依赖 runner 的异常路径:任何中间拒绝(before)与成功执行(after)
都被独立记录,复盘时能区分"被拒在审批前"与"实际执行过"。

每行一条 ToolAuditEvent。JSONL 追加写入崩溃安全:只丢最后一行,不影响历史
(M4 将复用该模式做会话持久化)。
"""

from __future__ import annotations

from pathlib import Path

from hooks.base import ToolAuditEvent


class AuditLog:
    """JSONL 审计日志(兼作默认 before/after Hook)。path 为日志文件。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: ToolAuditEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    async def before(self, event: ToolAuditEvent) -> None:
        """执行前 Hook:追加一条"进入待执行/待审批"的现场记录。"""
        self.append(event)

    async def after(self, event: ToolAuditEvent) -> None:
        """执行后 Hook:追加最终结果记录(含耗时/错误/结果)。"""
        self.append(event)

    def records(self) -> list[ToolAuditEvent]:
        """读回全部审计记录(按落盘顺序)。文件不存在 → 空列表。"""
        if not self._path.exists():
            return []
        out: list[ToolAuditEvent] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(ToolAuditEvent.model_validate_json(line))
        return out
