# M9 测试评估报告

> 阶段:M9 收尾(关闭 8 项完成度差距)。审查评估(工作流第 ⑤ 步)。评估时间:2026-09-09。
> 基线提交:`2588735`(W5)→`e473dbc`(W4)→`a312089`(W6)→`e1f8478`(W2)→`2e243dc`(W3)→`a9f58ea`(W1)。
> 测试口径:CI 全离线 mock(不跑真实 LLM/Docker/浏览器/外部 MCP);本报告只记本地确定性套件。

## 测试规模

### Python(pytest)—— 364 → 427

| W | 累计 | 本段新增 | 覆盖内容 |
|---|------|---------|----------|
| (W5 前) | 364 | — | M0–M8 全量基线 |
| W5 token 水位 | 372 | +8 | compression 估算/compress_to_budget 单测 + loop 长对话压缩集成 |
| W4 parallel_safe | 379 | +7 | 分区 mixed `[read,write,read,read]@2`、loop 并发水位(只读批峰值/写屏障) |
| W6 只读分流 | 381 | +2 | Planner 空→只读 pass;只读里写→deny 回灌 |
| W2 Trace | 389 | +8 | store ts 落盘/legacy 无 ts/build_trace gap+summary/渲染/缺文件→[] |
| W3 装配层 | 410 | +21 | config(tomllib)+ MCP 装配 fake client + skills + worker + verify extra 注入 |
| W1 浏览器审批 | **427** | **+17** | approvals 14(同 loop 审批流/resume 编排/端点/列表概要)+ SSE approval/resume 2 + 列表快照 1 |
| **全项目累计** | **427 passed** | +63 | W1 后全绿(a9f58ea);W7/W8 未改代码,基线不变 |

### TypeScript(vitest)—— 18 → 27

| W | 累计 | 本段新增 | 覆盖内容 |
|---|------|---------|----------|
| (M8) | 18 | — | 前端基线(parse/subscribe/taskform/conversation/sse-http) |
| W1 | **27** | +9 | approve/resume API(sse-http 2)、TaskForm 模式请求体 2、tasklist-approval 4(awaiting 允许/拒绝、done 恢复、App approval 事件驱动)、taskform body 1 |
| **全项目累计** | **27 passed** | +9 | W7 前端未动不回归 |

## review 修复(对应 review-M9)

| # | 问题 | 修复 |
|---|------|------|
| S1 | MCP AsyncExitStack 关闭从未 await;`__aenter__` 抛错不触发 `__aexit__` → 泄漏 | `push_async_callback` + 手动 `aclose`;exit-path 测试锁死 |
| S2 | `import api.app as api_app` 绑到 FastAPI 实例(包级 `app` 遮蔽子模块) | 测试改 `importlib.import_module("api.app")`;注释说明 |
| S3 | 审批 Future/后台协程须同 loop resolve | 单测区分 HTTP 轮询 vs async 直驱;portal 语义明确 |
| M9 | W4 分区期望 `[read,write,read,read]@2` | 按算法真实语义(读未达批量在写前 flush)→ `[read],[write],[read,read]` |
| M10 | resume 惯性新拷 demo | 复用 `handle.repo`(store 记录在仓库内) |

## 关键指标

- **回归后全量:427/427 pytest、27/27 vitest 通过**;ruff + mypy strict + vue-tsc 全清。
- **浏览器审批闭环**(W1):gate Future 挂起 → approval 非终止事件 → awaiting → resolve 放行/拒绝
  回灌,SSE 流在审批间保持打开;resume 复用 repo/session 不重放——均有单测锁定。
- **真实运行脚本**(W8):`WHYCODE_SMOKE_REAL=1` 守卫(未设 exit 0 实测),**CI 不执行**;
  ruff + mypy 对 scripts/ 6 文件全清。
- **评测数字口径**:0.89/0.73/0.00 为 **2026-08-31 M8 历史**待复核记录(当前 `ok==passed`
  同分母不可复算),**不写入当前口径**;须 `WHYCODE_SMOKE_REAL=1` 真实重跑并保存逐任务
  summary 后方可引用新值。

## 结论

M9 全 8 项(5 代码 + 文档 + runbook)关闭,测试 364→**427 pytest**、18→**27 vitest** 全绿,
ruff/mypy/vue-tsc 干净,CI 全离线。8 项差距(W5 token 水位、W4 parallel_safe、W6 只读分流、
W2 Trace、W3 装配层、W1 浏览器审批+resume、W7 文档口径、W8 真实运行脚本)由「边界/待办」转
「已实现 + 可离线验证」。真实 Provider/外部 MCP/生产稳定性留 W8 opt-in 脚本端到端验证。
