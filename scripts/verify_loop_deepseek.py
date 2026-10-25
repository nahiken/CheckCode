"""真实 DeepSeek 端到端验证脚本:M3 Verified Coding Loop(非测试,手动运行)。

用法:
    uv run python scripts/verify_loop_deepseek.py [--task "任务"] [--strict] [--auto-approve]

演示:复制 demo_repo → VerifiedLoop(worktree 隔离)→ Agent 在 worktree 内修复
→ 真实 pytest 验证 → RiskReport(diff/修改文件/验证结果/剩余风险)→ 清理。

验证:worktree 隔离、验证闭环、Diff/风险报告、审计落盘、真实 API 往返。

注意:需要 .env 里配置好 LLM_API_KEY / DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import get_llm_config  # noqa: E402
from cli.main import _make_approval_gate  # noqa: E402
from verify.loop import VerifiedLoop  # noqa: E402
from verify.prepare import prepare_repo  # noqa: E402
from worktree.manager import WorktreeManager  # noqa: E402


async def main() -> None:
    # Windows 控制台默认 GBK,强制 UTF-8 以支持 ✓/⚠/→ 等字符
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default=None, help="任务描述(默认:修复 demo_repo 缺陷)")
    parser.add_argument(
        "--strict", action="store_true", help="严格模式(只读 Planner 生成计划 + 审批)"
    )
    parser.add_argument("--auto-approve", action="store_true", help="审批自动放行(非交互)")
    parser.add_argument("--repo", default=None, help="目标仓库路径(默认复制 demo_repo 到临时目录)")
    parser.add_argument(
        "--session", default=None, help="恢复会话 ID(复用既有分支,不重放已完成副作用)"
    )
    args = parser.parse_args()

    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)

    if args.session and not args.repo:
        print("错误:恢复会话(--session)必须同时指定 --repo(会话记录在仓库内)。")
        sys.exit(1)

    # 复制 demo_repo 到临时目录,避免污染模板(与 CLI 共用准备逻辑)
    repo = prepare_repo(args.repo)
    task = args.task or (
        "请阅读 math_utils.py 并修复其中的缺陷:"
        "1) divide(1,0) 应抛 ValueError 而非 ZeroDivisionError;"
        "2) fib(-5) 应返回 -1 而非负数。"
        "修改后请确保测试通过。"
    )
    if args.session:
        print(f"恢复会话: {args.session}\n目标仓库: {repo}")
    else:
        print(f"目标仓库: {repo}\n任务: {task}")

    # 严格模式:先由只读 Planner 生成计划,再进闭环
    plan = None
    if args.strict and not args.session:
        from policy.planner import build_plan

        print("\n[严格模式] 只读 Planner 生成计划...")
        plan = await build_plan(config, repo, task)
        print(f"  计划: files={plan.files}, commands={plan.commands}")

    loop = VerifiedLoop(
        config,
        plan=plan,
        strict=args.strict,
        approval_gate=_make_approval_gate(args.auto_approve),
        session_id=args.session,
    )
    report = await loop.run(repo, task)
    print("\n" + report.render())
    # 审计落盘在 worktree 之外(单一来源:WorktreeManager.audit_path)
    audit_path = WorktreeManager(repo).audit_path(report.branch)
    session_path = WorktreeManager(repo).session_path(report.branch)
    print(f"\n审计日志: {audit_path}")
    print(f"会话记录: {session_path}")
    print(f"恢复: uv run python scripts/verify_loop_deepseek.py --session {report.branch}")


if __name__ == "__main__":
    asyncio.run(main())
