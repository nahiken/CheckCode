"""真实运行 smoke:真实 Provider 跑 VerifiedLoop + resume 不重放 + 回滚(M9 W8)。

⚠ 仅当 WHYCODE_SMOKE_REAL=1 且已配置真实 API key 时才执行;CI 永不触发(见下方守卫)。

验证面(镜像 verify_loop_deepseek.py 惯例,补 M9 resume/回滚):
1. 真实 Provider(默认 DeepSeek)跑一个 demo_repo 修复任务(auto-approve)
   → 断言 report.passed;分支上留下 Agent 改动。
2. resume 验证不重放:再次以同 session_id 跑 → 复用分支、不重复执行已完成副作用;
   pre-verify 通过即零模型调用(不重放已提交写操作)。
3. 回滚验证:git branch -f 回到基线 → 分支 diff 清空,主工作树无残留。

用法:
    WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_real.py [--task "任务"] [--repo 路径]

退出码:0 = 全部冒烟通过;1 = 任一断言失败(打印失败点)。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

# 守卫:未显式开启真实运行 → 直接拒绝(CI/误触都不执行)
if os.environ.get("WHYCODE_SMOKE_REAL") != "1":
    print("跳过:真实运行 smoke 需显式 WHYCODE_SMOKE_REAL=1(需真实 API key,CI 不跑)。")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import LLMConfig, get_llm_config  # noqa: E402
from cli.main import _make_approval_gate  # noqa: E402
from verify.loop import VerifiedLoop  # noqa: E402
from verify.prepare import prepare_repo  # noqa: E402
from verify.report import RiskReport  # noqa: E402
from worktree.manager import WorktreeManager  # noqa: E402

_DEFAULT_TASK = (
    "请阅读 math_utils.py 并修复其中的缺陷:"
    "1) divide(1,0) 应抛 ValueError 而非 ZeroDivisionError;"
    "2) fib(-5) 应返回 -1 而非负数。修改后请确保测试通过。"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


async def _run_once(config: LLMConfig, repo: Path, task: str, session_id: str) -> RiskReport:
    """跑一轮 VerifiedLoop;返回 RiskReport。"""
    loop = VerifiedLoop(
        config,
        approval_gate=_make_approval_gate(auto_approve=True),
        session_id=session_id,
    )
    return await loop.run(repo, task)


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default=None, help="任务描述(默认:修复 demo_repo 缺陷)")
    parser.add_argument("--repo", default=None, help="目标仓库路径(默认复制 demo_repo 到临时目录)")
    args = parser.parse_args()

    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)

    repo = prepare_repo(args.repo)
    task = args.task or _DEFAULT_TASK
    print(f"目标仓库: {repo}\n任务: {task}")
    base = WorktreeManager(repo).base_commit()
    print(f"基线提交: {base}")

    # ── 第 1 阶段:真实 run(自动放行)→ 应通过且有改动 ─────────────────
    print("\n[1/3] 真实 run(auto-approve)...")
    report1 = await _run_once(config, repo, task, session_id="smoke-real")
    if not report1.passed:
        print(f"✗ 首轮未通过: {report1.remaining_risk}")
        sys.exit(1)
    print(f"  ✓ passed; rounds={report1.rounds_used}; changed={report1.changed_files}")
    if not report1.changed_files:
        print("✗ 预期有文件改动,实际为空(真实 Agent 未写?)")
        sys.exit(1)
    branch1 = report1.branch
    print(f"  ✓ 分支={branch1}(保留,resume 锚点)")

    # ── 第 2 阶段:resume(同 session)→ 复用分支,不重放副作用 ──────────
    print("\n[2/3] resume(同 session,验证不重放)...")
    # 记分支改动数(resume 后应不变:已完成写不重放)
    files_before = sorted(_git(repo, "diff", "--name-only", base, branch1).splitlines())
    report2 = await _run_once(config, repo, task, session_id="smoke-real")
    if not report2.passed:
        print(f"✗ resume 未通过: {report2.remaining_risk}")
        sys.exit(1)
    files_after = sorted(_git(repo, "diff", "--name-only", base, report2.branch).splitlines())
    if files_after != files_before:
        print(f"✗ resume 重放副作用:改动文件集变化 {files_before} → {files_after}")
        sys.exit(1)
    print(f"  ✓ resume passed;改动文件集未变化({len(files_after)} 个,不重放)")

    # ── 第 3 阶段:回滚 → 分支回到基线,diff 清空,主工作树无残留 ───────
    print("\n[3/3] 回滚(branch -f 回基线)...")
    mgr = WorktreeManager(repo)
    mgr.force_branch(branch1, base)
    after = sorted(_git(repo, "diff", "--name-only", base, branch1).splitlines())
    if after:
        print(f"✗ 回滚后仍有改动: {after}")
        sys.exit(1)
    # 主工作树干净(worktree 已拆除、改动只移动分支指针)
    status = _git(repo, "status", "--short").strip()
    if status:
        print(f"✗ 主工作树有残留:\n{status}")
        sys.exit(1)
    print("  ✓ 回滚干净(分支回基线,主工作树无残留)")

    print("\n✓ smoke_real 全部通过(真实 Provider run → resume 不重放 → 回滚)。")


if __name__ == "__main__":
    asyncio.run(main())
