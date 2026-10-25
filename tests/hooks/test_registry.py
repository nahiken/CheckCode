"""M11 单元测试:HookRegistry 分发语义。

覆盖:注册顺序、sync/async 混用、before 抛错中断并上抛、after 抛错吞掉且不阻断后续 Hook、
事件快照(before 现场不被后续改写覆盖)、default_registry 复刻 AuditLog 两段式。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hooks.audit import AuditLog
from hooks.base import ToolAuditEvent
from hooks.registry import HookRegistry, default_registry


def _event() -> ToolAuditEvent:
    return ToolAuditEvent(tool_name="read_file", args={"path": "a.py"})


# === 注册与顺序 ===

async def test_hooks_run_in_registration_order_sync_and_async() -> None:
    """同步与异步 Hook 混用,按注册顺序串行触发。"""
    order: list[str] = []

    def sync_hook(event: ToolAuditEvent) -> None:
        order.append("sync")

    async def async_hook(event: ToolAuditEvent) -> None:
        order.append("async")

    reg = HookRegistry()
    reg.register("before", sync_hook)
    reg.register("before", async_hook)
    await reg.run("before", _event())

    assert order == ["sync", "async"]


async def test_kinds_are_isolated() -> None:
    """before / after 各自分桶,互不触发。"""
    seen: list[str] = []

    def before_hook(event: ToolAuditEvent) -> None:
        seen.append("before")

    def after_hook(event: ToolAuditEvent) -> None:
        seen.append("after")

    reg = HookRegistry()
    reg.register("before", before_hook)
    reg.register("after", after_hook)

    await reg.run("before", _event())
    assert seen == ["before"]
    await reg.run("after", _event())
    assert seen == ["before", "after"]


async def test_empty_registry_is_noop() -> None:
    reg = HookRegistry()
    await reg.run("before", _event())  # 不抛即通过
    assert reg.hooks_for("after") == ()


# === 事件快照 ===

async def test_hooks_receive_snapshot_not_live_event() -> None:
    """关键正确性:ToolRunner 会就地改写同一事件对象,granted 会被后续写入。

    若传引用,before 现场会在 after 阶段被覆盖——存下引用的 Hook 读到错值。
    此处断言字段被隔离,下一组断言可变字段(args)也被隔离。
    """
    captured: list[ToolAuditEvent] = []

    def capture(event: ToolAuditEvent) -> None:
        captured.append(event)

    reg = HookRegistry()
    reg.register("before", capture)

    event = _event()
    event.granted = True  # before 阶段:已放行、结果未定
    await reg.run("before", event)

    # 模拟 dispatch 收尾:就地改写同一个事件对象(after 阶段)
    event.duration_ms = 12.3
    event.is_error = False
    event.result = "content"

    assert captured[0] is not event
    assert captured[0].granted is True
    assert captured[0].duration_ms is None  # 快照冻结了 before 现场
    assert captured[0].is_error is None
    assert captured[0].result == ""


async def test_snapshot_isolates_mutable_args() -> None:
    """浅拷贝挡得住字段重绑,挡不住 `args` 就地改写——必须深拷贝。

    ToolAuditEvent.args 是 dict;传浅拷贝(引用)时,扩展 Hook 一个
    `event.args[...] = ...` 就能反向污染审计记录里落盘的参数。
    """
    def evil(event: ToolAuditEvent) -> None:
        event.args["path"] = "HACKED"

    reg = HookRegistry()
    reg.register("before", evil)

    event = _event()  # args={"path": "a.py"}
    await reg.run("before", event)

    assert event.args == {"path": "a.py"}  # live 事件未被 Hook 污染
    assert event.model_dump()["args"] == {"path": "a.py"}


# === 异常语义 ===

async def test_before_hook_error_propagates_and_stops_chain() -> None:
    """before 抛错:中断后续 Hook 并上抛(由 ToolRunner 转拒绝)。"""
    order: list[str] = []

    def boom(event: ToolAuditEvent) -> None:
        order.append("boom")
        raise RuntimeError("hook broken")

    def never(event: ToolAuditEvent) -> None:
        order.append("never")

    reg = HookRegistry()
    reg.register("before", boom)
    reg.register("before", never)

    with pytest.raises(RuntimeError, match="hook broken"):
        await reg.run("before", _event())
    assert order == ["boom"]  # 链被中断


async def test_after_hook_error_swallowed_and_chain_continues() -> None:
    """after 抛错:吞掉(工具已执行,结果必须回灌),后续 Hook 照常触发。"""
    order: list[str] = []

    def boom(event: ToolAuditEvent) -> None:
        order.append("boom")
        raise RuntimeError("after broken")

    def after_boom(event: ToolAuditEvent) -> None:
        order.append("next")

    reg = HookRegistry()
    reg.register("after", boom)
    reg.register("after", after_boom)

    await reg.run("after", _event())  # 不抛
    assert order == ["boom", "next"]


# === default_registry ===

async def test_default_registry_wires_audit_two_phase(tmp_path: Path) -> None:
    """默认注册表 = AuditLog.before/after:granted 触发两条(现场+结果)。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    reg = default_registry(audit)
    assert reg.hooks_for("before") == (audit.before,)
    assert reg.hooks_for("after") == (audit.after,)

    event = _event()
    event.granted = True
    await reg.run("before", event)
    event.duration_ms = 3.0
    event.is_error = False
    event.result = "ok"
    await reg.run("after", event)

    records = audit.records()
    assert len(records) == 2
    assert records[0].duration_ms is None  # before 现场
    assert records[1].duration_ms == 3.0  # after 结果
    assert records[1].result == "ok"
