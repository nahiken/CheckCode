# M7 测试设计:浏览器工作台(FastAPI + SSE + Vue 3)

> 工作流①产物(先设计再实现)。阶段 M7 按 `LEARNING_PLAN.md` 阶段 7 + `PHASED_PLAN.md` M7 里程碑。
> 设计目标:每项验收点先定测试(红)→ 实现(绿)→ 补边界。
> 核心价值:把 M1–M6 的 CLI 能力变成**浏览器里可实时观测的闭环**。

## 1. 功能范围

### 1.1 分层

```
web/(Vue 3 + TS + Vite + Vitest/MSW)   ← 浏览器工作台
  └─ fetch /events SSE(EventSource 或 fetch 流)
api/(FastAPI)                          ← 任务创建 + SSE 事件流
  └─ 调用 verify/loop.VerifiedLoop     ← M3 闭环
       └─ AgentLoop.run(events=...)    ← M1/M4 实时事件回调(已预埋,「为阶段 7 SSE 预埋」)
```

### 1.2 后端 `api/`

| 端点 | 方法 | 作用 |
|------|------|------|
| `POST /api/tasks` | 创建任务 | body `{task, repo?, strict?, auto_approve?}` → 启动后台 asyncio 任务 → 返回 `{task_id, sse_url}` |
| `GET /api/tasks/{task_id}/events` | SSE 流 | 实时推送 StepEvent(进度/Diff/验证/恢复/回滚)+ 终止事件(`report`) |
| `GET /api/tasks/{task_id}` | 任务状态 | 查询任务当前状态(结果缓存/未找到 404) |

**核心:SSE 事件流怎么来?**

`VerifiedLoop.run(repo, task)` 目前不暴露实时事件。M7 给 `VerifiedLoop.run` 加可选
`events: Callable[[StepEvent], Awaitable[None]] | None` 参数,透传给 `AgentLoop.run(events=...)`
(M4 已预埋)。API 层把每次事件写入一个 `asyncio.Queue`;SSE 端点从队列取事件按 SSE 格式
`data: {json}\n\n` 逐条 yield。任务完成时 push 一个 `report` 事件(含 RiskReport JSON)。

**SSE 事件类型**(复用 `StepKind` + 新增):

| event | 内容 |
|-------|------|
| `model_call` / `thinking` / `assistant` | Agent 流式输出(思考/文本) |
| `tool_exec_start` / `tool_result` / `invalid_params` | 工具执行进度 |
| `verify` | 每轮验证结果(TestResult) |
| `resume` | 恢复会话标记 |
| `report` | 最终 RiskReport 全量(终止事件) |
| `error` | 任务异常终止 |

### 1.3 前端 `web/`

- Vue 3 + TS + Vite;组件:任务列表 / 对话流(Agent 消息 + 工具调用)/ Diff 视图 / 验证报告。
- 关键交互:创建任务 → 订阅 SSE → 实时渲染 Agent 输出 → 结束后展示 Diff + RiskReport。
- Vitest 单测组件逻辑;MSW mock HTTP(任务创建)+ 手写 SSE 流 mock(事件推送)。

### 1.4 与既有架构的衔接

- **SSE 挂接点已预埋**:`AgentLoop.run(events=...)`(M4 注释「供 CLI/前端/评测消费」)。
- **RiskReport 可 JSON 化**:`model_dump()` → 前端直接渲染 Diff/验证记录/剩余风险。
- **审批**:`auto_approve=True` 或 M8 再做人机交互审批(SSE 双向通道);M7 先用 auto_approve。

## 2. 测试设计

### 2.1 `tests/api/test_sse.py`(单元,不启动真实 LLM)

**目标:SSE 事件流机制。**

| # | 用例 | 断言 |
|---|------|------|
| E1 | 队列生产者 push 事件 → SSE 端点 yield | 每条 `data: {json}\n\n` 完整、顺序保持 |
| E2 | 任务完成 push `report` 终止事件 | 流结束后客户端收到 report |
| E3 | 任务异常 → `error` 事件 + 流终止 | 不悬挂 |
| E4 | 不存在的 task_id → 404 | 明确错误 |
| E5 | 同一事件多消费者? | **广播语义(每连接独立队列,各收全量)**;迟到订阅回放历史(report/error 恒在) |
| E6 | rollback 实时送达 | 活跃订阅者收到 rollback 终止事件(非死事件) |

### 2.2 `tests/api/test_tasks_api.py`(集成,httpx.MockTransport)

**目标:任务创建 + 状态查询。** 用 mock transport 注入 fake model(不真实调用 LLM)。

| # | 用例 | 断言 |
|---|------|------|
| T1 | `POST /api/tasks` 创建 | 返回 202 + task_id + sse_url |
| T2 | `GET /api/tasks/{id}` | 返回任务状态(created/running/done) |
| T3 | 任务完成 → status=done + 结果缓存 | 可查询到 report |
| T4 | 未找到 → 404 | 明确错误 |
| T5 | `VerifiedLoop.run(events=...)` 被调用且事件到达队列 | 挂接正确 |

### 2.3 `tests/verify/test_loop_events.py`(集成)

**目标:`VerifiedLoop.run` 新增 events 透传。**

| # | 用例 | 断言 |
|---|------|------|
| V1 | 正常 run → events 回调收到全部事件 | model_call/tool_exec_start/tool_result/finish 齐全 |
| V2 | events 回调在验证轮次触发 verify 事件 | 每轮 TestResult 到达 |
| V3 | 不传 events → 行为不变(向后兼容) | 既有测试零破坏 |

### 2.4 `tests/web/`(Vitest + MSW,前端)

| # | 用例 | 断言 |
|---|------|------|
| W1 | 创建任务表单 → POST /api/tasks | mock 返回 202,组件进入 running |
| W2 | SSE 事件流 → 对话流组件渲染 | model_call/assistant/tool_result 逐步渲染 |
| W3 | report 事件 → Diff + RiskReport 渲染 | 结束后展示 |
| W4 | SSE 错误 → 显示错误态 | 不悬挂 |

### 2.5 回归

- 全量 `uv run pytest` 保持全绿(既有 317 不回归);ruff/mypy(strict) 全绿。
- `web/` 下 `npm run test:unit`(Vitest)全绿。

## 3. 验收点对照(`PHASED_PLAN.md` M7)

| 验收点 | 测试 | 结果 |
|--------|------|------|
| FastAPI 任务创建 | tests/api/test_tasks_api(T1) | ✅ |
| SSE 实时事件(进度/Diff/验证/恢复/回滚) | tests/api/test_sse + test_loop_events | ✅ |
| Vue 组件:任务列表 / 对话流 / Diff / 验证报告 | tests/web(W1–W4) | ✅ |
| Vitest + MSW 接口测试 | tests/web | ✅ |
| 浏览器开任务 → SSE 实时看输出 → Diff/报告 → 可回滚 | e2e(手测:uvicorn + npm dev) | ✅ |

## 4. 设计要点

1. **SSE vs WebSocket 的选择**:单向事件流(Agent → 前端)用 SSE——HTTP 复用、自动重连、
   `EventSource` 原生支持;WebSocket 用于双向实时(审批回传)时才需要。SSE 用 `data:` 帧 +
   `text/event-stream`,事件语义与 AgentLoop 的 StepKind 一一对应。
2. **后端如何把 CLI 闭环变成 SSE 流**:`VerifiedLoop.run(events=...)` 透传已预埋的
   `AgentLoop.run(events=...)` 回调 → `asyncio.Queue` → SSE generator;事件消费与任务执行解耦,
   前端只订阅、不阻塞 Agent 运行。
3. **前端增量渲染 Tool Use 流**:StepKind 细分(model_call/thinking/tool_exec_start/tool_result),
   Vue 组件按事件类型分别渲染文本/思考/工具卡片,Diff 与 RiskReport 在 report 事件一次性落地。
