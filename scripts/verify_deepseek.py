"""真实 DeepSeek 端到端验证脚本(非测试,手动运行)。

用法:
    uv run python scripts/verify_deepseek.py [--task "任务"] [--strict] [--auto-approve]

使用 M2 正式工具集(ToolRunner + 分层权限 + Hook 审计):
- normal 模式:只读放行;越权写 → Ask,由 ApprovalGate 决定(交互确认 / --auto-approve 全放行)
- --strict 模式:只读 Planner 生成计划,写必须命中计划白名单 + 人工审批

演示:Agent 读取 demo_repo 的 math_utils.py,修复 divide 缺陷,写回。
验证:流式解析、正式工具集、权限判定、审计落盘、真实 API 往返。

注意:需要 .env 里配置好 LLM_API_KEY / DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import get_llm_config, max_concurrency  # noqa: E402
from agent.loop import StepEvent, build_agent  # noqa: E402
from policy.models import PermissionDecision, Plan  # noqa: E402
from policy.planner import build_plan  # noqa: E402
from tools.runner import ToolRunner  # noqa: E402

SYSTEM = """你是 WhyCode 的安全编码助手。
- 修改文件前必须先 read_file 查看当前内容。
- 只做被明确要求的修改,不要额外重构。
- 修改后给出简要总结。"""


def _make_approval_gate(
    auto_approve: bool,
) -> Callable[[str, dict[str, Any], PermissionDecision], Awaitable[bool]]:
    """构造审批器:--auto-approve 全放行;否则交互输入 y/n(默认拒绝)。"""
    async def gate(name: str, args: dict[str, Any], decision: PermissionDecision) -> bool:
        if auto_approve:
            return True
        prompt = (
            f"\n[审批] 工具={name} 判定={decision.decision}({decision.layer})\n"
            f"  理由: {decision.reason}\n"
            f"  参数: {args}\n"
            f"  批准执行? [y/N] "
        )
        answer = input(prompt).strip().lower()
        return answer in {"y", "yes"}

    return gate


async def _print_event(ev: StepEvent) -> None:
    kind = ev.kind
    if kind == "model_call":
        print(f"\n[{ev.data['step']}] 模型思考中...")
    elif kind == "tool_exec_start":
        print(f"  → 执行 {ev.data['count']} 个工具调用")
    elif kind == "tool_result":
        print(f"  ✓ 工具结果(错误={ev.data['is_error']})")
    elif kind == "invalid_params":
        print(f"  ⚠ 非法参数({ev.data['tool']}): {ev.data['error']}")
    elif kind == "finish":
        print(f"\n=== 完成 ===\n{ev.data['content']}")
    elif kind == "max_steps":
        print("\n⚠ 达到 max_steps 上限")


async def main() -> None:
    # Windows 控制台默认 GBK,强制 UTF-8 以支持 ✓/⚠/→ 等字符
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default=None, help="任务描述(默认:演示 demo_repo 修复)")
    parser.add_argument("--strict", action="store_true", help="严格模式(计划白名单 + 审批)")
    parser.add_argument("--auto-approve", action="store_true", help="审批自动放行(非交互)")
    args = parser.parse_args()

    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)

    # 复制 demo_repo 到临时目录,避免污染模板
    src = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "demo_repo"
    workdir = Path(tempfile.mkdtemp()) / "demo_repo"
    shutil.copytree(src, workdir)
    task = args.task or (
        "请阅读 math_utils.py 并修复其中的缺陷:"
        "1) divide(1,0) 应抛 ValueError 而非 ZeroDivisionError;"
        "2) fib(-5) 应返回 -1 而非负数。修改后请运行 python -m pytest 确认测试期望。"
    )
    print(f"工作目录: {workdir}\n任务: {task}")

    # 严格模式:先用只读 Planner 生成计划(文件 + 命令白名单),再审批执行。
    # normal 模式:无计划,越权写走 Ask 交互确认。
    plan: Plan | None = None
    if args.strict:
        print("\n[严格模式] 只读 Planner 生成计划...")
        plan = await build_plan(config, workdir, task)
        print(f"  计划: files={plan.files}, commands={plan.commands}")
        print(f"  描述: {plan.description[:80]}")

    runner = ToolRunner(
        workdir,
        mode="strict" if args.strict else "normal",
        plan=plan,
        tool_timeout=config.tool_timeout,
        approval_gate=_make_approval_gate(args.auto_approve),
    )

    loop = build_agent(
        config,
        runner.tools_spec,
        runner.dispatch,
        system_prompt=SYSTEM,
        # M13:与生产同源取并发上限(诊断脚本也享受只读批并发;写仍是串行屏障)
        max_concurrency=max_concurrency(),
    )
    messages = await loop.run(task, events=_print_event)
    print(f"\n消息轮数: {len(messages)}")
    audit_path = workdir / ".whycode" / "audit.jsonl"
    print(f"审计日志: {audit_path}(存在={audit_path.exists()})")


if __name__ == "__main__":
    asyncio.run(main())
