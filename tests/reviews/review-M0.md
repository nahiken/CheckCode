# M0 代码审查记录

> 审查方式:新开子会话对 M0 提交 d5b2810 执行 /code-review(实证运行 + 源码阅读)。
> 状态:全部已修复并复测。记录问题、成因、解决,供 M1 及以后参考。

## P0 — 已修复

### 1. test_smoke.py worktree 测试失败时泄漏,破坏幂等(已实证)
- **问题**:`test_demo_repo_initializable` 无 try/finally。若 `git worktree add` 后断言/命令抛错,worktree 目录与 `feat/smoke` 分支残留;下次 `git worktree add -b feat/smoke` 因分支已存在 exit 255 → 永久失败。
- **成因**:创建与清理之间无异常安全边界;清理严格依赖前置成功。
- **解决**:测试全部改为在 `tmp_path` 副本上创建,conftest 提供 `worktree` fixture 用 yield + best-effort 清理;唯一分支名避免并行冲突。

## P1 — 已修复

### 2. 在原位源码树初始化 git 仓库,污染 fixture 目录
- **问题**:`init_demo_repo()` 直接向被 git 跟踪的 `tests/fixtures/demo_repo/` 写入 `.git`(实测生成),fixture 不再是纯净模板。
- **成因**:fixture 初始化发生在被跟踪的源码目录内,而非临时目录。
- **解决**:模板只读、入库不含 `.git`;conftest 用 `shutil.copytree` 复制到 `tmp_path` 再 init。已清理残留 `.git`/`__pycache__`。

### 3. Windows 下 CLI 中文乱码(实测)
- **问题**:`.venv/Scripts/mewcode.exe --help` 等在管道/终端下输出 GBK 字节被 UTF-8 终端解码为乱码。
- **成因**:Python 3.11 Windows 下管道输出走 ANSI 代码页(cp936)。
- **解决**:`main()` 入口对 stdout/stderr `reconfigure(encoding="utf-8", errors="replace")`。

### 4. demo_repo 自带测试有 CWD 依赖
- **问题**:从仓库根 `pytest tests/fixtures/demo_repo/tests/` 收集失败(`ModuleNotFoundError: math_utils`);仅在 demo_repo 目录内运行得 2 failed/4 passed(恰为两个已知缺陷)。
- **成因**:测试 `from math_utils import ...` 依赖 cwd 在 demo_repo 内。
- **解决(记录,供 M1 verify 使用)**:M1 的 verify 步骤必须在 worktree 内执行,否则会把「无法收集」误判为真实失败。demo_repo README 已注明测试命令 `cd` 到仓库根。

## P2 — 已修复

### 5. init_demo_repo 并发不安全(TOCTOU)
- **问题**:`if not (.git).exists()` 检查与 init/commit 非原子,并行 worker 下二次 commit 报 "nothing to commit"。
- **成因**:幂等判断与实际初始化之间无互斥。
- **解决**:改为 tmp_path 副本模式,无共享状态,竞态根除。

### 6. argparse 结构:--task 是顶层参数,resume/eval 静默接受
- **问题**:`--task` 对 resume/eval 无意义却被接受;用位置参数模拟子命令。
- **解决**:改用 `add_subparsers`;`--task` 仅属于 `run` 子命令(required=True)。

### 7. 清理命令吞错误 + worktree 路径未忽略
- **问题**:`check=True` 失败不带 stderr,排错靠猜;失败泄漏时 `tests/fixtures/demo_worktree` 以 `??` 出现在主仓库 status。
- **解决**:conftest 清理用 best-effort(`check=False`)不抛错;tmp_path 模式天然无残留,不再需要 .gitignore 特判。

## 记录(无需修复)

- `packages.find` 与 `mewcode` 入口:实证正确,无需改。
- `include` 预声明未来包:冗余但无害,随里程碑落地自然补齐。
- demo_repo 已知缺陷与文档一致(预期 2 红),README 已注明是故意设计。
- `git -C <path>` 若路径以 `-` 开头会被误当选项;当前为绝对路径,风险极低。
- `docs/00-environment.md` 演示命令 `echo change > ...` 在 Windows 会写入尾随空格,与测试的 `write_text("change")` 略有出入,属文档小瑕疵。

## 复测结论

- `uv run pytest`:33 passed(3 冒烟 + 30 新增),全绿。
- `uv run ruff check .`:通过。
- `uv run mypy cli`:通过。
