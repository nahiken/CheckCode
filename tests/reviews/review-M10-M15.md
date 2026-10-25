# M10–M15 代码审计报告(第二轮,跨里程碑)

> 范围:`git diff 78d120b..HEAD`(M10–M15,110 files / +11816 −349)。
> 方法:8 个独立 finder 视角 → 去重 → 6 个 verifier 逐条**实证**(真跑探针 / 变异 / A-B 对照)。
> 结果:全部 10 条复现,无一条被 REFUTED。修复见 commit `c248084`。
> 口径:修复前 **625 pytest + 34 vitest** → 修复后 **648 pytest + 34 vitest**。

---

## §1 视角与方法

| 视角 | 产出 |
|---|---|
| A1–A4 逐行 diff(M10+M11 / M12+M13 / M14 / M15) | 逐 hunk 读 + 读**所在函数**(未改行也在范围内) |
| B 删除行为审计 | 逐条 `-` 行找「不变式在哪重建」;查既有测试是否被削弱 |
| C 跨文件追踪 | 签名/契约变更的所有调用点(含 tests/scripts/eval/web) |
| 复用 / 简化 / 效率 | 重复实现、多余复杂度、启动与热路径浪费 |
| 高度 + 规范(CLAUDE.md) | 修在正确的抽象层;仓库无 CLAUDE.md,故规范轴为空 |

**验证阶段**:每个候选交给一个 verifier,要求**实跑**(真探针 / 真多进程 / A-B 父提交对照),
返回 CONFIRMED / PLAUSIBLE / REFUTED。结果:**10/10 CONFIRMED**,含 4 条端到端复现。

---

## §2 修复清单

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | `assembly/skills.py` | **默认路径静默瘫痪**:allow 为空 → 选中全部可见技能 → 取声明型技能的 allowedTools 并集 → 仓库里一个只声明 `mcp:fs`/`skill:x` 的技能就把本地工具面砍空(全 deny,**零报错**)。实测(readonly 探针)修前 `read_file → deny(layer=skill)`,修后 `permit(mode)`。 | 收窄**只跟随显式技能选择**;「声明了 allowedTools 却零本地工具」改为**不收窄 + 报警**(不再当作「合法空集」) |
| 2 | `assembly/worker.py` | **自身消息无限回执循环**:`process_inbox` 给「自己发的」广播回执 → 回执成为发给自己、可认领的消息 → 认领→回执→再认领。实测 6 轮产出 `m-1` / `m-1:reply` / `m-1:reply:reply` … **永不终止**,快照无界增长,`_drain` 不 sleep 热转。 | `record.sender == self.worker_id` → 不回执 |
| 3 | `verify/loop.py` | **只读会话不可 resume**:只读 pass 写 header/run/done 但**从不建分支**,`resumed=bool(records)` 为真 → `create_from_branch` → `fatal: invalid reference`。实测端到端复现,会话永久不可恢复。 | 新增 `_is_readonly_session`(done phase 的 `data.readonly`),此类会话按新会话处理。**变异复验**:停用判据即复现原错误 |
| 4 | `verify/trace.py` | **过期 passed**:run-1 done{passed:true} + run-2 崩溃未写 done → 摘要 `{run_id: "run-2", passed: true}`。CI/看板给未完成的运行开绿灯。 | 新一次 `run` phase 作废上一次结论(`passed=False`) |
| 5 | `assembly/build.py` | **白名单对 MCP 恒真空操作**:并集 `| set(extra_tools)` 从不看技能声明了哪个 server → 未声明的 server 的工具也进白名单;注释声称的按 server 作用域不存在。 | 如实修正注释与文档,记为**已知边界**(收窄是工具名级,不做 per-server 过滤) |
| 6 | `api/tasks.py` | **旧报告残留**:resume 失败 → `set_error` 只改状态不清 `report` → `/report` `/diff` 仍 **200 返回上一轮成果**,M12 的 409 契约对该 handle 永不可达。 | `set_error` 清空 `handle.report` |
| 7 | `api/app.py` + `ReportView.vue` | **计划被拒仍可回滚**:`force_branch` 的 `git branch -f` 会**凭空建出** M10 承诺「拒绝即不存在」的分支(实测分支数 2→3),违反 `test_plan_approval_loop` 的不变式。前端按钮只判 `v-if="taskId"`。 | 后端拒 `plan_rejected`/`readonly` 的回滚;前端不显示按钮 |
| 8 | `store/session.py` | **撕裂 UTF-8 → 500**:崩溃截断半个汉字时**解码**抛 `UnicodeDecodeError`(在 `try` 之外),`GET /trace` 实测 **500**,破坏「最多丢最后一行」承诺。 | `errors="replace"` → 坏行落入 `skipped` |
| 9 | `mailbox/queue.py` | **普通 nack 清空定向**:活跃 worker 一次可恢复的处理失败就把**私信**广播进公共池(实测 carol 领到了只发给 bob 的消息)。 | `_release_and_maybe_dead(recipient_gone=)`:只有 lease 过期(收件人疑似崩了)才去定向 |
| 10 | `verify/trace.py` | **granted 被当「已执行」**:before Hook 拒绝时 `granted=True`(语义是「权限放行」),trace 却渲染 `→ 已执行` 并计入 `granted`。 | 新增 `_is_executed()` + `summary.executed` 字段,渲染改用「已执行」 |

**同批附带修复**(同源或低成本):
- `verify/trace.py`:`run_id=""`(CLI `--run-id ""` / `?run_id=` 空串)归一化为「不过滤」——原样会静默返回近乎空的 Trace。
- `verify/trace.py`:按 run_id 过滤时 header **不带 ts** —— 它的 ts 属于首跑,参与时间跨度会让 `duration_s` ≈ 整段会话(实测 1002.5s vs 2.5s)。
- `api/app.py`:装配告警经 SSE `assembly_warning` 外显(原样**静默丢弃**,技能名拼错会无声退化成「不限制」)。
- `assembly/build.py`:`assembly.mcp` 改为**函数内延迟导入**(见下)。
- `verify/loop.py`:`plan_approved` 落盘移出「新会话」分支 —— 原样在 resume 路径拿到批准后**不落盘**,每次 resume 重问。

---

## §3 一处被「顺手修掉」的性能回归

M15 审计(commit `3d1f8ee`)把 `from assembly.build import build_assembly` 从函数体提到
**模块级**,修掉了「导入阻塞事件循环」的正确性 bug —— 但代价是 `assembly.build` 的传递依赖
`assembly.mcp → mcp_client.client → mcp SDK`(约 1s、上百个模块)变成**每个** API 进程的固定启动开销,
而 API 路径 `build_assembly(repo, None, ...)` **从不连 MCP**。

修法不是回退(那会重新引入阻塞),而是**把 `assembly.mcp` 延迟到真正要用它的守卫分支**:

```python
if self.config.mcp_servers:
    from assembly.mcp import MCPAssembly   # 只有真配了 MCP 才付这笔
    mcp = await self._stack.enter_async_context(MCPAssembly(...))
```

实测:`import api.app` **1.40s → 0.84s**,载入的 `mcp.*` 模块 **107 → 0**。
安全性:模块有 `from __future__ import annotations`,故 `MCPAssembly | None` 注解不求值
(已实测删除 TYPE_CHECKING 导入仍可 import,注解为字符串)。
守卫:`tests/assembly/test_lazy_mcp_import.py`(AST + sys.modules 双保险,变异复验:107 个模块)。

> **教训**:修「不该在热路径导入」时,别把导入**搬到更早的全局位置**就完事 ——
> 要问「这个依赖到底什么时候才真的需要」。**延迟到守卫分支**同时满足两个约束。

---

## §4 确认「没问题」的项(供后续复用)

- **M13 并发**:`max_concurrency()` 的 clamp/解析对 `"0"`/`"-1"`/`"abc"`/`""`/巨值/浮点串均正确;
  分区/回灌保持模型序与 `tool_call_id` 配对。**Planner 未被漏接线**(`policy/planner.py` 直接读
  `max_concurrency()`,有测试钉住)—— finder 的「Planner 串行」子claim 被 **REFUTED**。
- **签名变更的全部调用点**均安全:`VerifiedLoop` / `ToolRunner` / `PolicyEvaluator` /
  `build_assembly` / `SessionStore` / `assemble_skills` 的新参数**全部有默认值**,30+ 处调用点一致。
- **`TaskRecord` 新字段全有默认**(`kind=TASK`)→ M6 时代快照仍可解析。
- **`AgentLoop`/`build_agent` 的库默认仍是 1**;只有生产装配点读 `AGENT_MAX_CONCURRENCY`。
- **既有测试未被削弱**:改动过的既有测试文件,其断言变化是「加固/移位」而非放宽
  (如 `assert len(constructed)==1` 移到等待之后、等待条件收紧)。
- finder 报的 `assembly.build` 首导 **4.23s** 未复现(本机实测 1.07s),疑似冷缓存测量;
  但**方向正确**,故按实测值修复。

---

## §5 已知未修边界(诚实记录)

- **白名单对 MCP 是工具名级**:声明 `mcp:fs` 会把**所有已连 server 的**工具并入白名单
  (不做 per-server 过滤)。当前 `McpTool.server_label` 已携带 server 身份,可按需收紧。
- **CLI worker 路径**的白名单透传仍无自动化守卫(API 侧已补,见 `tests/api/test_allowlist_api.py`)。
- **`tools/runner.py` 同时传 `evaluator=` 与 `tools_allow=` 会静默丢弃白名单** —— 今天无生产调用点,
  建议加一行 `raise`(未做,记为后续项)。
- **`_lock_depth` 不区分线程**:同一 `MailboxStore` 被多线程复用时会误报「不可重入」;
  今天无生产路径跨线程共享(生产零 `threading`/`to_thread` 用法)。
- **POSIX `flock` 对同一 fd 可重入**:实测(Linux)同 fd 再取 `LOCK_EX` 会**成功** →
  `MailboxStore` 缓存的同一句柄不构成进程内互斥。今天无跨线程共享,故 latent;
  但它推翻了「POSIX 分支只是没跑过」的说法 —— 是**行为不同**,需在 CI 有 Linux runner 时正视。
- **`plan_approved` 在 resumed 分支重问**已修(落盘移出 else),但若在 `begin` 与
  `plan_approved` 之间崩溃仍会重问一次(可接受)。
