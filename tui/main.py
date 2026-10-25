"""tui/main.py —— `mewcode-tui` 入口。

**惰性导入 Textual**:未安装 `tui` extra 的环境应得到一句可操作的提示,而不是
`ModuleNotFoundError` 回溯。Textual + Rich 首次导入约 0.5–1s,CLI/API/Eval 路径
不该为它付这笔成本(与 `assembly/build.py` 对 mcp SDK 的处理同一条纪律)。
"""

from __future__ import annotations

import argparse
import io
import sys
from typing import cast


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mewcode-tui", description="WhyCode 终端工作台(Textual)"
    )
    parser.add_argument("--repo", default=None, help="目标仓库路径(默认复制 demo_repo)")
    parser.add_argument(
        "--auto-approve", action="store_true", help="审批自动放行(默认需人工确认)"
    )
    parser.add_argument("--strict", action="store_true", help="默认勾选严格模式")
    parser.add_argument("--test-command", default=None, help="验证命令(默认取计划或 pytest)")
    parser.add_argument("--max-fix-rounds", type=int, default=None, help="失败修复轮次上限")
    return parser


def main(argv: list[str] | None = None) -> None:
    # Windows 控制台默认 GBK → 中文会乱码(与 cli/main.py 同款处理)
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")

    args = _build_parser().parse_args(argv)

    try:
        from tui.app import WorkbenchApp
    except ImportError as exc:  # textual 未安装
        print(
            "需要安装 TUI 依赖:\n"
            "    uv sync --extra tui\n"
            "  (或 uv pip install -e '.[tui]')",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    WorkbenchApp(
        repo=args.repo,
        strict=args.strict,
        auto_approve=args.auto_approve,
        test_command=args.test_command,
        max_fix_rounds=args.max_fix_rounds,
    ).run()


if __name__ == "__main__":
    main()
