# M3 测试评估报告

> 阶段:M3 Worktree 隔离 + 验证闭环(Verified Coding Loop)。审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`b217238`(M3 核心)→ `a92a60e`(review 修复)。

## 测试规模

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/worktree/test_worktree_manager.py` | 10 | worktree 生命周期(create/remove 幂等/自定义/残留恢复/真冲突)、隔离性、Diff/变更文件 |
| 单元 | `tests/verify/test_verify_runner.py` | 7 | 确定性测试执行(通过/失败码/UTF-8/超时/截断/回退/占位) |
| 单元 | `tests/verify/test_report.py` | 7 | 风险推导(低/高/超时/越权/覆盖/空轮次)、渲染 |
| 集成 | `tests/verify/test_verified_loop.py` | 6 | 闭环:通过/修复两轮/限轮次/主仓隔离/审计持久化/测试命令选择 |
| 端到端 | `tests/verify/test_e2e_verified_loop.py` | 1 | 完整闭环:修改→测试→报告→worktree 清理无残留 |
| **合计** | **5 文件** | **31** | M3 新增 |

全项目累计:**201 tests passed**(M2 167 + M3 34,其中 3 个为新 review 回归),ruff/mypy strict 60 文件无问题。

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| 修改默认在独立 Worktree | `test_worktree_isolation_*` + `test_e2e_verified_loop_full_closure` | ✅ 主仓工作树零污染、HEAD 不动、无残留 worktree |
| 测试执行 + 失败限轮次修复 | `test_verified_loop_fix_path_rounds_2` / `test_verified_loop_limited_rounds` | ✅ 失败回灌修复;到限停止标记高风险 |
| RiskReport(文件/命令/结果/风险) | `test_report_render_contains_key_fields` + loop 各测试 | ✅ 全字段 + 可读渲染 |
| Diff 生成 | `test_diff_and_changed_files` | ✅ 三-dot 精确取证,排除运行噪声 |
| 端到端闭环可跑通 | `test_e2e_verified_loop_full_closure` + 真实 DeepSeek | ✅ 修改→测试→报告→清理 |

## 真实 LLM 端到端验证

两次真实 DeepSeek 调用成功:

1. **normal 模式**(`--auto-approve`):Agent 一次修复 divide/fib → 真实 pytest 通过(1 轮,1375ms)→ 报告低风险,修改文件 `math_utils.py`,Diff 干净。
2. **严格模式**(`--strict --auto-approve`):只读 Planner 生成 `files=['math_utils.py'], commands=['python -m pytest tests/test_math_utils.py']` → 闭环验证通过。

审计持久化:worktree 拆除后,`repo/.git/whycode-audits/<branch>.jsonl` 仍存 12 条记录(write_file 前后两段)。CLI `mewcode run --task "修复 math_utils" --auto-approve` 端到端跑通。

## 关键指标

- 修复后回归:201/201 通过,零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,59 source files。
- 范围蔓延判定:S4(风险等级未提 enum)记为「阶段 8 评测再量化」;`AGENT_FIX_ROUNDS`/`_TEST_PREFIXES` 为合理配置化。

## 结论

M3 全部验收点通过。worktree 隔离(主仓零污染)、确定性测试执行、失败限轮次修复、RiskReport + Diff 取证、端到端闭环均已实现并有测试 + 真实调用双重证据。review 修复了 2 个真 bug(normal 模式语义丢失、审计不持久)+ 4 项去重重构。下一步 M4(JSONL 持久化 + 会话恢复)。
