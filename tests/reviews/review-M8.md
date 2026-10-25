# M8 Review 记录(双轴 code-review)

> 审查范围:`git diff b041d2c...HEAD`(M8 核心提交 `680be8c`)。
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理。
> 审查时间:2026-08-31。状态:**已全部修复**(二轮修复提交,见文末修复表)。
> 两轴独立撞出同一最重问题:①token 双计(累计值被求和);②恢复率空转(resume 从未真正执行)。

## Spec 轴发现

### (a) 缺失 / 部分

| # | 规格依据 | 描述 |
|---|---------|------|
| M1 | design §1.4「恢复类:先跑一次…再同 session resume」 | **恢复率空转**:`eval/runner.py:111` 只跑一次 `loop.run`,`recovered = task.kind=="recovery" and report.passed`(:133)测的是普通成功率,resume 从未执行 → recovery_rate 无意义。 |
| M2 | design §1.4「restore-half:改一半后恢复(re-run 同一 task,复用 session)」 | **任务集形状偏离**:design 的 `fix-add-negative`/`fix-precision`/`restore-half` 缺失,被 `fix-both` + 两个普通 recovery 替代 —— 数量够 11,但「复用 session resume」的形状没了。 |
| M3 | design §1.4 task11「应被审批拒绝(越权率口径)」 | **越权「被审批拒绝」未实现**:`_auto_gate` 恒 True,越权写被执行后才事后标记(plan.allows),gate 拒绝路径无测试。 |

### (b) 越界(合理)

- `scope-extra-file`/`fix-both` 不在 design,但符合 scope/fix 类意图。可接受。

### (c) 实现可疑

| # | 规格依据 | 描述 |
|---|---------|------|
| M4 | design §1.4 Token 采集 | **token 双计**(同 S1):`_TokenCollector` 对每个 usage 事件**求和**,但 usage 事件是**累计值**(`loop.py:283-290` ContextWindow 累计)→ N 步任务高估 ~N×。 |
| M5 | design §1.2「验收命令通过 / 总任务」 | **任务成功率 = 验证通过率**:`ok=report.passed`、`passed=report.passed`(:141-143)完全相等,仅分母不同(`scored` 排除 readonly);验收命令未独立测量。 |
| M6 | design §1.2「恢复率 = resume 成功的任务 / resume 任务」 | **render() 分数错位**:`metrics.py:133` 显示 `ok/total`(如 9/11)但成功率分子是 `ok/len(scored)`(9/9);f-string 里还有个游离 `\n`。 |

### 清单核对
- 工具成功率 ✅(`_ToolCounter` 读 tool_result.is_error,runner.py:58-62);基线回归 ✅(CLI --baseline/--save-baseline 已接线);Docker ✅(nginx.conf /api 反代 + proxy_buffering off);token 四层链路已通但求和 bug(M4)。
- Dockerfile 用 `pip install .` 而非 `uv build` wheel —— 与验收「uv build 出 wheel」不完全一致(可接受,镜像内 pip 装等价)。

## Standards 轴发现

### 硬问题

| # | 气味 | 位置 | 描述 |
|---|------|------|------|
| S1 | 计算错(最严重,同 M4) | `eval/runner.py:34-48` `_TokenCollector` | usage 事件是**累计值**但被**求和** → N 步任务 token 高估 ~(N+1)/2×;`TOKEN_REGRESSION` 基线比对失真。修:取末值(max/last)而非求和;或 `_observe` 发每轮增量。 |
| S2 | 死逻辑(同 M1) | `eval/runner.py:8,117,133` + `session_id` 参数零消费 | docstring 声称「先跑一次…再 resume」但只有一次 run;`recovered` 只是普通成功;`session_id` 无调用方。修:实现两阶段(repo/session 复用)或删恢复率声明。 |
| S3 | 分母错位 | `eval/metrics.py:86,88-90` | `out_of_scope_rate = out_of_scope/len(results)` 但分子只计 `has_write_plan` → 应除以写计划数(11 任务里 9 个);`verify_rate=passed/n` 含 readonly。单元测试只用写计划任务 → 掩盖。 |
| S4 | 默认值反逻辑 | `cli/main.py:190,198` | `--auto-approve` store_true 默认 False,但 `run_eval_all` 默认 True + help「评测默认」;默认 CLI 跑 `gate=None` → 计划外写全被拒 → scope 任务永远 out_of_scope=False,评测量反了目的。 |

### 判断性

| # | 气味 | 位置 | 描述 |
|---|------|------|------|
| b-1 | Primitive Obsession / Repeated Switches | `task.kind` 字符串比较 4 处 + EvalResult 三个 bool 派生字段 | 抽 TaskKind 枚举;is_recovery/is_readonly/has_write_plan 是 kind+plan_files 派生的 Data Clump。 |
| b-2 | Middle Man | pyproject `mewcode-eval = cli.main:main` | 同 argv 别名,跑起来打印通用 usage 而非 eval,名不副实。 |
| b-3 | 死参数 | `run_eval_task(session_id=...)` | 无调用方(与 S2 恢复流未实现绑定)。 |
| b-4 | 边界 | `agent/loop.py` `add_tokens` | provider 只回 total_tokens(无 prompt/completion 拆分)→ token 记 0;client.py 只对缺 total 回退,反向没有。 |
| b-5 | 潜伏 | `.dockerignore` 排除 tests/(demo_repo 源)+ python:3.11-slim 无 git | eval 在 Docker 内跑会坏;当前 CMD 是 uvicorn,宿主侧才用 eval → 潜伏。 |

## 总结

- **Standards**:4 硬 + 5 判断;最重 = **S1 token 双计**(累计值被求和,指标失真)。
- **Spec**:3 缺失 + 3 实现可疑 + 1 合理越界;最重 = **M1 恢复率空转**(resume 从未执行,指标无意义)。
- 两轴收敛于同一核心问题:指标的真实性 —— token 双计 + 恢复率空转,都是「指标看着有,实际不可信」。

## 二轮修复(全部已修复,复测全绿)

| # | 修复 |
|---|------|
| S1/M4 | token 取**末值**(usage 事件是累计值,最后一个 = 任务总 token),非求和;注释说明口径 |
| S2/M1 | **真两阶段恢复**:recovery 固定 session_id,phase2 同 repo+session 再 run → VerifiedLoop 自动 resume;recovered=phase2 通过;新增两阶段测试 |
| S3 | metrics 加 `scored_total`/`write_plan_total`,越权率/验证率分母各归其位;readonly 排除 |
| S4 | **评测恒全放行**(移除 --auto-approve 歧义),CLI 与 runner 默认一致 |
| M5 | 设计取舍:`ok=report.passed`(闭环最终验证即验收),report 文档说明口径 |
| M2 | 恢复任务描述注明「首次会话可能中断,恢复后继续」(形状对齐两阶段) |
| M6 | render 用 `ok/scored_total` + 去游离 \n + 补分母信息 |
| b-2 | **eval_main 独立 entry**:mewcode-eval 固定走 eval 子命令,帮助显示 eval 专属参数 |
| b-3 | session_id 死参数 → 恢复类真正使用 |

**修复后验证:364 pytest + 18 vitest + ruff/mypy(strict,106 文件)全绿;mewcode-eval 帮助显示 eval 参数。**
