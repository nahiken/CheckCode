# review-M13 — 生产启用 parallel_safe 并发(双轴)

> 阶段:M13。审核:实现完成后交另一会话做**独立双轴**评审(Standards + Spec),
> 再主会话复核修复。审计方法:代码通读 + 实测命令 + **自建 15 条临时测试**(双向并发探针、
> 同批乱序完成、混合非法参数、env 边界、并发审计 append 20KB、并发审批 gate、变异测试),
> 用后即删(md5 校验复原)。

## 1. Spec 轴

M13 声称 6 条:

| # | 声称 | 判决 | 证据 |
|---|------|------|------|
| 1 | 生产装配启用并发,单一接线点 | ⚠️→✅ | `verify/loop.py` 接线生效且 CLI/Web/worker/只读 pass 全经它;**但 Planner 旁路** → 中-1(已修) |
| 2 | 库默认仍为 1 | ✅ | `agent/loop.py:85/326` 未改;实测 env=8 时直接构造 `AgentLoop` 峰值仍 1 |
| 3 | 写仍是串行屏障 | ✅ | 实测 `partition_tool_calls([r1,w1,r2,r3],...)` → `[[r1],[w1],[r2,r3]]`;双向探针 `violations==[]`;`run_command` 同理 |
| 4 | 结果顺序不受影响 | ✅ | 同批两个读、第一个慢 30 倍(完成序反转)→ JSONL ids 仍 == 模型序;内容与 id 不串位 |
| 5 | 默认 4 的有界性 | ⚠️→✅ | 默认实测峰值 4;**但 env 无上界** → 轻-1(已修) |
| 6 | env 解析健壮 | ✅ | 实测全集:`None/""/   `→4;`0/-3`→1;`abc/2.5/1e2/0x10`→4;`1_0`→10(意外接受,已随上界 clamp 收敛) |

## 2. 发现的问题

### 中-1(真缺口)Planner 绕过接线点,并发恒为 1(已修)

- **问题**:`policy/planner.py` 直接 `build_agent(...)` **不带 `max_concurrency`** → 库默认 1。
  而 Planner 正是**多文件只读扫描**、最该并发的一段;Web 每个任务都先跑它
  (`api/app.py` 不注入 plan → `_resolve_plan` → `build_plan`)。
- **实测**:monkeypatch 捕获 `build_agent` kwargs,env=8 跑 `build_plan` → kwargs **无该键**,
  `loop._max_concurrency == 1`。
- **修法**:把 env 解析上移到**中立模块** `agent/config.py`(`max_concurrency()`),
  `verify/loop.py`(薄封装保留旧名)、`policy/planner.py`、`scripts/verify_deepseek.py` 三处共用 ——
  既补上旁路,又消除 `policy → verify` 的方向依赖与「跨模块 import 私有名」坏味。
- **回归测试**:`test_planner_uses_shared_concurrency`。

### 轻-1 env 无上界 → 可把默认 executor 打满(已修)

- **问题**:`max(1, int(raw))` 只保下界。`AGENT_MAX_CONCURRENCY=999…9` 原样成为分区块大小。
- **影响**:同步工具经默认 executor(线程池约 `min(32, cpu+4)`);超出部分排队,而
  `asyncio.wait_for(run_in_executor(...), tool_timeout)` 的计时**从入队开始** →
  队列尾部工具可能「还没跑就超时」。
- **修法**:clamp 到 `[1, MAX_CONCURRENCY_CEILING=32]`(与默认 executor 同量级)。
  回归测试 `test_max_concurrency_has_upper_bound`。

### 轻-3(既有真 bug,顺带修)`tool_call_id` 为空

- **问题**:`agent/loop.py` 的 `error_by_id` 分支构造 `AgentResult` **不带 `tool_call_id`**
  → tool 消息 id 为空,与 assistant 的 tool_calls 无法配对(provider 硬约束);
  且因 id 为假值而**跳过落盘** → 会话里没有该调用的记录,resume seed 留下悬空 tool_call。
- **与 M13 无关**(`agent/loop.py` 本里程碑零改动),但被并发审计顺带发现。
- **修法**:补 `tool_call_id=tc.id`(一行)。既有测试只断言消息**顺序**、从不断言 id,故一直漏网。
- **注**:schema 不合法(如缺字段)走 runner 路径,id 正常;只有「JSON 解析失败」踩此坑。

### 轻-2 文档悬空引用

`M13_PROGRESS.md` 写「双轴审计见 §5」等 —— 属流程后续产物(本文件/`report-M13`/`docs/13` 现已就位)。

## 3. 并发热点专项(审计逐项实测,**未发现真缺陷**)

| 热点 | 结论 | 依据 |
|---|---|---|
| **并发 × 审批** | 无问题 | `ToolRunner` 每 run 单实例共享,但共享的只有只读的 `tools`/`evaluator`(纯函数)、`hooks`、`audit`;每次 dispatch 新建 `ToolAuditEvent`;`HookRegistry` 给每个 Hook **深拷贝快照**(M11)。实测:两个需审批工具并发、gate 挂起 50ms → 两 gate 都被调用、结果各归各位。**生产不可达**:只读工具在 evaluator 第 4 步直接 permit(生产不传 rules);MCP 工具未声明 `parallel_safe` → 永不进并发批 |
| **并发 × 审计** | 无问题 | `AuditLog.append` 是**同步函数、无 await 点** → 单事件循环下不可能交错。实测:30 协程并发 append **20KB** 记录 → 30 行完整、长度精确 |
| **并发 × `results_by_id`** | 无顺序依赖 | 唯一消费者按 id 取值;且 `asyncio.gather` **保序** → 填充顺序本就是模型序(非完成序) |

## 4. 测试缺口(审计指出 → 已补/已修)

1. **Planner 旁路零覆盖** → 已补 `test_planner_uses_shared_concurrency`。
2. **超大 env 值无测试** → 已补上界 clamp 用例。
3. **交付的 barrier 断言是单侧的**(最严重的方法论问题)→ 审计用**变异测试**证明:
   把分区换回「纯切片块大小 2」后,原 `reads_during_write == 0` **照样通过**
   (因为读在写**之前**进入,`_writing` 当时为 False)。
   已改为**双端检查**(进入与退出都查 `写 ∧ 读` 活跃),并断言 `violations == []`。
4. **`READ_PATHS` 含不存在的 `notes.txt`** → 换成真实文件(`README.md`),避免用例混入
   「文件不存在」的错误结果。

## 5. 复核(主会话)

- 修复后 `pytest` **520 passed**(512→520,+8);`ruff` / `mypy strict`(70 文件,含 scripts)全清。
- 双端 barrier 断言经**变异验证**:改回单侧判定后,变异分区能触发 `violations`。

## 6. 另一处独立修复(非 M13,会话中一并处理)

`tests/api/test_approvals_api.py::test_resume_runs_again_reusing_repo` 存在**既有竞态**:
`client.post(resume)` 只 `create_task` 不 await,返回后立即断言「假 loop 构造一次」——
后台协程可能尚未被调度。锤测 2/15 失败。修法:**先等任务到终态(`status=="done"`)再断言**
(注意不能等 `("done","error")`:种下的任务本来状态就是 error,会立刻命中)。
修后 37 轮全绿 + API 目录 10 轮 0 失败。
