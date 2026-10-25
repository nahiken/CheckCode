"""M13 集成测试:生产路径的 parallel_safe 并发。

M9 W4 把分区算法(`partition_tool_calls`)与并发水位测通了,但生产装配仍传
`max_concurrency=1` —— 并发是「休眠」能力。M13 让**只读生产装配**
(`VerifiedLoop._build_agent_loop`)从 `AGENT_MAX_CONCURRENCY` 取值(默认 4);
库默认(`AgentLoop` / `build_agent`)仍是 1。

本文件测三件事(全部走**真实 VerifiedLoop** + mock transport + demo_repo 真实文件):
1. 一轮里 3 个 `read_file`(parallel_safe)→ 并发峰值 == 3。
2. `[read, write, read, read]` → 写期间并发读 == 0(写是串行屏障),JSONL 顺序 == 模型序。
3. `AGENT_MAX_CONCURRENCY=1` → 恢复严格串行(峰值 1)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from agent.loop import AgentLoop
from hooks.audit import AuditLog
from policy.models import Plan
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from tools.registry import build_tools, tools_spec_from
from tools.runner import ToolRunner
from verify.loop import VerifiedLoop

PLAN = Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])

# demo_repo 里真实存在的文件(read_file 会成功 —— 避免用例里混入"文件不存在"的错误结果)
READ_PATHS = ["math_utils.py", "tests/test_math_utils.py", "README.md"]


def _read_round(paths: list[str], *, ids: list[str]) -> str:
    """一轮里发多个 read_file 调用。"""
    parts = []
    for i, (p, cid) in enumerate(zip(paths, ids, strict=True)):
        parts.append(
            tool_delta(i, id_=cid, name="read_file", args=json.dumps({"path": p}))
        )
    return "".join(parts) + finish("tool_calls") + usage_block()


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


def _transport(requests: list[str]) -> httpx.MockTransport:
    it = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(it))

    return httpx.MockTransport(handler)


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


class _Meter:
    """记录工具执行的并发峰值,以及「写与读是否同时活跃」。

    违规判定在**进入与退出两处**检查:只看「进入时是否有写」是单侧断言 ——
    若读在写**之前**进入(`_writing` 当时还是 False),写期间的违规永远不会被计数。
    实测(变异测试):把分区换回纯切片后,单侧断言仍然通过,双端检查才报出违规。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.active = 0
        self.peak = 0
        self.violations: list[str] = []
        self._reads = 0
        self._writes = 0
        self._writing = False
        self.reads_during_write = 0

    def _check(self, where: str, name: str) -> None:
        if self._writes and self._reads:
            self.violations.append(f"{where} {name}: writes={self._writes} reads={self._reads}")
            if where == "enter":
                self.reads_during_write += 1

    async def __call__(self, name: str, args: dict[str, Any]) -> Any:
        is_write = name != "read_file"  # 本用例里只 read_file 是并发安全
        if is_write:
            self._writes += 1
        else:
            self._reads += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self._check("enter", name)
        try:
            await asyncio.sleep(0.02)  # 让出事件循环,给并发留出可观测窗口
            return await self._inner(name, args)
        finally:
            self._check("exit", name)
            self.active -= 1
            if is_write:
                self._writes -= 1
            else:
                self._reads -= 1


def _loop_with_meter(
    demo_repo: Path, requests: list[str], meter: _Meter, **kw: Any
) -> VerifiedLoop:
    """真实 VerifiedLoop,但 dispatch 包一层计量器(记录并发水位)。"""
    return VerifiedLoop(
        _config(),
        plan=PLAN,
        approval_gate=_auto_approve,
        transport=_transport(requests),
        dispatch=meter,
        **kw,
    )


# build_tools 在 demo_repo 上构造真实工具表(供计量器内层调用)
def _real_dispatch(demo_repo: Path) -> Any:
    runner = ToolRunner(
        demo_repo,
        mode="normal",
        plan=PLAN,
        approval_gate=_auto_approve,
        audit=AuditLog(demo_repo / ".whycode" / "audit.jsonl"),
    )
    return runner.dispatch


# === 1. 三个只读调用并发(峰值 3) ===

async def test_readonly_batch_runs_concurrently(demo_repo: Path) -> None:
    """一轮 3 个 read_file → 并发峰值 == 3(生产默认 AGENT_MAX_CONCURRENCY=4)。"""
    requests = [
        _read_round(READ_PATHS, ids=["c1", "c2", "c3"]),
        _text_round(),
        _text_round(),  # 验证通过后收尾
    ]
    meter = _Meter(_real_dispatch(demo_repo))
    lp = _loop_with_meter(demo_repo, requests, meter)
    await lp.run(demo_repo, "读三个文件")
    assert meter.peak == 3, f"期望 3 个只读调用并发,实际峰值 {meter.peak}"


# === 2. 写是串行屏障 ===

async def test_write_is_serial_barrier(demo_repo: Path) -> None:
    """[read, write, read, read]:写期间并发读 == 0;JSONL 顺序 == 模型序。"""
    req = (
        tool_delta(0, id_="r1", name="read_file", args=json.dumps({"path": "math_utils.py"}))
        + tool_delta(1, id_="w1", name="write_file",
                     args=json.dumps({"path": "notes.txt", "content": "hi\n"}))
        + tool_delta(2, id_="r2", name="read_file", args=json.dumps({"path": "notes.txt"}))
        + tool_delta(3, id_="r3", name="read_file", args=json.dumps({"path": "math_utils.py"}))
        + finish("tool_calls")
        + usage_block()
    )
    requests = [req, _text_round(), _text_round()]
    meter = _Meter(_real_dispatch(demo_repo))
    lp = _loop_with_meter(demo_repo, requests, meter)
    report = await lp.run(demo_repo, "读-写-读-读")

    assert meter.violations == [], f"写与读并发共存: {meter.violations}"
    assert meter.reads_during_write == 0
    # session JSONL 里 tool_call 记录顺序 == 模型调用顺序
    sess = demo_repo / ".git" / "whycode-sessions" / f"{report.branch}.jsonl"
    ids = [
        json.loads(ln)["tool_call_id"]
        for ln in sess.read_text(encoding="utf-8").splitlines()
        if ln and json.loads(ln).get("type") == "tool_call"
    ]
    assert ids == ["r1", "w1", "r2", "r3"]


# === 3. AGENT_MAX_CONCURRENCY=1 → 严格串行 ===

async def test_env_one_restores_serial(demo_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_MAX_CONCURRENCY=1 → 恢复严格串行(峰值 1)。"""
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "1")
    requests = [_read_round(READ_PATHS, ids=["c1", "c2", "c3"]), _text_round(), _text_round()]
    meter = _Meter(_real_dispatch(demo_repo))
    lp = _loop_with_meter(demo_repo, requests, meter)
    await lp.run(demo_repo, "读三个文件")
    assert meter.peak == 1, f"AGENT_MAX_CONCURRENCY=1 应串行,实际峰值 {meter.peak}"


async def test_env_invalid_falls_back_to_default(
    demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非法 AGENT_MAX_CONCURRENCY 值 → 回退默认 4(不崩)。"""
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "abc")
    requests = [_read_round(READ_PATHS, ids=["c1", "c2", "c3"]), _text_round(), _text_round()]
    meter = _Meter(_real_dispatch(demo_repo))
    lp = _loop_with_meter(demo_repo, requests, meter)
    await lp.run(demo_repo, "读三个文件")
    assert meter.peak == 3


# === 4. 库默认仍是 1(生产装配与库默认的分界) ===

async def test_library_default_remains_serial(demo_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直接构造 AgentLoop(库默认 max_concurrency=1)→ 串行,不受 env 影响。

    这是 M13 的兼容纪律:只有**生产装配**(VerifiedLoop._build_agent_loop)读 env,
    库默认不变 —— 既有直接构造 AgentLoop 的测试与调用方行为不回归。
    """
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "8")
    meter = _Meter(_real_dispatch(demo_repo))
    tools = build_tools(demo_repo)
    from agent.client import StreamingClient

    loop = AgentLoop(
        StreamingClient(_config(), transport=_transport([_read_round(READ_PATHS, ids=["c1", "c2", "c3"]), _text_round()])),
        tools_spec_from(tools),
        meter,
        system_prompt="x",
        max_steps=10,
        max_concurrency=1,  # 库默认
    )
    await loop.run("读三个文件")
    assert meter.peak == 1


# === 5. env 解析单测 ===

def test_env_max_concurrency_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from verify.loop import _env_max_concurrency

    monkeypatch.delenv("AGENT_MAX_CONCURRENCY", raising=False)
    assert _env_max_concurrency() == 4  # 默认
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "")
    assert _env_max_concurrency() == 4  # 空串 = 未设
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "6")
    assert _env_max_concurrency() == 6
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "0")
    assert _env_max_concurrency() == 1  # clamp >= 1
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "-3")
    assert _env_max_concurrency() == 1
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "nope")
    assert _env_max_concurrency() == 4  # 非法 → 默认


def test_max_concurrency_has_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """上界 clamp:超大值不能把默认 executor 打满(否则队列尾部的工具会「没跑就超时」)。"""
    from agent.config import MAX_CONCURRENCY_CEILING, max_concurrency

    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "999999999999999999999")
    assert max_concurrency() == MAX_CONCURRENCY_CEILING
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "33")
    assert max_concurrency() == MAX_CONCURRENCY_CEILING
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "8")
    assert max_concurrency() == 8  # 上界之内原样


# === 6. Planner 也读同一并发上限(审计中-1 回归) ===

async def test_planner_uses_shared_concurrency(
    demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planner 的多文件只读扫描必须同样享受并发(与 VerifiedLoop 同源)。

    审计发现:初版只改了 VerifiedLoop,而 Planner(`policy/planner.py`)直接
    `build_agent(...)` 不带 max_concurrency → Web/CLI 任务的**第一步规划**仍串行,
    与「单一接线点」的说法不符。
    """
    monkeypatch.setenv("AGENT_MAX_CONCURRENCY", "7")
    captured: dict[str, Any] = {}

    import policy.planner as planner_mod
    from agent.loop import build_agent as real_build_agent
    from policy.planner import build_plan

    def spy(config: Any, *a: Any, **kw: Any) -> Any:
        captured.update(kw)
        return real_build_agent(config, *a, **kw)

    monkeypatch.setattr(planner_mod, "build_agent", spy)
    # 计划文本解析失败无妨 —— 我们只关心 loop 的构造参数
    with contextlib.suppress(ValueError):
        await build_plan(
            _config(),
            demo_repo,
            "规划任务",
            transport=_transport([_text_round("plan: file: math_utils.py")]),
        )
    assert captured.get("max_concurrency") == 7
