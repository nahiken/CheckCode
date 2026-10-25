# M12 测试评估报告

> 阶段:M12(run_id + Trace JSONL + 运行摘要 + artifact 端点)。评估时间:2026-09-10。
> 基线提交:M11 末。测试口径:CI 全离线 mock;本报告只记本地确定性套件。
> 「512」为 M12 之后的**全项目口径**。

## 测试规模

### Python(pytest)—— 477 → 512(+35)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/store/test_run_id.py` | **+6** | 统一填充 run_id、显式优先(不被覆盖)、默认空、旧文件解析为 ""、resume 追加新值、判别式 union 不破 |
| `tests/verify/test_trace_jsonl.py` | **+15** | to_jsonl 每行可解析/空/末尾换行;run_summary 计数(含被拒调用)、两次 run;run_id 过滤 + header 保留;**B1 回归 ×2**(header run_id 被首跑写死时过滤第二次 run);**M2 回归 ×2**(resume_verify 失败/done 权威);加载过滤 |
| `tests/api/test_artifacts_api.py` | **+14** | report/diff/trace 的 200-404-409 矩阵;jsonl content-type;非法 format → 422;M3 结构化 409 detail(`status=error`);**session_file 契约**;运行中空结果 |
| `tests/verify/test_verified_loop.py`(改) | +0 | 只读 pass 的 run_id + 落盘断言(header/run/done) |
| **全项目累计** | **512 passed** | M12 后 |

### TypeScript(vitest)—— 32(不变,有意)

前端**不加** TS 客户端:`ReportView`/`DiffView` 已由 SSE `report` 事件驱动,新端点是给 CI/`curl`
的产物接口。加无人调用的 `fetchTrace`/`fetchDiff` 正是 M11 审计惩罚的「死代码」——维持 32 是**决定**。

## 关键语义(测试锁定)

- **run_id 权威来源**:`phase="run"` 的 detail(非 header 字段)。B1 回归测试按**生产形态**
  (header 由首跑 store 写入)构造 —— 裸构造 header 的 fixture 曾掩盖该 bug。
- **摘要 `passed`**:取 done phase 的 `detail.passed`(loop 权威结论),非启发式。
- **只读 pass 完整性**:run_id 一致 + 落盘 header/run/done。
- **端点契约**:409 结构化(区分进行中/已失败);非法 format 422;运行中读 Trace 返空 200。

## 关键指标

- **512/512 pytest、32/32 vitest 通过**;`ruff check .`、mypy strict(64 源文件)全清。
- 端到端实跑:`mewcode trace --format jsonl --run-id run-111` → run_id 过滤生效、JSONL 逐行可解析、
  摘要计数正确。

## 结论

M12 关闭审计软点 #5(无 run_id / Trace 非 JSONL)与 #2b(无运行摘要/artifact 端点):
每条会话记录带 run_id、`mewcode trace` 输出 JSONL + 运行摘要、`/report` `/diff` `/trace` 成为一等端点。
测试 477 → **512**(+35)。双轴审计发现 1 重(B1)+ 2 中(M1/M2)+ 1 轻(M3),全部修复并补回归测试。
详见 `review-M12.md`。
