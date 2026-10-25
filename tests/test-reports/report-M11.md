# M11 测试评估报告

> 阶段:M11(真正的 Hook 注册表)。审查评估(工作流第 ⑤ 步)。评估时间:2026-09-10。
> 基线提交:`507255b`(M10 末)。测试口径:CI 全离线 mock;本报告只记本地确定性套件。
> **M11 独立于 M10 的测试改动**:M10 关闭软点 #1/#2/#3,M11 关闭软点 #4,
> 下面「477」即 M11 之后的**全项目口径**,勿与 M10 报告的 460 混读。

## 测试规模

### Python(pytest)—— 460 → 477(+17)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/hooks/test_registry.py` | **+8** | 注册序、sync/async 混用、kind 分桶、空注册表、**字段快照隔离**、**可变字段(args)深拷贝隔离**、before 抛错中断+上抛、after 抛错吞掉+链继续、default_registry 接线 |
| `tests/tools/test_runner_hooks.py` | **+9** | granted 双触发、rejected 仅 after、接线等价(自定义 vs 默认注册表)、**审计形态黄金值(冻结时钟全字段)**、before 抛错→拒绝+layer=hook、before 拒绝时写工具**零落盘**、after 抛错→结果照常回灌、**被拒路径 after 抛错**、默认注册表绑定 self.audit |
| `tests/tools/test_runner.py`(未改) | 0 | **原地 18 条全过** = 兼容纪律的活证据 |
| **全项目累计** | **477 passed** | M11 后 |

### TypeScript(vitest)—— 32(不变)

M11 是后端重构(JSONL 审计的 Hook 分发),前端不涉及,基线 32 不回归。

## 关键语义(测试锁定 + 变异验证)

- **兼容纪律(逐字节)**:`test_audit_record_is_byte_stable` 用**冻结时钟**断言 granted 路径
  审计记录的**全字段**(含 `result` 文案),把形态钉死。审计探针(独立于测试套件的
  frozen-clock md5 对比:8 次 dispatch × 两 runner,2990 字节,`diff` 无输出)证实
  M11 前后**逐字节相同**。
- **快照隔离**:字段快照 + `args` 深拷贝两条测试。**变异验证**:把 `deep=True` 去掉 →
  `test_snapshot_isolates_mutable_args` 变红;把 `_reject` 换成 `pass` →
  `test_before_hook_error_*` 两条同时变红。守卫**有效**。
- **before 抛错 → 零落盘**:对 `write_file` 断言 `not (tmp_path/"a.py").exists()`,物证级。
- **被拒路径的 after 抛错**:最危险盲区(`_reject → _finish` 也触发 after),断言只吞不抛、
  结果照常回灌、审计保留拒绝记录。

## 关键指标

- **477/477 pytest、32/32 vitest 通过**;`ruff check .`、mypy strict(64 源文件)全清。
- 默认注册表下审计输出与 M11 前逐字节一致(见上)。

## 结论

M11 关闭审计软点 #4(「无 Hook 分发器;`AuditLog.after` 死代码」):`ToolRunner` 不再直连
`AuditLog`,改为经 `HookRegistry` 分发,审计降级为默认注册表里的一个 Hook。测试 460 → **477**
(+17),全绿。双轴审计发现的 2 处「文档承诺/测试命名与实现不符」(浅拷贝、黄金守卫名不副实)
已修复并以变异测试证明;核心声称(逐字节兼容)以最强手段实测成立。详见 `review-M11.md`。
