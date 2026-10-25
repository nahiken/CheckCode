# review-M12 — run_id + Trace JSONL + 运行摘要 + artifact 端点(双轴)

> 阶段:M12。审核:实现完成后交另一会话做**独立双轴**评审(Standards + Spec),
> 再主会话复核修复。审计方法:代码通读 + 实测命令(`--junitxml` 取证)+
> **构造反例脚本**(两次 run 的 JSONL、错误附加行计数、只读闭环真实运行),用后即删。

## 1. Spec 轴

M12 声称 7 条:

| # | 声称 | 判决 | 证据 |
|---|------|------|------|
| 1 | run_id 落到每条记录(基类唯一、单一写入点、旧文件可解析) | ✅ | `store/models.py:32`(基类唯一);`store/session.py` `_append`;旧文件实测解析为 `""` |
| 2 | resume 语义(多次 run 各记一条) | ⚠️→✅ | 主路径 ✅;只读路径 ❌ 见 M1(已修) |
| 3 | Trace JSONL 每行可解析、末尾换行、空 → "" | ✅ | `verify/trace.py` `to_jsonl`;测试 3 条 |
| 4 | 运行摘要纯从 TraceEntry 派生,计数正确 | ⚠️→✅ | 计数 ✅(错误附加行未误计);`run_id` 见 B1、`passed` 见 M2(均已修) |
| 5 | run_id 过滤只留该次运行(header 除外) | ❌→✅ | records 过滤表达式符合规格;但 header 的 run_id 语义把**摘要**带错 → B1 |
| 6 | artifact 端点 404/409/空 200 | ⚠️→✅ | 矩阵实测;`status=error` 不可区分 → M3(已修) |
| 7 | 兼容 | ✅ | 512 passed 无回归;`run_id=""` 对旧调用方零变化 |

## 2. 发现的问题(全部已修)

### B1(重)过滤第 2+ 次 run 时摘要 run_id 取错

- **根因**:run_id 有**两个竞争来源** —— header 字段(首跑写死)与 `phase="run"` 的
  `detail.run_id`(每次运行)。`run_summary` 只从 header 行取 → 过滤 run-2 时报 run-1。
- **实测**:构造真实落盘顺序(header 由 run-1 的 store 写入)→ `filter=run-2` 时
  `summary.run_id = run-1` ❌。
- **修法**:权威来源改为 `_last_run_id`(最后一条 run phase 的 detail);header 字段降级为回退。
- **为何原本测不出**:fixture 的 header 是裸构造(run_id 空),而生产里 header 必被 store 填成
  首跑 id —— **fixture 与生产形态不符掩盖了缺陷**。已按生产形态补 3 条回归测试。

### M1(中)只读 pass 的 run 元数据断裂

- **根因**:`_run_readonly` 只 `_open_session`,**不写 run phase**,`RiskReport` 也不带 run_id。
  实测只读闭环:`loop.run_id=run-ba48…` 而 `report.run_id=""`;摘要在只读会话上 `session_id=""`、
  `phases=[]`、`passed=False`(而 `RiskReport.passed` 因 readonly 恒 True → **两处结论互斥**)。
- **修法**:只读路径补 header + run phase + done phase;抽 `_record_session_start` 公共辅助
  (主路径/只读路径共用,消除「三处构造点漏字段」);`RiskReport(run_id=self.run_id)`。

### M2(中)`passed` 假阳性

- **根因**:`_has_failed_verify` 只看 `phase=="verify"`,漏 `resume_verify`;且
  `AGENT_FIX_ROUNDS=1` + resume 预验证失败 → `range(2,2)` 为空 → 一轮 verify 都不跑,
  `done` 照写 → 误判通过。
- **修法**:`passed` 取 done phase 的 `detail.passed`(loop 落盘权威结论),不再启发式推断。

### M3(中/轻)409 无法区分「进行中」与「已失败」

- **根因**:`/report`、`/diff` 对 `status=error`(终态)也返 409 + 纯中文 detail。
- **修法**:409 detail 结构化 `{message, status, error}`。

### Standards(已修)

| 项 | 处置 |
|---|---|
| `_run_id_from` 命名说谎(多 run 会话无唯一值) | 拆 `_first_run_id`(回退)/ `_last_run_id`(权威) |
| `_require_handle -> Any`;旧端点未收敛 | 收紧 `TaskHandle`;抽 `_report_or_409`;3 处内联 404 收敛 |
| `task_trace(format=...)` 遮蔽内建 + 未校验 | 改名 `fmt` + `Query(pattern=...)` → 非法 422 |
| 函数内重复 import;CLI/API 输出契约重复 | 保留(见 docs/12 有意取舍) |

## 3. 测试缺口(审计指出 → 已补)

1. **生产形态 fixture 缺失**(最关键,正是 B1 的温床)→ 补「header 由 store(run_id=RUN1)写入」用例。
2. 只读路径零覆盖 → 在 `test_verified_loop_readonly_nl_task` 加 run_id/落盘断言。
3. `session_file` 与 loop 的 session_id 一致性无契约 → 补 `test_session_file_matches_loop_session_id`。
4. 非法 `format`、`status=error` 的 409 无用例 → 已补。

## 4. 复核(主会话)

- B1/M1/M2 修复后,`pytest` 512 passed;`ruff` / `mypy strict`(64 文件)全清。
- B1 的修复用**生产形态**重建 fixture 验证(不再靠裸构造 header)。
- M1 的修复用只读闭环的真实运行验证(run_id 一致、落盘有 header+run+done)。

## 5. 一致性提醒(审计提出,已记入文档)

- `handle.session_file` 与 loop 的 session 路径**两处都**用 `_new_session_id(task_id)`:
  已加契约测试钉死;若 loop 侧改前缀,该测试会红(而非静默指向错文件)。
- 审计提出 fixture 分布「6/13/8」与实测「6/11/10」不符:那是审计**中途**快照,最终为 6/15/14(见 `report-M12.md`)。
