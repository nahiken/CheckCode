# verify
"""验证闭环:确定性测试执行 + 失败限轮次修复 + RiskReport。

- runner.py   TestRunner:subprocess 确定性测试执行(UTF-8/超时)
- report.py   RiskReport:修改文件/测试命令/验证结果/剩余风险 + Diff
- loop.py     VerifiedLoop:Plan → worktree → Agent 修改 → 验证 → 限轮次修复 → 报告
- prepare.py  目标仓库准备(显式路径或 demo_repo 副本 + git init)
"""