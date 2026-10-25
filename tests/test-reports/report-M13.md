# M13 测试评估报告

> 阶段:M13(生产启用 parallel_safe 并发)。评估时间:2026-09-11。
> 基线提交:M12 末(`a4b0ee8`)。测试口径:CI 全离线 mock;本报告只记本地确定性套件。
> 「520」为 M13 之后的**全项目口径**。

## 测试规模

### Python(pytest)—— 512 → 520(+8)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/verify/test_concurrency_integration.py` | **+8** | ①3 个 `read_file` → 并发峰值 3(生产默认 4);②`[read,write,read,read]` → 写期间读并发 **0**(双端检查 `violations==[]`)+ JSONL tool_call 顺序 == 模型序;③`AGENT_MAX_CONCURRENCY=1` → 峰值 1(串行);④非法值回退 4;⑤**库默认纪律**(直接构造 `AgentLoop` 时 env 无效,仍串行);⑥`_env_max_concurrency` 解析边界(缺省/空/0/负/非数);⑦**上界 clamp**(超大值 → 32);⑧**Planner 同源取上限**(审计中-1 回归) |
| `tests/api/test_approvals_api.py`(改) | +0 | 修既有竞态:`resume` 后先等 `status=="done"` 再断言构造次数 |
| **全项目累计** | **520 passed** | M13 后 |

### TypeScript(vitest)—— 32(不变)

M13 是后端并发接线,前端不涉及。

## 关键语义(测试锁定 + 变异验证)

- **写是串行屏障**:`_Meter` 在工具**进入与退出两处**检查「写 ∧ 读 是否同时活跃」。
  审计用**变异测试**证明单侧检查(只看进入)能在真实违规下通过 —— 已改双端,并对
  `partition_tool_calls` 的分组结构另有单测(`tests/agent/test_tool_calls.py`)。
- **结果顺序 == 模型序**:同批两个读、第一个慢 30 倍(完成序反转)→ JSONL 顺序仍为模型序。
- **库默认 × 生产装配的分界**:`AgentLoop`/`build_agent` 默认 1;`VerifiedLoop` 与 Planner
  从 `agent.config.max_concurrency()` 取(默认 4,clamp `[1,32]`)。
- **并发安全性**(审计专项实测,非本套件):`AuditLog.append` 同步无 await → 30 协程并发写
  20KB 记录 30 行完整;并发审批 gate 各自归位;`HookRegistry` 深拷贝快照保证事件隔离。

## 关键指标

- **520/520 pytest、32/32 vitest 通过**;`ruff check .`、mypy strict(70 源文件,含 `scripts/`)全清。
- 锤测稳定性:M13 测试 12 轮 ×6 = 72 次全绿;API 目录 10 轮 0 失败(修复竞态后)。

## 结论

M13 关闭审计软点 #1b(`parallel_safe` 并发在生产 `max_concurrency=1` 休眠):生产装配从
`AGENT_MAX_CONCURRENCY` 取值(默认 4),写仍是串行屏障。测试 512 → **520**(+8)。
双轴审计未发现并发真缺陷,但发现**接线点没盖全**(Planner 旁路,已修)与**测试断言单侧**
(变异测试证实,已改双端),另顺带修了一个既有真 bug(JSON 解析失败时 tool_call_id 为空)。
详见 `review-M13.md`。
