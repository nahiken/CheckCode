# demo_repo — 供 Agent 修改的样例仓库

被 `mewcode` 当作目标仓库,用于里程碑演示与评测(可被反复修改、Worktree 隔离、回滚)。

## 内容

- `math_utils.py` — 一个简单计算模块(含一个已知缺陷,供修复类任务使用)
- `tests/test_math_utils.py` — 对应测试
- `README.md`

## 注意

- 此目录本身是独立 git 仓库,不是 whycode 主仓库的一部分。
- 修改任务在 **独立 Worktree** 中进行,测试命令:
  ```bash
  python -m pytest tests/ -q
  ```
