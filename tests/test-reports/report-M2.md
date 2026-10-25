# M2 测试评估报告

> 阶段:M2 工具/权限/Hook。审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`f8922f1`(M2 核心)→ `6c9d8da`(review 修复)。

## 测试规模

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/policy/test_policy.py` | 22 | 路径沙箱(含 symlink 逃逸)、黑名单、分层评估器全链 |
| 单元 | `tests/tools/test_tools.py` | 14 | 文件工具/run_command/截断/超时 |
| 单元 | `tests/hooks/test_audit.py` | 3 | JSONL 落盘/读回/父目录 |
| 单元 | `tests/policy/test_planner.py` | 7 | 计划解析、只读 Planner、写被拒、空计划报错 |
| 集成 | `tests/tools/test_runner.py` | 16 | 统一安全入口:schema 校验/越权/审批/规则/审计/异常兜底 |
| 端到端 | `tests/tools/test_e2e_policy.py` | 7 | loop+ToolRunner 组合:越权拒/计划放行/严格模式/规则/全链路 |
| **合计** | **6 文件** | **69** | M2 新增 |

全项目累计:**165 tests passed**(M1 96 + M2 69),ruff 全绿,mypy strict 47 文件无问题。

## 关键安全断言(验收对照)

| 验收点 | 测试 | 结果 |
|--------|------|------|
| Agent 越权写被拒 | `test_e2e_unauthorized_write_blocked_and_audited` | ✅ 文件不变 + 审计 granted=False |
| 路径逃逸被拒 | `test_path_escape_blocked_by_sandbox` + symlink 逃逸 | ✅ 沙箱层拦截 |
| 高危命令误触发 | `test_high_risk_command_blocked` + `test_blacklist_*` | ✅ 黑名单拦截 |
| 参数 schema 校验(空对象) | `test_schema_validation_rejects_missing_required` | ✅ M1 遗留修复 |
| 严格模式审批 | `test_strict_mode_plan_write_still_asks` / `test_e2e_strict_mode_requires_approval` | ✅ 命中计划仍需审批 |
| 只读 Planner | `test_build_plan_reads_then_outputs_plan` / `write_attempt_denied` | ✅ 只读生成计划 |
| Hook 审计落盘 | `test_audit_records_full_flow`(asked/approved/granted/result/duration_ms) | ✅ 完整审计 |
| 工具超时 | `test_run_command_timeout`(0.1s 超时) | ✅ |

## 真实 LLM 端到端验证

两次真实 DeepSeek 调用成功:

1. **normal 模式**(`--auto-approve`):Agent 读 math_utils.py → 修复 divide/fib → 跑 pytest 6 passed。审计日志:`read_file permit`(只读放行)→ `write_file ask approved=True`(越权写审批)→ `run_command ask approved=True`(1516ms)。
2. **严格模式**(`--strict --auto-approve`):只读 Planner 生成计划 `files=['math_utils.py'], commands=['python -m pytest']` → 修复 → pytest 6 passed。审计:`read_file permit` / `write_file ask(严格模式命中计划仍需审批)` / `run_command ask`。

## 关键指标

- 修复后回归:165/165 通过,零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,47 source files。
- 范围蔓延判定:Rule pattern 子串匹配、truncate_output 提前实现 —— 记为合理保留(详见 review-M2.md)。

## 结论

M2 全部验收点通过。分层权限(沙箱→黑名单→规则→计划→模式)、正式工具集、两段式 Hook 审计、只读 Planner、严格模式审批均已实现并有测试与真实调用双重证据。下一步 M3(Worktree 隔离 + Verified Coding Loop)。
