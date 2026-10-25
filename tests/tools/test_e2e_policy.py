"""M2 端到端测试:ReAct Loop + ToolRunner(真实文件系统 + demo_repo)。

场景:
1. 越权写被拒:normal 模式无审批器 → write_file 命中 ask → 默认拒绝。
2. 计划白名单:write_file 命中计划 → 直接放行,文件真实写入。
3. 严格模式:写需审批,未审批 → 拒绝。
4. 非法参数(schema 缺必填)→ 错误 Observation 回灌,工具不执行。
5. 完整链路:read_file(许可)→ write_file(计划许可)→ 读回 → 最终回答。
6. 审计落盘与 loop 事件联动。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from agent.config import LLMConfig
from agent.loop import AgentLoop, StepEvent, build_agent
from agent.types import AgentResult
from hooks.audit import AuditLog
from policy.models import Plan, Rule
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from tools.runner import ToolRunner


def _capture_loop(
    requests: list[str],
    dispatch: Callable[[str, dict[str, Any]], Awaitable[AgentResult]],
) -> tuple[AgentLoop, list[dict[str, Any]]]:
    request_iter = iter(requests)
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text=next(request_iter))

    config = LLMConfig(base_url="https://test.example", api_key="k", model="m", max_steps=10)
    loop = build_agent(config, {}, dispatch, system_prompt="你是安全编码助手", transport=httpx.MockTransport(handler))
    return loop, captured


async def test_e2e_unauthorized_write_blocked_and_audited(demo_repo: Path) -> None:
    """normal 模式:Agent 越权写文件 → 默认拒绝,文件不变,审计落盘。"""
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(demo_repo, audit=audit)
    r1 = tool_delta(0, id_="call_w", name="write_file", args='{"path":"hacked.py","content":"evil"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("写入被拒绝,我放弃") + finish() + usage_block()

    loop, captured = _capture_loop([r1, r2], runner.dispatch)
    messages = await loop.run("请在仓库里创建一个 hacked.py 文件")

    # 工具未执行:文件不存在
    assert not (demo_repo / "hacked.py").exists()
    # 回灌的是错误 Observation
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content and ("审批" in tool_msgs[0].content or "越权" in tool_msgs[0].content)
    # 审计记录:before(判定现场)+ after(结论),ask / mode / 未执行
    records = audit.records()
    assert len(records) == 2
    assert records[0].tool_name == "write_file"
    assert records[0].decision == "ask"
    assert records[0].granted is False  # 权限未放行
    assert records[0].executed is False
    assert records[1].executed is False  # after:同样没执行(无 gate → 默认拒)
    # 发给 provider 的 messages 含 tool 错误消息(模型能看到失败原因)
    assert "tool" in {m["role"] for m in captured[1]["messages"]}


async def test_e2e_plan_permit_writes_file(demo_repo: Path) -> None:
    """计划白名单放行:Agent 修改计划内文件,真实生效,审计记录 permit。"""
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    plan = Plan(files=["math_utils.py"])
    runner = ToolRunner(demo_repo, audit=audit, plan=plan)
    r1 = tool_delta(0, id_="call_w", name="write_file", args='{"path":"math_utils.py","content":"# fixed"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("完成") + finish() + usage_block()

    loop, _ = _capture_loop([r1, r2], runner.dispatch)
    await loop.run("修改 math_utils.py")

    assert (demo_repo / "math_utils.py").read_text(encoding="utf-8") == "# fixed"
    records = audit.records()
    assert records[0].decision == "permit"
    assert records[0].layer == "plan"


async def test_e2e_strict_mode_requires_approval(demo_repo: Path) -> None:
    """严格模式:写操作命中计划仍需审批;无审批器 → 拒绝。"""
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(demo_repo, mode="strict", plan=Plan(files=["math_utils.py"]), audit=audit)
    r1 = tool_delta(0, id_="call_w", name="write_file", args='{"path":"math_utils.py","content":"# x"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("无审批,拒绝") + finish() + usage_block()

    loop, _ = _capture_loop([r1, r2], runner.dispatch)
    await loop.run("改 math_utils.py")

    assert (demo_repo / "math_utils.py").read_text(encoding="utf-8") != "# x"
    records = audit.records()
    assert records[0].mode == "strict"
    assert records[0].granted is False


async def test_e2e_rule_permit_allows_pytest(demo_repo: Path) -> None:
    """规则允许 pytest → 直接执行,测试通过。"""
    import json
    import sys

    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(
        demo_repo,
        audit=audit,
        rules=[Rule(tool="run_command", pattern="pytest", effect="permit")],
    )
    command = f"{sys.executable} -m pytest -q"
    r1 = tool_delta(0, id_="call_c", name="run_command", args=json.dumps({"command": command})) + finish("tool_calls") + usage_block()
    r2 = text_delta("测试通过") + finish() + usage_block()

    loop, _ = _capture_loop([r1, r2], runner.dispatch)
    messages = await loop.run("跑测试")
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    # demo_repo 的测试当前失败(缺陷未修)→ 内容含失败信息
    assert "passed" in tool_msgs[0].content or "failed" in tool_msgs[0].content
    assert audit.records()[1].decision == "permit"
    assert audit.records()[1].layer == "rules"


async def test_e2e_schema_missing_required_rejected(demo_repo: Path) -> None:
    """Agent 给 read_file 空对象参数 → runner 的 schema 校验拦截 → 错误 Observation,不执行。

    链路:loop 的 parse_tool_arguments 通过(JSON 合法)→ dispatch 到达 runner →
    runner 的 JSON Schema 校验发现缺必填字段 path → 拒绝。这正是 M1 review 遗留的修复。
    """
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(demo_repo, audit=audit)
    r1 = tool_delta(0, id_="call_bad", name="read_file", args="{}") + finish("tool_calls") + usage_block()
    r2 = text_delta("我补充了路径") + finish() + usage_block()

    loop, _ = _capture_loop([r1, r2], runner.dispatch)
    messages = await loop.run("读文件")
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "参数校验失败" in tool_msgs[0].content
    assert audit.records()[0].layer == "schema"


async def test_e2e_full_read_write_readback(demo_repo: Path) -> None:
    """完整链路:read(许可)→ write(计划许可)→ read 读回 → 最终回答。"""
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(demo_repo, audit=audit, plan=Plan(files=["notes.txt"]))
    r1 = tool_delta(0, id_="c1", name="read_file", args='{"path":"notes.txt"}') + finish("tool_calls") + usage_block()
    r2 = tool_delta(0, id_="c2", name="write_file", args='{"path":"notes.txt","content":"hello"}') + finish("tool_calls") + usage_block()
    r3 = tool_delta(0, id_="c3", name="read_file", args='{"path":"notes.txt"}') + finish("tool_calls") + usage_block()
    r4 = text_delta("完成") + finish() + usage_block()

    (demo_repo / "notes.txt").write_text("old", encoding="utf-8")
    loop, _ = _capture_loop([r1, r2, r3, r4], runner.dispatch)
    messages = await loop.run("读写 notes.txt")

    # 读回内容 = 写入后的新内容
    assert messages[7].content == "hello"
    assert (demo_repo / "notes.txt").read_text(encoding="utf-8") == "hello"
    # 审计:每步 before+after 两条,共 6 条;after 依次为 read/write/read 的 permit
    records = audit.records()
    assert len(records) == 6
    afters = [r for r in records if r.duration_ms is not None]
    assert [r.tool_name for r in afters] == ["read_file", "write_file", "read_file"]
    assert [r.decision for r in afters] == ["permit", "permit", "permit"]


async def test_e2e_events_and_audit_both_recorded(demo_repo: Path) -> None:
    """事件流 + 审计双通道:loop 事件与审计记录都能完整还原执行过程。"""
    audit = AuditLog(demo_repo / ".whycode" / "audit.jsonl")
    runner = ToolRunner(demo_repo, audit=audit, plan=Plan(files=["notes.txt"]))
    (demo_repo / "notes.txt").write_text("x", encoding="utf-8")
    r1 = tool_delta(0, id_="c1", name="write_file", args='{"path":"notes.txt","content":"y"}') + finish("tool_calls") + usage_block()
    r2 = text_delta("写好了") + finish() + usage_block()

    loop, _ = _capture_loop([r1, r2], runner.dispatch)
    seen: list[str] = []
    async def record(ev: StepEvent) -> None:
        seen.append(ev.kind)
    await loop.run("写文件", events=record)

    # M4 新增 assistant 事件(每轮模型输出后发出)
    assert seen == [
        "model_call", "assistant", "tool_exec_start", "tool_result",
        "model_call", "assistant", "finish",
    ]
    records = audit.records()
    assert len(records) == 2  # before + after
    assert records[1].granted is True
    assert records[1].duration_ms is not None
