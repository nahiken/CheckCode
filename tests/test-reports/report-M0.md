# WhyCode M0 里程碑 — 测试设计与评估报告

- 日期: 2026-08-31
- 范围: M0 里程碑(uv 项目骨架 / CLI 入口 `mewcode` / 冒烟测试 / demo_repo fixture)
- 环境: Windows 11 · Python 3.11 · uv 0.7.12 · pytest(dev 依赖)

---

## 一、覆盖层次与用例清单

按「单元 → 集成 → 端到端」分层组织,新增 4 个测试文件(共 30 个新用例,加上既有冒烟 3 个,合计 **33** 个)。

### 单元测试(Unit)

| 文件 | 用例 | 覆盖点 |
|------|------|--------|
| `tests/test_config.py`(8) | `test_pyproject_parseable` | pyproject.toml 可用 tomllib 解析,`[project]` 段存在 |
| | `test_pyproject_metadata_fields` | name/version/requires-python/description/readme |
| | `test_pyproject_runtime_dependencies` | pydantic、httpx 运行时依赖 |
| | `test_pyproject_dev_dependencies` | pytest/pytest-asyncio/pytest-cov/ruff/mypy |
| | `test_pyproject_console_script` | 入口 `mewcode = cli.main:main` |
| | `test_pyproject_pytest_testpaths` | testpaths=tests;demo_repo 被 norecursedirs 排除 |
| | `test_pyproject_mypy_strict` | mypy strict=true |
| | `test_pyproject_ruff_config` | ruff select 规则 E/F/I/UP/B/SIM |

### 集成测试(Integration)

| 文件 | 用例 | 覆盖点 |
|------|------|--------|
| `tests/test_cli.py`(10) | `test_cli_module_importable` | `cli.main` 可导入且暴露 `main` |
| | `test_cli_version` | `--version` 输出 `whycode 0.1.0`,退出码 0 |
| | `test_cli_help` | `--help` 列出 run/resume/eval |
| | `test_cli_no_command_prints_help` | 无子命令打印 usage,退出码 0 |
| | `test_cli_run_without_task_errors` | `run` 缺 `--task` 报错,退出码 **2** |
| | `test_cli_run_with_task_ok` | `run --task` 正常,输出任务文本,退出码 0 |
| | `test_cli_unknown_command_exit_code` | 未知子命令 `invalid choice`,退出码 **2** |
| | `test_cli_resume_available` | `resume` 可用,退出码 0 |
| | `test_cli_eval_available` | `eval` 可用,退出码 0 |
| | `test_cli_task_accepted_by_resume_observed` | 观察:`--task` 为顶层参数,resume 也接受(记录现状) |
| `tests/test_demo_repo.py`(8) | `test_init_demo_repo_idempotent` | fixture 幂等:连续调用不报错 |
| | `test_demo_repo_is_git_repo_after_init` | 初始化后为合法 git 仓库 |
| | `test_demo_repo_expected_files_present` | math_utils.py / 自带 tests / README 存在 |
| | `test_math_utils_add_subtract_ok` | add/subtract 正常行为 |
| | `test_math_utils_divide_normal_ok` | divide 正常除法 |
| | `test_math_utils_divide_by_zero_current_bug` | **缺陷记录**:除零当前抛 ZeroDivisionError(目标:ValueError) |
| | `test_math_utils_fib_normal_ok` | fib(10)==55 正常行为 |
| | `test_math_utils_fib_negative_current_bug` | **缺陷记录**:fib(-5) 当前返回 -5(目标:-1) |

### 端到端测试(E2E)

| 文件 | 用例 | 覆盖点 |
|------|------|--------|
| `tests/test_worktree.py`(4) | `test_worktree_add_list_remove_lifecycle` | worktree add→出现在 list→remove→从 list 消失 |
| | `test_worktree_isolation_untracked_file` | worktree 内新增未跟踪文件不污染主仓库 status |
| | `test_worktree_commit_not_seen_by_main` | worktree 内 commit 不影响主仓库 HEAD |
| | `test_worktree_two_parallel_worktrees` | 同一仓库同时存在多个独立 worktree |
| `tests/test_smoke.py`(3, 既有) | `test_cli_help` | CLI 可运行并打印 help |
| | `test_demo_repo_initializable` | demo_repo 可初始化 + Worktree 隔离 |
| | `test_packages_importable` | 核心空包可导入 |

---

## 二、测试运行结果

| 检查项 | 命令 | 结果 |
|--------|------|------|
| 单元+集成+端到端 | `uv run pytest` | **33 passed**,约 6–7 s |
| Lint | `uv run ruff check .` | **All checks passed!** |
| 类型 | `uv run mypy cli` | **Success: no issues found in 2 source files** |

- pytest 分文件:test_smoke 3 · test_cli 10 · test_config 8 · test_demo_repo 8 · test_worktree 4 = 33。
- 最慢用例:worktree commit 隔离(1.25 s)、双 worktree(1.05 s)——均为真实 git 操作,属合理开销。
- 运行结束后主仓库与 demo_repo 均干净(无残留 worktree / 分支),清理逻辑生效。

### 编写过程中的问题(已修复,均为测试自身问题,非功能 bug)

1. Windows 下 `git worktree list` 输出为 `/` 分隔路径,而 `str(Path)` 为 `\` 分隔,导致 2 个用例路径匹配失败 → 增加 `_worktree_is_listed()` 规范化比较后修复。
2. ruff 报告 5 项:2 处 import 排序(I001)、1 处未用导入(F401)、2 处 `zip()` 缺少 `strict`(B905)→ 已全部修复。

### demo_repo 自带测试(独立仓库,不纳入主套件)

在 demo_repo 目录内按 README 约定运行:

```
2 failed, 4 passed
```

- 失败 1: `test_divide_by_zero_raises_value_error` → 实际抛 `ZeroDivisionError`
- 失败 2: `test_fib_negative_returns_minus_one` → `assert -5 == -1`

这 2 个失败即为 M0 故意遗留、供 M1 Agent 修复的缺陷,与主套件 `test_demo_repo.py` 中记录的现状一致。

---

## 三、关键结论与风险

### 关键结论

- **M0 骨架稳定可用**: CLI 子命令路由与退出码、pyproject 配置完整性、`git worktree` 生命周期与隔离性均验证通过;ruff / mypy 零告警。
- **缺陷状态已固化为测试基线**: math_utils 的 `divide`(除零应抛 ValueError)与 `fib`(负数应返回 -1)以「现状断言」记录,修复后需同步更新 `tests/test_demo_repo.py` 中对应用例与 demo_repo 自带测试。
- **Worktree 隔离机制可靠**: 未跟踪文件、独立 commit、并行多 worktree 三种场景均验证主仓库不被污染,为 M1「Worktree 隔离修改」奠定验证基础。

### 风险与建议(发现的问题仅记录,未改动交付代码)

1. **demo_repo 自带测试存在 CWD 依赖**: 从仓库根目录直接运行 `pytest tests/fixtures/demo_repo/tests/` 会收集失败(`ModuleNotFoundError: No module named 'math_utils'`),只有按 demo_repo/README 在 demo_repo 目录内运行才正常。M1 的 verify 步骤必须显式在 worktree 内执行测试命令,否则会把「无法收集」误判为真实失败。
2. **demo_repo 文件被主仓库跟踪 + 运行时独立 git 仓库的张力**: demo_repo 的源文件已纳入 whycode 主仓库 git 跟踪,同时其 `.git` 为运行时初始化且未跟踪。若 Agent 在 M1 修改 worktree 中的 demo_repo 文件并提交,主仓库会把这些文件显示为已修改,造成嵌套仓库语义冲突。建议 M1 前明确 demo_repo 的托管方式(如从主仓库排除、以独立 repo 方式维护)。
3. **`--task` 为顶层参数**: CLI 对 run/resume/eval 都接受 `--task`,仅在 run 分支强制校验。若语义应为「仅 run 专属」,M1 应改为 argparse subparser;现状已用 `test_cli_task_accepted_by_resume_observed` 记录。
4. **`test_smoke.py::test_demo_repo_initializable` 清理脆弱**: 使用固定路径 `demo_worktree`,中途失败不清理会导致重跑失败。建议 M1 改为临时路径(本次未改动既有测试)。
5. **Windows 中文编码**: CLI 中文错误消息经管道捕获为 GBK 编码,跨平台断言只能依赖 ASCII 子串(本套件已采用)。M1 若要精确断言中文消息,需统一 UTF-8 输出或在测试侧按平台处理。
6. **已知缺陷(非失败,属设计遗留)**: 除 `divide`/`fib` 外,`divide` 返回 float 而参数注释为 `float`,行为无异常,暂不处理。

---

*本报告由 M0 测试设计阶段产出;新增测试文件:`tests/test_cli.py`、`tests/test_config.py`、`tests/test_demo_repo.py`、`tests/test_worktree.py`。*
