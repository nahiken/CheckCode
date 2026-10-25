"""M15 审计修复的回归测试(第二轮 code review):只读会话不可 resume。

审计 C5:只读 pass(M9 W6)写 header/run/done 但**从不创建分支**。若把它当作可恢复
会话,`create_from_branch` 会去重建一个不存在的分支 → WorktreeError(实测
`fatal: invalid reference: sess-X`),会话从此永久不可恢复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from agent.config import LLMConfig
from tests.agent.helpers import finish, text_delta, tool_delta, usage_block
from verify.loop import VerifiedLoop, _is_readonly_session


def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=10)


async def _auto_approve(name: str, args: dict[str, Any], decision: Any) -> bool:
    return True


def _transport(requests: list[str]) -> httpx.MockTransport:
    it = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(it))

    return httpx.MockTransport(handler)


def _text_round(text: str = "完成") -> str:
    return text_delta(text) + finish() + usage_block()


def _plan_round() -> str:
    """Planner 输出一个非空计划(触发写阶段 → 需要 worktree)。"""
    body = "[计划]\nfile: math_utils.py\n[/计划]"
    return text_delta(body) + finish() + usage_block()


def _write_round(content: str, id_: str) -> str:
    args = json.dumps({"path": "math_utils.py", "content": content})
    return (
        tool_delta(0, id_=id_, name="write_file", args=args)
        + finish("tool_calls")
        + usage_block()
    )


def test_is_readonly_session_detects_marker(tmp_path: Path) -> None:
    """判据:存在 done phase 且 data.readonly 为真。"""
    from store.models import HeaderRecord, PhaseRecord
    from store.session import SessionStore

    store = SessionStore(tmp_path / "s.jsonl")
    store.record_header(
        HeaderRecord(session_id="s1", repo="/r", task="t", branch="s1", mode="normal")
    )
    assert _is_readonly_session(store.records()) is False
    store.record_phase(PhaseRecord(phase="done", data={"passed": True, "readonly": True}))
    assert _is_readonly_session(store.records()) is True


async def test_readonly_session_is_not_resumed(demo_repo: Path) -> None:
    """端到端:只读会话 + 后来的 resume 不得走 create_from_branch(它会失败)。

    复现修好前的路径:① 只读跑(Planner 空输出,不建分支);② 复用**同一 session_id**
    再跑一次且 Planner 这次给了计划 → 修好前会 `WorktreeError: invalid reference`。
    """
    session_id = "sess-readonly-probe"
    # ① 只读 pass:Planner 回「无计划」→ 转只读(只读 pass 自己还会再问模型一次)
    loop1 = VerifiedLoop(
        _config(),
        approval_gate=_auto_approve,
        transport=_transport([_text_round("没有计划"), _text_round("这个仓库没问题")]),
        session_id=session_id,
    )
    report1 = await loop1.run(demo_repo, "解释一下")
    assert report1.readonly is True

    # 分支确实不存在(只读 pass 不建)
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(demo_repo), "branch", "--list", session_id],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == ""

    # ② 复用同一 session:Planner 这次给出计划 → 修好前 create_from_branch 抛 WorktreeError
    loop2 = VerifiedLoop(
        _config(),
        approval_gate=_auto_approve,
        transport=_transport(
            [
                _plan_round(),  # Planner
                _write_round("def add(a, b):\n    return a + b\n", "c1"),
                _text_round(),
            ]
        ),
        session_id=session_id,
        test_command="python -c pass",
    )
    report2 = await loop2.run(demo_repo, "修复 math_utils")

    # 修好前:抛 WorktreeError;修好后:按新会话处理并正常产出
    assert loop2.last_resumed is False  # 不当作恢复
    assert report2.readonly is False
