"""M11 集成测试:ToolRunner × HookRegistry。

覆盖:before/after 的触发时机(放行与被拒**两路径都触发 before**)、注册表变更与默认
注册表审计输出一致、before 抛错 → 拒绝且工具未执行且判定层记 hook、after 抛错 →
结果照常回灌。

⚠️ 触发时机语义在「两段式审计真实化」后变更过:此前被拒路径**只有 after**;现在
before 上移到权限判定之后、审批/执行之前,故被拒路径是 before+after。理由:
设计文档声称的是「执行前后统一触发」,而只覆盖成功路径的 before 与之不符,且被拒
调用缺少「即将调用」现场会让审计无法复盘「当时打算调什么、为什么被拒」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hooks.audit import AuditLog
from hooks.base import ToolAuditEvent
from hooks.registry import default_registry
from tools.runner import ToolRunner


def _runner(tmp_path: Path, **kwargs: Any) -> ToolRunner:
    return ToolRunner(tmp_path, audit=AuditLog(tmp_path / "audit.jsonl"), **kwargs)


# === 触发时机 ===

async def test_granted_call_fires_both_hooks(tmp_path: Path) -> None:
    seen: list[str] = []

    def before(event: ToolAuditEvent) -> None:
        seen.append(f"before:{event.granted}")

    def after(event: ToolAuditEvent) -> None:
        seen.append(f"after:{event.duration_ms is not None}")
    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("before", before)
    reg.register("after", after)

    r = _runner(tmp_path, hooks=reg)
    result = await r.dispatch("read_file", {"path": "missing.py"})

    assert not result.is_error or "不存在" in result.content
    assert seen == ["before:True", "after:True"]


async def test_rejected_call_fires_both_hooks(tmp_path: Path) -> None:
    """被拒调用:before 记「即将调用」意图,after 记最终结论(两段都在)。

    语义变更(原为「仅 after」):before 移到权限判定之后触发后,被拒调用同样有
    「打算调什么、权限怎么判的」现场 —— 这正是 `granted` 字段存在的意义:
    before 时 `granted` 仍为 False(此时还没放行),after 时才带上最终判定。
    """
    seen: list[str] = []

    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("before", lambda event: seen.append(f"before:{event.granted}"))
    reg.register("after", lambda event: seen.append(f"after:{event.granted}"))

    r = _runner(tmp_path, hooks=reg)
    await r.dispatch("run_command", {"command": "rm -rf /"})  # 黑名单 deny

    # before 时尚未放行(granted=False);after 时判定已定(仍 False,因为被拒)
    assert seen == ["before:False", "after:False"]


async def test_custom_registry_records_match_default_registry(tmp_path: Path) -> None:
    """接线等价守卫:自定义注册表(仍含 AuditLog)与默认注册表产出相同记录。

    这只证明「两条接线路径等价」——两侧走同一份被测代码,任何**共享路径**的输出
    退化都同样作用于两侧,结构上不可能被这条测试发现。真正的「逐字节」守卫是
    下面的 test_audit_record_is_byte_stable(冻结时钟 + 全字段黄金值)。
    """
    default = _runner(tmp_path / "a")
    custom = _runner(
        tmp_path / "b",
        hooks=default_registry(AuditLog(tmp_path / "b" / "audit.jsonl")),
    )

    for r in (default, custom):
        await r.dispatch("read_file", {"path": "missing.py"})  # granted:before+after
        await r.dispatch("run_command", {"command": "rm -rf /"})  # denied:before+after

    d, c = default.audit.records(), custom.audit.records()
    assert [e.tool_name for e in d] == [e.tool_name for e in c]
    assert [e.granted for e in d] == [e.granted for e in c]
    assert [e.executed for e in d] == [e.executed for e in c]
    assert [e.duration_ms is None for e in d] == [e.duration_ms is None for e in c]


# === 黄金测试:审计记录形态逐字冻结(M11 兼容纪律) ===

# 形态黄金值:钉死**完整字段集合与取值**(非投影)。冻结 duration_ms 后,
# 任何对 ToolAuditEvent 字段/默认值/序列化参数的改动都会让本测试变红。
_GOLDEN_BEFORE = {
    "tool_name": "read_file",
    "args": {"path": "missing.py"},
    "mode": "normal",
    "decision": "permit",
    "layer": "mode",
    "reason": "只读工具放行",
    "asked": False,
    "approved": False,
    "granted": True,
    "executed": False,  # before 现场:权限已放行,但还没执行
    "duration_ms": None,
    "is_error": None,
    "result": "",
}
_GOLDEN_AFTER = {
    **_GOLDEN_BEFORE,
    "executed": True,  # 执行路径置位的权威判据
    "duration_ms": 0.0,  # 冻结时钟
    "is_error": True,
    "result": "文件不存在: missing.py",
}


async def test_audit_record_is_byte_stable(tmp_path: Path, monkeypatch: Any) -> None:
    """默认注册表下,granted 路径的审计记录 = before 现场 + after 结果(黄金值)。

    与 M11 前的输出逐字一致。用冻结时钟消除 duration_ms 抖动,断言**全字段**
    而非投影——这是「审计输出逐字节不变」的可回归守卫(审计探针的固化)。
    """
    import tools.runner as runner_mod

    class _FrozenTime:
        @staticmethod
        def monotonic() -> float:
            return 0.0

    monkeypatch.setattr(runner_mod, "time", _FrozenTime)

    r = _runner(tmp_path)
    await r.dispatch("read_file", {"path": "missing.py"})

    records = [e.model_dump() for e in r.audit.records()]
    assert records == [_GOLDEN_BEFORE, _GOLDEN_AFTER]


# === 异常语义 ===

async def test_before_hook_error_rejects_and_skips_execution(tmp_path: Path) -> None:
    """before Hook 抛错 → 调用被拒(不执行工具),审计层记 hook。

    默认注册表里 AuditLog.before 排在自定义 Hook 之前,故现场记录**已落盘**
    (granted=True 表示「权限已放行」,非「工具已执行」——与 timeout/执行异常路径同义),
    紧随其后的 after 记录以 layer=hook 表明真正的归宿。
    """
    def boom(event: ToolAuditEvent) -> None:
        raise RuntimeError("拦截")

    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("before", boom)

    r = _runner(tmp_path, hooks=reg)
    result = await r.dispatch("read_file", {"path": "a.py"})

    assert result.is_error
    assert "before Hook" in result.content
    records = r.audit.records()
    assert len(records) == 2
    assert records[0].is_error is None  # before 现场:结果未定
    assert records[1].layer == "hook"  # after:被 Hook 拒绝
    assert records[1].is_error is True
    assert records[1].granted is True  # 权限放行了,倒在 Hook 上


async def test_before_hook_error_does_not_run_write_tool(tmp_path: Path) -> None:
    """before Hook 拒绝时写工具**零落盘**(与审批拒绝同等强度)。"""
    def boom(event: ToolAuditEvent) -> None:
        raise RuntimeError("拦截")

    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("before", boom)

    r = _runner(tmp_path, hooks=reg)
    # 先经审批放行到「即将执行」,再由 before Hook 拦下
    async def gate(name: str, args: dict[str, Any], decision: Any) -> bool:
        return True

    r.approval_gate = gate
    result = await r.dispatch("write_file", {"path": "a.py", "content": "x"})

    assert result.is_error
    assert not (tmp_path / "a.py").exists()


async def test_after_hook_error_returns_tool_result(tmp_path: Path) -> None:
    """after Hook 抛错不影响结果回灌:工具已执行,结果必须返回。"""
    def boom(event: ToolAuditEvent) -> None:
        raise RuntimeError("after 坏了")

    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("after", boom)

    r = _runner(tmp_path, hooks=reg)
    result = await r.dispatch("read_file", {"path": "missing.py"})

    assert "不存在" in result.content  # 工具结果照常返回
    assert len(r.audit.records()) == 2  # AuditLog(默认 Hook)照常落盘


async def test_after_hook_error_on_rejected_call_returns_result(tmp_path: Path) -> None:
    """被拒路径(granted=False)上 after Hook 抛错同样只吞不抛。

    这是最危险的盲区:_reject → _finish 也触发 after,若此处漏吞,一次
    「黑名单拒绝」会变成未捕获异常。断言结果照常回灌且审计保留拒绝记录。
    """
    def boom(event: ToolAuditEvent) -> None:
        raise RuntimeError("after 坏了")

    reg = default_registry(AuditLog(tmp_path / "audit.jsonl"))
    reg.register("after", boom)

    r = _runner(tmp_path, hooks=reg)
    result = await r.dispatch("run_command", {"command": "rm -rf /"})

    assert result.is_error
    assert "黑名单" in result.content
    records = r.audit.records()
    assert len(records) == 2  # 被拒路径:before(判定现场)+ after(结论)
    assert records[0].layer == "blacklist"
    assert records[0].executed is False
    assert records[1].layer == "blacklist"
    assert records[1].granted is False
    assert records[1].executed is False


async def test_default_registry_is_bound_to_runner_audit(tmp_path: Path) -> None:
    """不传 hooks → self.hooks 绑定到 self.audit(二者同一个 AuditLog)。"""
    r = _runner(tmp_path)
    assert r.hooks.hooks_for("before") == (r.audit.before,)
    assert r.hooks.hooks_for("after") == (r.audit.after,)
