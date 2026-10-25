# M3 Review 记录(双轴 code-review)

> 审查范围:`git diff 2030e22...b217238`(M3 核心提交)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 修复提交:`a92a60e`(mode 参数化 / 审计持久化 / _git 去重 / 风险推导重构)
> 修复后验证:**200 tests passed**,ruff/mypy(strict,59 文件)全绿,真实 DeepSeek normal+strict 双重验证

## 二轮 Review(M3 review-fix 范围 `b217238...HEAD`,修复提交)

> 双轴复审 review 修复提交(`a92a60e` + `8c7d170`)。Spec 轴逐一核对了声称修复的 7 项(全部正确落地)。

### Standards 轴(4 项)

| # | 气味(严重度) | 位置 | 成因 | 修复 |
|---|--------------|------|------|------|
| S1' | Duplicated Code(med) | `verify/loop.py`、`cli/main.py`、`scripts/verify_loop_deepseek.py` 三处手搭审计路径 `repo/.git/whycode-audits/<branch>.jsonl` | 审计路径知识散落 | `WorktreeManager.audit_path(branch)` 单一来源,三处改调 |
| S2' | Duplicated Code(med) | `verify/loop.py` + `verify/report.py` 末轮投影 `rounds[-1]`/`passed` 重复 3 处 | 投影逻辑复制 | 抽 `last_result(rounds)` helper;`RiskReport.passed` 改 computed property |
| S3' | Divergent Change(低-med) | `cli/main.py` 的 `_prepare_repo` 含 print/sys.exit 副作用且被 script import | CLI 模块因 repo 准备被编辑 | 迁到 `verify/prepare.py`,CLI 只 import |
| S4' | Repeated Switch(低) | CLI/script 各写一遍 `mode="strict" if strict else "normal"` | mode 映射重复 | `VerifiedLoop` 加 `strict: bool` 折叠映射 |

### Spec 轴(1 项,低严重度)

| # | 类型 | 位置 | 描述 |
|---|------|------|------|
| S5' | wrong(minor) | `docs/03:115` | 声称「全项目 198」,实际 200 → 改 200 |

### 二轮验证

- 7 项 review 修复经 Spec 轴逐一验证正确(P1 normal 模式语义恢复、P2 审计外置持久化、S1-S6 全部落地),无遗漏/无回归/无范围蔓延。
- 修复后 **201 tests passed**(新增 `test_report_passed_projected_from_rounds`),ruff/mypy(strict,60 文件)全绿,真实 DeepSeek normal 端到端通过。

## 一轮 Standards 轴发现(7 项)

| # | 气味(严重度) | 位置 | 成因 | 修复 |
|---|--------------|------|------|------|
| S1 | Duplicated Code(high) | `cli/main.py` `_prepare_repo` 与 `scripts/verify_loop_deepseek.py` 复制 demo_repo 块完全相同 | 两处各自实现,未共用 | 脚本改 import `_prepare_repo`,删本地实现 |
| S2 | Duplicated Code(med-high) | `worktree/manager.py` `_git` 与 `_git_in_wt` 仅 cwd 不同 | 分两个方法 | 统一 `_git(cwd, *args)`,删 `_git_in_wt` |
| S3 | Data Clump(med) | `derive_remaining_risk(passed, rounds_used, last, plan_files, changed_files, max_fix_rounds)` 6 参数 | 前三个都是 rounds 的投影 | 改收 `rounds` + `plan` + `changed_files` + `max_fix_rounds`,内部投影 |
| S4 | Primitive Obsession(med-low) | `remaining_risk: str` 存中文风险描述,低/中/高不可解析 | 风险等级是领域概念 | 记为合理保留:当前仅展示用途,阶段 8 评测若需量化再提 enum(见下) |
| S5 | Duplicated Code(low-med) | `verify/report.py` `_covered_by` 与 `Plan.allows` 重复实现路径覆盖 | 匹配逻辑复制 | 改调 `plan.allows()`,删 `_covered_by` |
| S6 | Speculative Generality(low) | `Worktree.repo` 字段从未被读取 | 冗余字段 | 删除该字段 |
| S7 | Duplicated Code(low) | `TestRunner.run` 超时/异常分支构造近似 TestResult | 两个分支 | 判定为可接受:分支语义(timed_out flag)不同,合一反而更乱,保留 |

## Spec 轴发现(2 项,均为真 bug)

| # | 类型 | 规格依据 → diff 位置 | 描述 |
|---|------|----------------------|------|
| P1 | wrong(**真 bug**) | M2 规格「严格模式 vs normal 模式」→ `verify/loop.py` 硬编码 `ToolRunner(mode="strict")`;`cli --strict` 只影响是否先打印计划 | **normal 模式语义丢失**:计划内写被 ask、越权才确认的流程被严格模式替代;`--strict`/非 strict 行为几乎相同。修复:`VerifiedLoop` 加 `mode` 参数,CLI/脚本 `--strict` 真正选择 strict,默认 normal |
| P2 | wrong(**真 bug**) | docs/03 声称「审计落盘」→ `cli` 打印 `repo/.whycode/audit.jsonl` 但审计实际写 `wt.path/.whycode/`,worktree 拆除即销毁;打印路径从不写入 | **审计不持久**:复盘无从谈起。修复:审计写到 `repo/.git/whycode-audits/<branch>.jsonl`(worktree 之外),CLI/脚本打印真实路径;新增回归测试 |

## 已确认不需修复

- **S4(风险等级 enum)**:`remaining_risk: str` 当前只做人类可读展示,阶段 8 评测指标(如「越权修改率」)再量化。已在 review 记录,避免提前抽象。
- **S7(TestRunner 分支)**:超时分支带 `timed_out=True` 语义,合一需引入 tuple,可读性无净收益。

## 已满足的规格点(不需修复)

- worktree 隔离(兄弟目录 + 沙箱边界 = worktree 路径)✓
- 测试执行 + 失败限轮次修复(AGENT_FIX_ROUNDS,默认 2)✓
- RiskReport(修改文件/测试命令/验证结果/剩余风险 + diff)✓
- Diff 生成(三-dot `git diff base...branch`)✓
- 端到端闭环可跑通(单元 + 真实 DeepSeek normal/strict)✓
- docs/03-verified-loop.md ✓

## 修复后验证

- 200 tests passed(167 + M3 33),ruff/mypy(strict,59 文件)全绿。
- 真实 DeepSeek:normal(计划内写放行)+ strict(只读 Planner → 计划 → 审批)均通过,审计落盘 `repo/.git/whycode-audits/` 存活。
- 审计持久化回归测试:`test_verified_loop_audit_persists_after_cleanup`。
