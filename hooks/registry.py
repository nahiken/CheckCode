"""Hook 注册表:工具执行前后按注册顺序触发多个 Hook。

M11 把 M2 遗留的「两段式 Hook 实际只有一个消费者(AuditLog)」升级为真正可注册的
分发器:ToolRunner 只面向 `HookRegistry.run(kind, event)`,不再直连 `AuditLog`。
审计本身从「硬编码的调用」降级为「默认注册表里的一个 Hook」,扩展点因此开放。

语义(与工具执行流水线对齐):
- **顺序**:同一 kind 内按注册顺序**串行 await**(Hook 间可能存在因果依赖,如先落盘后通知)。
- **sync/async 混用**:返回 awaitable 则 await,否则视为同步 Hook 直接调用。
- **快照**:每个 Hook 收到 `event.model_copy(deep=True)`。ToolRunner 会就地改写同一个事件
  对象(granted / duration_ms / result),不快照的话「before 现场」会被后续写入覆盖,
  存下引用的 Hook 会读到错值。**必须深拷贝**:字段重绑(如 `event.granted = True`)浅拷贝
  就能隔离,但 `args` 这类可变字段是**共享引用**,扩展 Hook 一个 `event.args[...] = ...`
  就能反向污染审计记录——这正是本类要挡住的。
- **before 抛错** → 中断后续 Hook 并向调用方抛出,由 ToolRunner 转为拒绝(该调用不执行),
  判定层记 `hook`;对齐既有「审批器异常按拒绝处理」的策略。
- **after 抛错** → 吞掉 + 记日志:工具**已经执行或已被拒**,结果都必须回灌模型,
  不能因 Hook 失败而丢结果。

**异常范围**:只吞 `Exception`。`BaseException`(`CancelledError` / `KeyboardInterrupt`)会照常
穿透——吞掉取消信号是反模式,故不覆盖;调用方「永不冒泡」的措辞以 `Exception` 为界。

**after 与被拒路径**:`after` 由 `_finish` 统一触发,而 `_finish` 也是**被拒路径**的收尾点。
此时 `granted=False`(权限未放行),日志与 docstring 据此区分,勿假定「after ⇒ 工具已执行」。

`default_registry(audit)` 复刻 M2 的行为(唯一 Hook = AuditLog,写同样两条记录),
保证审计输出逐字节不变——这是本里程碑的兼容纪律。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from hooks.audit import AuditLog
from hooks.base import ToolAuditEvent

logger = logging.getLogger(__name__)

HookKind = Literal["before", "after"]
# 唯一带特殊语义的 kind(其余 kind 抛错一律吞掉);避免字面量在多处各写一遍。
_BEFORE: HookKind = "before"
# Hook 可为同步或异步:返回 awaitable 即 await,否则直接调用。
ToolHook = Callable[[ToolAuditEvent], Awaitable[None] | None]


class HookRegistry:
    """按 kind 分桶、按注册序触发的 Hook 分发器。"""

    def __init__(self) -> None:
        self._hooks: dict[HookKind, list[ToolHook]] = {"before": [], "after": []}

    def register(self, kind: HookKind, hook: ToolHook) -> None:
        """注册一个 Hook(注册序即触发序)。"""
        self._hooks[kind].append(hook)

    def hooks_for(self, kind: HookKind) -> tuple[ToolHook, ...]:
        """该 kind 已注册的 Hook(只读快照,便于测试断言顺序)。"""
        return tuple(self._hooks[kind])

    async def run(self, kind: HookKind, event: ToolAuditEvent) -> None:
        """依次触发该 kind 的全部 Hook(before 抛错中断抛出;其余抛错吞掉记账)。"""
        for hook in self._hooks[kind]:
            try:
                outcome = hook(event.model_copy(deep=True))
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                if kind == _BEFORE:
                    raise
                # 含被拒路径:after 也会在 granted=False 时触发,文案不可假定工具已执行。
                logger.exception(
                    "after Hook 失败(结果照常返回): tool=%s granted=%s",
                    event.tool_name,
                    event.granted,
                )


def default_registry(audit: AuditLog) -> HookRegistry:
    """默认注册表:唯一 Hook = AuditLog,复刻 M2 的两段式审计。

    granted 路径 before(现场)+ after(结果)两条记录,拒绝路径仅 after 一条——
    与 M11 之前逐字节一致。

    需要追加 Hook 时以此起手,而非另起一个空注册表(否则审计会被静默关掉):
        reg = default_registry(audit)
        reg.register("after", my_hook)

    注意 `AuditLog.before` 排在**最前**:任何拦截型 before Hook 触发时,一条
    `granted=True`(权限已放行)的现场记录已经落盘——与 timeout/执行异常路径同义,
    `granted` 表示「权限放行」而非「工具已执行」。

    同时传 `audit=` 与 `hooks=` 时,请用**同一个** AuditLog 构造 registry,否则
    `runner.audit.records()` 读不到 Hook 写进另一个对象的记录。
    """
    registry = HookRegistry()
    registry.register("before", audit.before)
    registry.register("after", audit.after)
    return registry
