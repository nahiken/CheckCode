# M8 测试设计:评测与交付(eval/ + 指标 + mewcode-eval + Docker)

> 工作流①产物(先设计再实现)。阶段 M8 按 `LEARNING_PLAN.md` 阶段 8 + `PHASED_PLAN.md` M8 里程碑。
> 核心价值:把 M1–M7 的闭环能力**量化**(固定任务集 + 指标 + 基线回归),再打包交付(wheel + Docker 全栈)。

## 1. 功能范围

### 1.1 分层

```
eval/
  tasks.py     # 11 条固定任务集(规范:快照仓库 + 验收命令 + 计划内文件)
  runner.py    # 单任务评估:prepare_repo 副本 + VerifiedLoop + 计时 + 指标采集
  metrics.py   # EvalResult / EvalSummary + 指标汇总 + 基线回归
cli/main.py    # mewcode eval(调用 eval/runner)→ mewcode-eval entry
Dockerfile     # 后端镜像(FastAPI + uvicorn)
web/Dockerfile # 前端镜像(Vue build → Nginx 静态资源)
nginx.conf     # /api 反代 + SSE 关闭缓冲(proxy_buffering off)
docker-compose.yml
```

### 1.2 指标定义与口径(要点)

| 指标 | 定义 | 口径 |
|------|------|------|
| 任务成功率 | 验收命令通过的任务 / 总任务 | `verify_command` 在 worktree 跑通 |
| 验证通过率 | RiskReport.passed / 总任务 | 闭环最后一轮验证通过 |
| 越权修改率 | 修改了计划外文件的任务 / 总任务 | `plan.allows(file)` 之外 |
| 恢复率 | resume 成功的任务 / resume 任务 | resume 后验证通过 |
| 工具成功率 | 成功工具调用 / 总工具调用 | `AgentResult.is_error=False` |
| Token | 每任务 prompt+completion tokens | 从 `ContextWindow` 累计(M8 接入) |
| 耗时 | 每任务墙钟时长 | `time.perf_counter` 包住 VerifiedLoop.run |

**基线回归**:与基线 EvalSummary 比较,任何指标恶化(成功率下降/越权率上升/Token 上升超阈值)→ 标记 regression。

### 1.3 Token 采集(阶段 8 接入真实 token 记账)

`context/compression.py ContextWindow` 已预留 `prompt_tokens/completion_tokens` 字段。
M8 接入:`StreamCollector` 聚合 usage-only SSE 块(`stream_options.include_usage`)→
`AgentLoop._call_model` 把 `collector.usage` 累加到 `ContextWindow` → `usage` 事件带 token。

### 1.4 固定任务集(11 条)

以 `tests/fixtures/demo_repo` 为模板(修复 divide/fib + 测试类 + 只读类 + 恢复类):

1. `fix-divide`:修复 divide 除零 → `python -m pytest tests/ -q` 通过
2. `fix-fib`:修复 fib 边界 → pytest 通过
3. `fix-add-negative`:修复 add 负数 → pytest 通过
4. `fix-precision`:修复浮点精度 → pytest 通过
5. `test-verify`:只加测试不改实现 → pytest 通过
6. `test-refactor`:重构保留行为 → pytest 通过
7. `readonly-describe`:只读描述,不改文件 → `git diff --name-only base` 为空
8. `readonly-plan`:只读做计划,不改文件 → diff 为空
9. `restore-half`:改一半后恢复(re-run 同一 task,复用 session)→ 验证通过
10. `resume-recovery`:resume 会话 → 验证通过(恢复率)
11. `scope-guard`:尝试越权改计划外文件 → 应被审批拒绝(越权率口径)

### 1.5 CLI

`mewcode eval` → 跑全部 11 条,打印 EvalSummary 表格 + 指标 + 基线回归标记。
`mewcode-eval` entry → 同一 handler。版本升 0.4.0。

### 1.6 Docker

- `Dockerfile`:python:3.11-slim + uv + `uv build` wheel + pip install + uvicorn 启动。
- `web/Dockerfile`:node build Vue → nginx 托管静态 + /api 反代到 api:8000。
- `nginx.conf`:`location /api` → `proxy_pass http://api:8000; proxy_buffering off;`(SSE)。
- `docker-compose.yml`:api + web 两服务。

## 2. 测试设计(单元 → 集成 → 端到端)

### `tests/eval/test_tasks.py`
- 任务集:11 条、id 唯一、verify 命令非空、plan 文件已声明;每类至少 1 条。

### `tests/eval/test_runner.py`(httpx.MockTransport 注入)
- 单任务:mock agent 修复 → EvalResult.ok=True + 计时 > 0 + Token 采集。
- 失败任务 → ok=False。
- 越权任务:mock 改计划外文件 → out_of_scope=True(越权率计数)。
- 恢复任务:先 fail 一次,resume 后通过 → recovery_ok。

### `tests/eval/test_metrics.py`
- EvalSummary:成功率/验证通过率/越权率/恢复率/工具成功率/总 Token/平均耗时 计算正确。
- 基线回归:相同结果 → 无回归;成功率下降 → regression=True。

### `tests/agent/test_usage.py`(Token 采集)
- usage-only SSE 块 → StreamChunk.usage 聚合 → ContextWindow.prompt/completion 累计。
- `usage` 事件带 token 字段。

### `tests/cli/test_cli_eval.py`
- `mewcode eval` 解析 + 调用 runner + 打印摘要;`mewcode-eval` entry 指向同 handler。

### 回归
- 全量 pytest 保持全绿(既有 341);ruff/mypy 全绿;前端 vitest 18 不回归。

## 3. 验收点对照(`PHASED_PLAN.md` M8)

| 验收点 | 测试 | 结果 |
|--------|------|------|
| `eval/`:11 条固定任务集;快照+验收命令+RiskReport 汇总指标 | test_tasks + test_runner + test_metrics | ✅ |
| 指标:任务成功率/验证通过率/越权修改率/恢复率/工具成功率/Token/耗时;基线回归 | test_metrics | ✅ |
| `mewcode` CLI + `mewcode-eval` + 0.4.0 wheel | test_cli_eval + `uv build` | ✅ |
| Docker Compose(FastAPI + Vue/Nginx);Nginx 静态资源、/api 反代、关闭 SSE 缓冲 | `docker compose config` + 手测 | ✅ |
| **验收**:`uv build` 出 wheel;`docker compose up` 全栈可访问 | 手测 | ✅ |

## 4. 设计要点

1. **评测指标的定义与口径**:每个指标可复现(固定任务集 + 快照仓库 + 验收命令);
   越权率/恢复率对应 M2/M4 的能力可量化验证。
2. **基线回归如何定位性能回退**:保存基线 EvalSummary,与本次比对,指标恶化(成功率降/
   越权率升/Token 升)自动标记 regression,回看该任务的 RiskReport/Diff/审计日志。
3. **Token 采集**:`stream_options.include_usage` → usage-only SSE 块 → StreamCollector
   聚合 → ContextWindow 累计 → 评测汇总;这是「模型用量可观测」的基础设施。
4. **交付形态**:`uv build` 出 wheel + Docker Compose 全栈(Nginx 静态 + /api 反代 +
   SSE 关缓冲)。
