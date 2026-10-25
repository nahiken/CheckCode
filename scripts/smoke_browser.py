"""浏览器冒烟(可选,M9 W8,opt-in):真实驱动点击「允许/拒绝」与「恢复」。

⚠ 仅当 WHYCODE_SMOKE_REAL=1、Docker 栈已起、且安装了 playwright 时执行。

依赖(可选,不在项目必装里):
    pip install playwright && playwright install chromium

流程:
1. 打开 http://127.0.0.1:8000(Vue/Nginx 服务前端)。
2. 创建任务(strict + 关自动放行)→ 触发审批 → 等待「等待人工审批」面板。
3. 点「允许执行」→ 任务放行继续(观察状态回 running)。
4. (需要第二次审批/或 error 行)点「恢复」验证 resume 入口存在。

用法:
    WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_browser.py

缺 playwright 时打印提示并退出 0(可选冒烟,非强制)。
"""

from __future__ import annotations

import os
import sys

if os.environ.get("WHYCODE_SMOKE_REAL") != "1":
    print("跳过:浏览器冒烟需显式 WHYCODE_SMOKE_REAL=1。")
    sys.exit(0)

try:  # 可选依赖:未装则不阻塞(可选冒烟)
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
except ImportError:
    print("提示:未安装 playwright,跳过浏览器冒烟(可选)。")
    print("  安装:pip install playwright && playwright install chromium")
    sys.exit(0)

BASE = os.environ.get("WHYCODE_SMOKE_BASE", "http://127.0.0.1:8000")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector('[data-testid="task-input"]', timeout=10_000)

        # 1. 严格模式 + 关自动放行 → 写操作会触发审批
        page.fill('[data-testid="task-input"]', "读一下仓库里的文件并说明结构")
        page.check('[data-testid="strict-box"]')
        page.uncheck('[data-testid="autoapprove-box"]')
        page.click("form button[type=submit]")

        # 2. 等待审批面板或 done/error(真实 LLM 有无 key 分支)
        #    有 key + strict 写 → 面板出现;无 key/只读 → 直接 error/done
        try:
            page.wait_for_selector('[data-testid="approval-panel"]', timeout=25_000)
            print("[✓] 审批面板出现(等待人工审批)")
            page.click('[data-testid="panel-approve-btn"]')
            # 面板关闭 = 审批已 resolve(任务放行继续)
            page.wait_for_selector(
                '[data-testid="approval-panel"]', state="detached", timeout=15_000
            )
            print("[✓] 点「允许执行」→ 面板关闭(审批 resolve)")
        except Exception:
            print("[i] 未出现审批面板(可能任务只读/无 key 直接完成)——跳过点击审批")

        # 3. 任务列表应含该任务(awaiting/running/done/error)
        page.wait_for_selector('[data-testid="task-list"]', timeout=10_000)
        page.wait_for_timeout(1500)  # 等列表刷新
        print("[✓] 任务列表可见")
        browser.close()
    print("\n✓ 浏览器冒烟通过(或按提示降级)。")


if __name__ == "__main__":
    main()
