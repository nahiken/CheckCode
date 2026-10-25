"""M15 集成测试:VerifiedLoop 透传 allowedTools 白名单到闭环内 ToolRunner。

承重断言:白名单外的工具在闭环内被拒 → 错误 Observation 回灌给模型 →
闭环继续(不崩),且**磁盘上没有任何未授权写入**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.config import LLMConfig
from policy.models import Plan
from store.models import ToolCallRecord
from store.session import SessionStore
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop


@pytest.fixture()
def plan_math_utils() -> Plan:
    return Plan(files=["math_utils.py"], commands=["python -m pytest tests/ -q"])


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


def _make_transport(requests: list[str]) -> httpx.MockTransport:
    request_iter = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(request_iter))

    return httpx.MockTransport(handler)


def _write_round(content: str, id_: str) -> str:
    args = json.dumps({"path": "math_utils.py", "content": content})
    return (
        tool_delta(0, id_=id_, name="write_file", args=args)
        + finish("tool_calls")
        + usage_block()
    )


def _read_round(path: str, id_: str) -> str:
    return (
        tool_delta(0, id_=id_, name="read_file", args=json.dumps({"path": path}))
        + finish("tool_calls")
        + usage_block()
    )


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


def _session(demo_repo: Path, branch: str) -> SessionStore:
    from worktree.manager import WorktreeManager

    return SessionStore(WorktreeManager(demo_repo).session_path(branch))


def _capture_agent_tools(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """抓 `build_agent` 收到的 tools_spec(模型**看得见**哪些工具)。

    `verify/loop.py` 在模块级 `from agent.loop import build_agent`,故补丁打在 verify.loop。
    """
    import verify.loop as loop_mod

    real = loop_mod.build_agent
    seen: list[dict[str, Any]] = []

    def spy(config: Any, tools_spec: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(dict(tools_spec))
        return real(config, tools_spec, *args, **kwargs)

    monkeypatch.setattr(loop_mod, "build_agent", spy)
    return seen


async def test_write_file_call_rejected_by_skill_layer(
    demo_repo: Path, plan_math_utils: Plan
) -> None:
    """白名单只给 read_file → 模型发起的 write_file 被 **skill 层**拒,文件未被改。

    这是「白名单真的在写入路径上生效」的承重断言:layer 精确等于 skill
    (若层序写错,会变成 plan/mode/sandbox,断言立刻变红)。
    """
    requests = [
        _write_round("def add(a, b):\n    return a + b\n", "call_w"),
        _text_round(),
    ]
    loop = VerifiedLoop(
        _config(),
        plan=plan_math_utils,
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        test_command="python -c pass",
        allowed_local_tools={"read_file"},
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    records = _session(demo_repo, report.branch).records()
    call = next(r for r in records if isinstance(r, ToolCallRecord))
    assert call.tool_name == "write_file"
    assert call.layer == "skill"
    assert call.decision == "deny"
    assert call.granted is False
    assert report.changed_files == []  # 拒绝发生在执行之前:没有任何写入落盘


async def test_denied_tool_observations_are_recorded(
    demo_repo: Path, plan_math_utils: Plan
) -> None:
    """白名单只给写工具 → 模型发起的 read_file 被拒;错误 Observation 落盘且模型仍走完。

    说明:用非严格模式 + 计划命中,写工具**无需审批**即执行(隔离掉审批因素),
    这样白名单是唯一的拒绝来源 —— 断言才真的指向 skill 层。
    """
    requests = [
        _read_round("README.md", "call_r"),
        _write_round("def add(a, b):\n    return a + b\n", "call_w"),
        _text_round(),
    ]
    loop = VerifiedLoop(
        _config(),
        plan=plan_math_utils,
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        test_command="python -c pass",
        allowed_local_tools={"write_file"},
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    records = _session(demo_repo, report.branch).records()
    calls = [r for r in records if isinstance(r, ToolCallRecord)]
    by_name = {r.tool_name: r for r in calls}
    assert by_name["read_file"].layer == "skill"
    assert by_name["read_file"].granted is False
    assert by_name["read_file"].is_error is True
    # 白名单内的写工具照常放行(未命中计划外路径 → permit)
    assert by_name["write_file"].granted is True
    assert by_name["write_file"].layer != "skill"


async def test_allowed_tool_still_effective_in_loop(
    demo_repo: Path, plan_math_utils: Plan
) -> None:
    """白名单含 write_file → 闭环内的写入照常生效(收窄不等于瘫痪)。"""
    requests = [_write_round("def add(a, b):\n    return a + b\n", "call_w"), _text_round()]
    loop = VerifiedLoop(
        _config(),
        plan=plan_math_utils,
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        test_command="python -c pass",
        allowed_local_tools={"write_file"},
    )

    report = await loop.run(demo_repo, "修复 math_utils")

    assert report.changed_files == ["math_utils.py"]
    assert "return a + b" in report.diff


async def test_denied_tools_remain_visible_to_model(
    demo_repo: Path, plan_math_utils: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**有意设计**:白名单外的工具仍留在模型的 tools_spec 里(看得见、调不动)。

    理由:留给模型的信息是「你可以试」→ 撞上 deny → 错误 Observation 回灌 → 自己换路。
    若从 schema 里摘掉,模型只会一直猜自己为什么做不到(黑盒),而白名单是
    **动态、每任务不同**的收窄,不是 Agent 的静态能力边界。

    这条测试把该决定钉成**行为契约**:一旦有人「顺手」把 tools_spec 也按白名单过滤,
    它会变红,迫使这里重新讨论,而不是悄悄改语义。
    """
    seen = _capture_agent_tools(monkeypatch)
    requests = [_read_round("README.md", "call_r"), _text_round()]
    loop = VerifiedLoop(
        _config(),
        plan=plan_math_utils,
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        test_command="python -c pass",
        allowed_local_tools={"read_file"},
    )

    await loop.run(demo_repo, "读一下判断能不能改")

    assert seen, "build_agent 未被调用"
    spec_names = set(seen[0])
    assert "read_file" in spec_names  # 白名单内
    assert "write_file" in spec_names  # 白名单外 —— 仍在 schema 里
    assert "run_command" in spec_names


async def test_readonly_pass_also_honours_allowlist(demo_repo: Path) -> None:
    """只读 pass(M9 W6)同样受白名单约束 —— **行为**断言:list_dir 真被 skill 层拒。

    触发方式:不注入计划 + 只读 Planner 返回空计划 → 闭环转入只读 pass。
    ⚠️ 只断言「runner 拿到了白名单」是不够的(那只测接线);这里断言工具**真的被拒**
    (审计 M15 测试缺口:仅接线断言属「测了症状没测规格」)。
    """
    # Planner 的模型回复:无 [计划] 标记 → parse_plan 抛 ValueError → 只读 pass
    planner_reply = text_delta("这个文件没什么问题") + finish() + usage_block()
    # 只读 pass 里模型尝试 list_dir(白名单只给了 read_file → 应被 skill 层拒)
    readonly_round = (
        tool_delta(0, id_="call_l", name="list_dir", args=json.dumps({"path": "."}))
        + finish("tool_calls")
        + usage_block()
    )
    requests = [planner_reply, readonly_round, _text_round()]

    loop = VerifiedLoop(
        _config(),
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        allowed_local_tools={"read_file"},
    )
    report = await loop.run(demo_repo, "解释一下这个仓库")

    assert report.readonly is True
    records = _session(demo_repo, report.branch).records()
    call = next(r for r in records if isinstance(r, ToolCallRecord))
    assert call.tool_name == "list_dir"
    assert call.layer == "skill"  # 不是 plan(只读 pass 的 strict 空计划也想拒它)
    assert call.granted is False


async def test_readonly_pass_runner_receives_allowlist(
    demo_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接线断言(与上面的行为断言互补):只读 pass 的 runner 确实拿到了白名单。"""
    planner_reply = text_delta("这个文件没什么问题") + finish() + usage_block()
    requests = [planner_reply, _text_round()]

    captured: list[Any] = []
    import verify.loop as loop_mod

    real = loop_mod.ToolRunner

    def factory(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("tools_allow"))
        return real(*args, **kwargs)

    monkeypatch.setattr(loop_mod, "ToolRunner", factory)

    loop = VerifiedLoop(
        _config(),
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        allowed_local_tools={"read_file"},
    )
    report = await loop.run(demo_repo, "解释一下这个仓库")

    assert report.readonly is True
    assert {"read_file"} in captured


async def test_none_leaves_loop_unchanged(demo_repo: Path, plan_math_utils: Plan) -> None:
    """allowed_local_tools=None → 闭环行为与不传该参数一致(兼容纪律)。"""
    requests = [_read_round("README.md", "call_r"), _text_round()]
    loop = VerifiedLoop(
        _config(),
        plan=plan_math_utils,
        approval_gate=_auto_approve,
        transport=_make_transport(requests),
        test_command="python -c pass",
        allowed_local_tools=None,
    )

    report = await loop.run(demo_repo, "读一下 README")

    records = _session(demo_repo, report.branch).records()
    read = next(r for r in records if isinstance(r, ToolCallRecord) and r.tool_name == "read_file")
    assert read.granted is True  # 无限制 → 只读照常放行
