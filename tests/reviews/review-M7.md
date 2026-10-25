# M7 Review 记录(双轴 code-review)

> 审查范围:`git diff e74f183...HEAD`(M7 核心提交 `1ec586f`)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 审查时间:2026-08-31。修复提交:`ea357d3`(review 修复)+ `da3c5ca`(评估记录)
> 修复后验证:**340 tests passed** + 前端 vitest 14/14 + ruff/mypy(strict,100 文件)全绿
> + 真实 uvicorn 冒烟(SSE 端点 report 帧含 WindowsPath 不崩)

## Spec 轴发现

### 真缺口(验收点落空)

| # | 类型 | 规格依据 | 描述 |
|---|------|---------|------|
| F1 | missing(最严重) | PHASED_PLAN「SSE 事件(…回滚)」+「→ 可回滚」验收点 | **回滚完全缺失**:api/ 无 rollback 端点,前端无回滚按钮,SSE 事件表无回滚事件。修复:`POST /api/tasks/{id}/rollback`(git reset 到 base_commit)+ SSE `rollback` 事件 + 前端回滚按钮 |
| F2 | wrong(致命) | design §1.2「report 终止事件 + RiskReport 全量」 | **report 帧序列化崩溃**:`_report_to_dict` 用 `model_dump()`,repo 是 WindowsPath → `json.dumps` 抛 TypeError → SSE 在 report 处崩断,客户端永远收不到终止事件。另 `passed` 是 property 不在 dump → 前端恒显失败。修复:fastapi `jsonable_encoder` + `passed` 字段显式并入 |
| F3 | wrong(致命) | design §2.1「工具执行进度」 | **工具事件协议不符**:后端 `tool_exec_start` 只发 `{step,count}`、`tool_result` 发 `{step,tool_call_id,is_error}`,前端 ConversationStream 读 `data.tool`/`data.content` → 真实工具名/结果恒空(测试 mock 假字段掩盖)。修复:后端 tool 事件带 name/content 或前端按真实字段渲染 |
| F4 | missing | LEARNING_PLAN「Vue 3 + TS + Zod 校验」 | **Zod 缺失**:createTask 响应仅 `as` 断言。修复:加 zod + 响应 schema 校验 |
| F5 | missing | PHASED_PLAN「任务列表 / Diff 视图」 | **任务列表缺失**:web/ 无任务列表组件,后端无 GET /api/tasks。修复:`GET /api/tasks` 列表端点 + 前端列表组件 |
| F6 | wrong | design §2.1「未找到 → 404」 | **404 语义错**:`task_status` 返回 200+error,测试断言 200 掩盖。修复:未找到 → 404 |
| F7 | wrong | design §1.2「auto_approve」 | **auto_approve 接线失效**:请求字段解析后未用,`_auto_approve_gate` 恒 True。修复:gate 依 auto_approve 构造 |
| F8 | missing | design §2.1「tests/api/test_sse.py」 | **SSE 端点测试缺失**:未驱动真实 SSE generator(顺序/report 终止/挂接)。修复:补 test_sse.py |
| F9 | missing | design §2.3「events 全路径透传」 | **修复轮/恢复路径无覆盖**:只有正常路径测 events。修复:补修复轮 + resume 透传测试 |

### 确认成立(不需修)

- SSE 协议 `event_to_dict → {event, data}` 与 StepEvent 一致;AgentLoop 只 emit 原 11 种 kind,resume/verify 仅 VerifiedLoop 直发,无意外 emit。
- 队列满丢弃最旧 → report/error 恒在队尾,不会因满被丢。

## Standards 轴发现

### 硬违规

| # | 气味 | 位置 | 描述 | 修复 |
|---|------|------|------|------|
| S1 | 序列化崩溃 | api/app.py `_report_to_dict` | **同 F2** | jsonable_encoder + passed |
| S2 | 死代码/旋钮失效 | api/app.py `to_jsonable`(零消费)+ `make_config`(空闭包)+ `_TaskRequest.auto_approve`(未接线) | 同 F7 + 删死代码 | 删 to_jsonable/make_config;auto_approve 接线 |
| S3 | 后台任务生命周期 | api/app.py `asyncio.create_task` 无引用保存 | 挂起任务可被 GC 中断 | 存引用(self._background_tasks) |
| S4 | 工具事件字段协议不符 | agent/loop.py vs web/ConversationStream | **同 F3** | 统一字段 |
| S5 | SystemExit 不可捕 | prepare_repo 内部 sys.exit | 坏 repo 时任务卡 running | except BaseException |

### 判断性(已修)

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| b-1 | 破坏性读多消费者 | TaskHandle.queue 是破坏性读 | 单消费者语义 + 迟到订阅回放 report |
| b-2 | 前端分帧 bug | sse.ts buffer 尾部处理 | 修缓冲逻辑(完整帧不残留) |
| b-3 | 死字段/清单重复 | api/tasks.py docstring 手抄事件清单 | 引用 StepKind 单一来源 |
| b-4 | verify 输出双截断 | model_dump vs PhaseRecord[:500] | 统一 |

## 修复后验证

- **全量测试 + ruff/mypy(strict)全绿 + vitest 全绿**。
- F2:report 帧真实序列化(WindowsPath + passed)通过 SSE 端点验证。
- F3:工具事件字段协议统一,前端渲染真实字段。
- F1:rollback 端点 + 前端回滚按钮 + 测试。

---

# M7 Review 记录(二轮 / 整体再审 M7)

> 审查范围:`git diff e74f183...HEAD`(M7 全部 4 提交:核心 `1ec586f` + 修复 `ea357d3` + 文档 `da3c5ca`/`ff1e983`)。
> 审查方式:并行 Standards + Spec 子代理。时间:2026-08-31。
> 状态:**已修复**(后续修复提交)。两轴独立撞出同一最重问题:S1/R5 回滚 `reset_hard` 破坏主仓。

## Spec 轴发现

### (a) 缺失 / 部分

| # | 规格依据 | 描述 |
|---|---------|------|
| R1 | PHASED_PLAN「Vitest + **MSW** 接口测试」;design §1.3「MSW mock HTTP」 | **MSW 未交付**:`web/package.json` 无 msw 依赖,测试全用手写 fetch/ReadableStream mock(`docs/07` §6 亦承认)。 |
| R2 | PHASED_PLAN「组件:…Diff…」;design §1.3「Diff 视图」 | **Diff 非独立组件**:内嵌 `ReportView.vue:50` 的 `<pre class="diff">`,未拆 DiffView 组件(功能在,形式部分)。 |
| R3 | design §2.2 T1「返回 **202** + task_id + sse_url」 | **创建端点返回 200 非 202**:`app.py:128` 无 `status_code`,测试断言随实现改 200(静默偏离设计)。 |
| R4 | design §2.1 E4「不存在的 task_id → **404**」 | **SSE 未知任务返 200+error 帧**:`app.py:180-184` StreamingResponse(设计曾要求 404)。 |

### (b) 越界(合理,可接受)

- `GET /api/tasks` + `POST /rollback`:不在 design 端点表(3 端点),为「任务列表」组件与验收「可回滚」所需。
- 转发多余 StepKind(`truncated`/`finish`/`all_invalid`/`max_steps`/`usage`):design 10 事件表外,复用 StepKind 可接受。

### (c) 实现可疑

| # | 规格依据 | 描述 |
|---|---------|------|
| R5 | PHASED_PLAN 验收「可回滚」 | **回滚硬重置主仓,破坏性**(同 S1):`app.py:236-237` `reset_hard(base)` 把主仓当前分支+工作树回退到基线,清掉任务期间用户在主线的一切提交/改动;Agent 改动本在已删 worktree/孤儿分支,主树无改动 → 对主树无可视效果,只重置孤儿 sess 分支。 |
| R6 | design §1.2 SSE 事件表(无 rollback 事件) | **rollback SSE 事件是死的**:流在 report 终止(`app.py:187`),前端收 report 即停(`sse.ts:91`);回滚仅允许 done/error 后 → 无活跃订阅者能收到 rollback 帧,UI 只靠 POST 响应。 |
| R7 | design §1.2「verify 每轮 TestResult」 | **verify 轮号不一致**:resume 路径发 `round: 0`(`verify/loop.py:182`),正常路径从 1 起 → 前端显示「验证第0轮」。 |
| R8 | design §1.2 body `{task, repo?, strict?, auto_approve?}` | **手写 dict 解析非 Pydantic**:`bool(raw.get("auto_approve"))` 对字符串 `"false"` 强转为 True(`_parse_task_request`)。 |

## Standards 轴发现

### 硬问题

| # | 气味 | 位置 | 描述 |
|---|------|------|------|
| S1 | 破坏性副作用(最严重) | `app.py:236-237` + `worktree/manager.py:205-212` | **同 R5,双轴独立确认**:rollback 的 `reset_hard` 作用于主仓(`self.repo`)→ 回退主分支 + 清工作树。`force_branch` 已够丢弃 Agent commit(worktree 在 loop finally 已删),reset_hard 冗余且摧毁无关工作。修:删 reset_hard,只留 force_branch。 |
| S2 | 资源泄漏 / 跨任务串流 | `TaskForm.vue:35` / `App.vue:15` | **SSE 订阅不关旧**:每次提交新订阅不关旧的;任务 B 启动/重选时任务 A 流仍开,迟到事件经无 taskId 的 onEvent 混入共享 events → 污染 B 视图。修:先关旧订阅,或事件带 taskId 前端过滤。 |
| S3 | 破坏性读多消费者 | `api/tasks.py` queue / `app.py:187-199` | 任一 N 个客户端连 `/events` 共享同一 asyncio.Queue,每个事件恰被一个消费者取走 → 并发读者各看部分/空流,重连无法回放。修:广播(每订阅者队列)或文档化单客户端+并发 409。 |
| S4 | 位置配对 | `ConversationStream.vue:26` | `tool_result` 按 `out[out.length-1]` 位置配对,后端已广播 `tool_call_id`(`agent/loop.py:235`);并发工具乱序 → 结果挂错卡。修:按 tool_call_id 建键。 |

### 判断性

| # | 气味 | 位置 | 描述 |
|---|------|------|------|
| b-1 | 吞取消 | `app.py:95` `except BaseException` | 捕 `asyncio.CancelledError` 转永久 error 态且不重抛 → 服务关停取消无法完成。修:重抛 CancelledError。 |
| b-2 | Speculative Generality | `app.py:58-65` + `_parse_task_request` | `gate(name,args,decision)` 忽略全参数;`strict`/`auto_approve` 前端根本没发(createTask 只发 task)→ 无活调用者的钩子。 |
| b-3 | Duplicated Code + Middle Man | `verify/loop.py:178-216` + `_call_events:408` | `kind="verify"` 事件构造两处重复(round 0 预验 + 轮循环);抽 `_emit_verify`;`_call_events` 纯透传。 |
| b-4 | 死终止事件 | `app.py:187,198` | rollback 事件在 report 之后才 push → 无消费者;UI 靠 POST 响应(无害但误导)。 |

## 二轮总结

- **Standards**:4 硬 + 4 判断;最重 = **S1 回滚 `reset_hard` 破坏主仓**(双轴独立确认)。
- **Spec**:4 缺失 + 3 实现可疑 + 2 合理越界;最重 = **R5 破坏性回滚 / R1 MSW 未交付**。
- 两轴最严重问题收敛于同一处:rollback 的 `reset_hard` 摧毁无关工作。

## 二轮修复(后续提交)

| # | 修复 |
|---|------|
| S1/R5 | rollback 删 `reset_hard`,只留 `force_branch`(Worktree 隔离下主仓无需 reset);worktree/manager.py 删 reset_hard 方法 |
| S2 | TaskForm 保存订阅关闭句柄,新提交前先关旧 |
| S3 | TaskHandle 改广播(history + 每订阅者队列);SSE 端点迟到回放 + 终止事件实时送达(修 R6);未完成任务 404(修 R4) |
| S4 | ConversationStream 工具卡片按 tool_call_id 建键 |
| R1 | msw devDep + sse-http.test.ts(node 环境,绝对 URL)+ msw-handlers.ts |
| R2 | DiffView.vue 独立组件 |
| R3/R4 | 创建端点 202;SSE 未知任务 404 |
| R7/R8 | verify 轮号 resume 从 1 起;Pydantic 请求模型(字符串 "false" 正确解析) |
| b-3 | verify 事件构造去重(_emit_verify) |

**修复后验证:341 pytest + 18 vitest + ruff/mypy(strict,100 文件)+ vue-tsc(noEmit)全绿。**
