# M7 测试评估报告

> 阶段:M7 浏览器工作台(FastAPI 任务创建 + SSE 事件流 + Vue 3 前端)。
> 审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`1ec586f`(M7 核心)+ review 修复 `ea357d3`。

## 测试规模

### Python(pytest)

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/api/test_tasks_api.py` | 13 | 任务创建/空校验/404/状态查询/SSE 帧往返/TaskManager 队列有界/error/report/rollback |
| 单元 | `tests/api/test_sse.py` | 5 | **真实 SSE generator**:事件顺序/report 终止/Path 序列化/error+rollback 终止/帧往返 |
| 集成 | `tests/verify/test_loop_events.py` | 5 | VerifiedLoop events 透传:全部事件/verify 数据/零回调 noop/**工具字段协议**/**修复轮透传** |
| **合计** | **3 文件** | **23** | M7 后端新增 |

全项目累计:**340 tests passed**(M6 317 + M7 后端 23),ruff/mypy strict 100 文件无问题。

### TypeScript(vitest)

| 文件 | 用例数 | 覆盖内容 |
|------|-------|----------|
| `web/tests/sse.test.ts` | 6 | SSE 帧解析(单/多/注释/损坏)/流式订阅/report 终止/HTTP 错误 |
| `web/tests/conversation.test.ts` | 4 | 对话流四类事件渲染/工具 error 样式/ReportView 渲染/**回滚按钮触发请求** |
| `web/tests/taskform.test.ts` | 4 | createTask POST/**TaskForm 提交订阅 SSE 透传**/空输入禁用 |
| **合计** | **14** | M7 前端新增 |

前端 **14/14 vitest 通过**,vue-tsc 类型检查干净。

## review 修复(双轴,4 严重 + 5 硬违规 + 4 判断)

### Spec 轴

- **F1 回滚完全缺失**(验收点落空)→ `POST /api/tasks/{id}/rollback`(force_branch 重置任务分支到基线 + reset_hard)+ SSE `rollback` 终止事件 + 前端回滚按钮。
- **F2 report 序列化崩溃**(致命):`model_dump()` 留 WindowsPath → `json.dumps` 崩,SSE 终止事件永不到达;`passed` 是 property 不在 dump → 前端恒显失败 → `jsonable_encoder`(Path→str)+ 显式并入 passed。
- **F3 工具事件协议不符**(致命):后端 `tool_exec_start` 只发 `{step,count}`、`tool_result` 发 `{step,tool_call_id,is_error}`,前端读 `data.tool/content` 恒空 → 后端补 `tools`(工具名列表)+ `content` 字段。
- **F4 Zod 缺失** → 加 zod,createTask 响应 schema 校验。
- **F5 任务列表缺失** → `GET /api/tasks` + 前端 TaskList 组件。
- **F6 404 语义错**(200+error,测试断言掩盖)→ HTTPException 404 + 测试改断言 404。
- **F7 auto_approve 接线失效**(解析后未用)→ `_make_approval_gate(auto_approve)`。
- **F8/F9 测试缺口** → 补 test_sse.py(真实 generator)+ 修复轮透传测试。

### Standards 轴

- **S2 死代码/旋钮失效**:`to_jsonable`(零消费)+ `make_config`(空闭包)+ auto_approve(未接线)→ 删除/接线。
- **S3 后台任务生命周期**:`asyncio.create_task` 无引用 → `_background_tasks` 集合 + done_callback 清理。
- **S5 SystemExit 不可捕**:`prepare_repo` 内部 sys.exit → `except BaseException`。
- **b-1 破坏性读多消费者**:TaskHandle.queue 单消费者;迟到订阅需回放 report(设计声明单消费者语义)。
- **b-2 前端分帧 bug**:完整帧残留 buffer → 下块重复触发 → 只消费以 `\n\n` 终止的完整帧。
- **b-3 事件清单重复**:docstring 手抄 StepKind → 引用单一来源。

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| FastAPI 任务创建 | test_tasks_api(T1)+ 冒烟(真实 uvicorn) | ✅ 202/200 + task_id + sse_url |
| SSE 实时事件(进度/验证/恢复) | test_sse + test_loop_events | ✅ 真实 generator 顺序/终止 |
| **回滚**(验收点) | test_tasks_api + test_sse(rollback 终止) | ✅ 端点 + 前端按钮 + SSE 事件 |
| Vue 组件:对话流/Diff/验证报告 | web/tests/conversation.test.ts | ✅ 四类事件渲染 |
| Vitest + Zod | web/tests + zod schema | ✅ 14/14 + 响应校验 |
| 浏览器开任务 → SSE → Diff/报告 → 可回滚 | 冒烟(uvicorn + report 帧验证) | ✅ 全链路挂接 |

## 关键指标

- 修复后回归:340/340 通过(后端)+ 14/14(前端),零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,100 source files;vue-tsc 干净。
- **F2 关键验证**:report 含 WindowsPath → `_report_to_dict` 转 str + passed=True → 真实 SSE 端点产出完整 report 帧(json.dumps 不崩)。
- 真实 uvicorn 冒烟:SSE 端点对未知任务返回 error 帧、任务创建返回 task_id+sse_url。

## 结论

M7 全部验收点通过。FastAPI 任务创建 + SSE 事件流(进度/验证/恢复/回滚)+ 前端对话流/Diff/验证报告/回滚按钮 + Vitest/Zod,review 修复后全栈验证通过。review 发现的最严重问题(report 帧崩溃致 SSE 永不终止、回滚缺失、工具事件协议不符)已全部修复并有回归测试。下一里程碑 M8(评测 + 打包交付)。
