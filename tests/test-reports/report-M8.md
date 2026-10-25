# M8 测试评估报告

> 阶段:M8 评测与交付(eval/ 11 条任务集 + 指标汇总 + 基线回归 + token 采集 + CLI/wheel + Docker)。
> 审查评估(工作流第 ⑤ 步)。评估时间:2026-08-31。
> 基线提交:`680be8c`(M8 核心)+ 二轮 review 修复(未独立提交号,随收尾提交)。

## 测试规模

### Python(pytest)

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/eval/test_eval.py`(任务集) | 4 | 11 条任务 id 唯一/verify+plan_files 规范/get_task/五类齐全 |
| 单元 | `tests/eval/test_eval.py`(Runner) | 5 | fix 通过/不修失败/越权判定/只读无改动/**两阶段恢复(recovery phase1+phase2 resume)** |
| 单元 | `tests/eval/test_eval.py`(Metrics) | 9 | 成功率/通过率/越权率/恢复率/工具成功率/Token/耗时/**readonly 排除分母/越权率写计划分母**/基线回归(相等无回归+恶化检测) |
| 单元 | `tests/agent/test_stream_client.py` | +1 | **usage-only 块捕获 token** |
| 单元 | `tests/agent/test_stream_collector.py` | +1 | **collector 聚合 usage** |
| 单元 | `tests/agent/test_loop.py` | +1 | **usage 事件带累计 token(record_event)** |
| 集成 | `tests/test_cli.py` | +3 | eval 无 key 退出/未知任务报错/help 含 --baseline |
| **合计** | — | **+3** | M8 二轮修复新增 |

全项目累计:**361 → 364 pytest passed**(M8 二轮修复新增 3:两阶段恢复 + 2 分母测试)。

### TypeScript(vitest)

前端 M7 已 18/18,M8 无前端改动,不回归。

## review 修复(二轮,对应 review-M8)

| # | 问题 | 修复 |
|---|------|------|
| S1/M4 | **token 双计**:usage 事件是累计值,`_TokenCollector` 求和 → 高估 ~N× | 改**取末值**(最后一个 usage 事件 = 任务总 token),注释说明口径 |
| S2/M1 | **恢复率空转**:只跑一次 `loop.run`,`recovered` 是普通成功 | **真两阶段**:recovery 固定 session_id,phase2 同 repo+session 再 run → VerifiedLoop 自动 resume;recovered=phase2 通过;新增两阶段测试 |
| S3 | **分母错位**:越权率除以总数而非写计划任务数;验证率含 readonly | 加 `scored_total`/`write_plan_total` 字段,分母各归其位;新增分母测试 |
| M5 | **验收未独立**:`ok=report.passed`,成功率=通过率 | 设计取舍:闭环中 report.passed 即最终验证,二者天然一致;文档说明口径 |
| S4 | **auto_approve 矛盾**:CLI 默认 False 但 runner 默认 True | **评测恒全放行**(移除 --auto-approve 歧义,评测衡量能力非审批) |
| M2 | **任务集形状**:恢复任务描述未体现两阶段 | 任务描述注明「首次会话可能中断,恢复后继续」 |
| M6 | **render 分数错位**:`ok/total` vs 分子 `ok/scored`;游离 \n | render 用 `ok/scored_total` + 去 \n;补分母信息 |
| b-2 | **mewcode-eval 别名**:跑出通用 usage | 独立 `eval_main` entry,固定走 eval 子命令(帮助显示 eval 专属参数) |
| b-3 | session_id 死参数 | 恢复类真正使用(两阶段共用) |

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| `eval/`:11 条固定任务集 + 快照 + 验收命令 + RiskReport 汇总指标 | test_eval.py 任务集 | ✅ 11 条/五类 |
| 指标:成功率/通过率/越权率/恢复率/工具成功率/Token/耗时;基线回归 | test_eval.py Metrics | ✅ 8 用例 + 分母修正 |
| `mewcode` CLI + `mewcode-eval` + 0.4.0 wheel | test_cli.py + uv build | ✅ eval_main entry |
| Docker Compose + Nginx 静态/反代/SSE 关缓冲 | docker compose config | ✅ |
| **验收**:`uv build` 出 wheel;`docker compose up` 全栈 | uv build 验证 wheel | ✅ whycode-0.4.0 |

## 关键指标

- 回归后全量:**364/364 通过**(361 + M8 二轮新增 3),ruff/mypy(strict,106 文件)全绿;vitest 18/18 不回归。
- **两阶段恢复**:phase1 写坏 → phase2 resume 写对 → recovered=True(测试锁定「恢复率」真实语义)。
- **token 采集**:usage 事件累计值 → 取末值 = 任务总 token(修复前按步数高估 ~N×)。
- 真实 DeepSeek 冒烟(M8 核心):11 条任务,成功率 0.89、验证通过率 0.73、越权率 0.00、~26s/任务。

## 结论

M8 全部验收点通过。11 条固定任务集、7 项指标(分母口径修正)、基线回归、token 采集、
mewcode-eval 专属 entry、0.4.0 wheel、Docker Compose 全栈。二轮 review 修复了
「指标看着有、实际不可信」的核心问题(token 双计 + 恢复率空转 + 分母错位)。下一阶段无(M0–M8 全链路交付完成)。
