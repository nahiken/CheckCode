# M10 审计发现记录(第三方双轴 code-review)

> 审计对象:M10 计划审批门 + 推理通道 + 回滚覆盖(基线 `78d120b`,改动未提交)。
> 审计方式:`mattpocock-skills:code-review` 双轴(Standard / Spec)并行子代理 + 主会话复核。
> 审计时间:2026-09-10。复现口径:pytest **458** / vitest **32** / ruff / mypy strict / vue-tsc 全绿。

## 0. 复现结果(与交接单 §1 一致)

| 命令 | 期望 | 实测 |
|---|---|---|
| `uv run pytest -q` | 458 passed | ✅ 458 collected,全绿 |
| `npx vitest run` | 32 passed | ✅ 32 passed |
| `uv run ruff check .` | All checks passed | ✅ |
| `uv run mypy ...` | 63 source files | ✅ Success: no issues found in 63 source files |
| `npx vue-tsc --noEmit` | 无输出 | ✅ 无输出 |

**结论**:交接单的功能面陈述成立;**但有 3 处「声称的安全保证」与代码实际不符**(Spec 轴),
以及一组可收敛的重复/坏味(Standards 轴)。

---

## 1. Spec 轴(规格符合度)

规格来源:`FEATURE_COMPLETION_PLAN.md` §M10、`progresses.md` §二、`AUDIT_HANDOFF_M10.md`、
`docs/10-m10.md`。

### S-1【重】拒绝路径并非「零 session 文件」,且自证测试未断言

- **规格**:`FEATURE_COMPLETION_PLAN.md:38` —— "gate 插在计划解析(132-142)与
  worktree/session 创建(145-166)**之间**——拒绝时零 worktree、零分支、**零 session 文件**。"
  交接单 §3.1 亦称拒绝路径"不触碰任何 … session 文件"。
- **实际**:第 0 步在门**之前**无条件构造 `SessionStore` 并 `continue_session()`
  (`verify/loop.py:140-147`),被拒绝时仍写一条 phase 记录(`verify/loop.py:180-182`),
  经 `store/session.py:_append:61-63`(`path.parent.mkdir` + 追加写)落地
  `<repo>/.git/whycode-sessions/<id>.jsonl`。
- **自证缺口**:`tests/verify/test_plan_approval_loop.py::test_reject_returns_early_no_side_effects`
  的 docstring 写「无 session 文件」,**但断言里没有这一条**——声称未被测试。
- **定性**:规格明确写了「零 session 文件」,此为**硬违反**,非有意取舍(§4 未列此项)。
- **改法**:把 session 目录/文件的创建推迟到审批通过之后(拒绝路径只读既有记录,不写)。

### S-2【重】resume 只复用「批准标志」,不复用「计划」

- **规格**:`FEATURE_COMPLETION_PLAN.md:38` —— "**resume 时若存在该 phase 则复用计划、
  不重复询问**。"
- **实际**:`_plan_already_approved()`(`verify/loop.py:513-522`)只返回布尔量用于**跳过门**;
  `run()` 随后仍走 `build_plan` **重新生成**计划(`verify/loop.py:151-156`),因为 API resume
  不注入计划(`api/app.py:139-147`)。已批准计划只以 `{files}` 记法持久化(`loop.py:197`),**从不读回**。
- **后果**:恢复的会话可能执行一份**从未展示给审批**的新计划——审批门的语义被削弱。
- **定性**:规格明写「复用计划」,此为**实现与规格不符**。
- **改法**:`plan_approved` phase 落盘完整计划(files+commands);resume 时若有则读回注入。

### S-3【中】`plan_approval` SSE 事件重复发出(其一带空 `approval_id`)

- **实际**:`verify/loop.py:170-176` 先发一个 `StepEvent(kind="plan_approval")`(**无**
  `approval_id`);随后 gate 经 `api/tasks.py:set_awaiting_plan_approval` 又推第二个(带 `approval_id`)。
  `App.vue:handleEvent` 取第一个 → 面板以 `approval_id: ""` 渲染;此时点击会 POST 空 id → 404。
- **定性**:实现缺陷(事件重复 + 首个事件字段不全)。
- **改法**:`VerifiedLoop` 不再发该事件,由 gate(掌握 `approval_id` 的一侧)唯一发出。

### S-4【轻】空计划严格任务绕过审批

`run()` 在 Planner 返回空计划时于 `verify/loop.py:161` 提前转入只读 pass,**早于**计划门,
故 `strict + 空计划` 完全不触发审批。可论证为有意(空计划=无写意图),但**未在规格中声明**。
→ 处置:**记录为已知边界**,补注释与文档,不改行为(改行为会让 W6 只读分流回归)。

### S-5【轻】`tests/policy/test_plan_approval.py` 未创建

`FEATURE_COMPLETION_PLAN.md:48` 点名该文件;实际覆盖并入 `test_plan_approval_loop.py`。
→ 处置:测试覆盖已实质存在,更新文档措辞即可。

### S-6【轻】范围蔓延

`LEARNING_PLAN.md`、`PHASED_PLAN.md`(mcp→mcp_client 改名、措辞改写)、
`PROJECT_VERIFICATION.md §3` 与 M10 三个软点无关。→ 处置:留待 commit 时分组说明。

### §4「有意取舍」逐项复核

| # | 取舍 | 复核 |
|---|---|---|
| 1 | 闭环内不自动回滚 | ✅ `verify/loop.py` 无 rollback 调用 |
| 2 | 测试命令来自 plan 声明/注入 | ✅ `verify/loop.py:455-462` |
| 3 | 拒绝时 `passed=True` | ✅ `verify/report.py:90` |
| 4 | 两门并存不短路 | ✅ `loop.py:376` 仍走逐工具门 |
| 5 | 推理通道诚实边界 | ✅ `docs/10-m10.md:67-77`、`.env.example` |
| 6 | `auto_approve=True` 计划门放行 | ✅ `api/app.py:106-107`、`cli/main.py` |
| 7 | `require_plan_approval` 默认 True 受 strict 门控 | ✅ `api/app.py:62,146` |
| T1 | `kind` 字段同步 | ✅ 有意加字段,`TaskList.vue` 消费 |
| T2 | `force_branch` 仅在拆除后调用 | ✅ `api/app.py:303`、`scripts/smoke_real.py:116` 均在 teardown 之后 |

---

## 2. Standards 轴(编码规范)

> 无 `CODING_STANDARDS.md`/`CONTRIBUTING.md`;文档化标准仅 `pyproject.toml`
> (ruff `line-length=100`/`select=E,F,I,UP,B,SIM`、mypy `strict`)。**硬性违规:0**。
> 以下均为 Fowler 气味基线(判断题)。

### D-1【重】重复代码(approval ↔ plan_approval 多处同构)

| 位置 | 重复对象 |
|---|---|
| `api/app.py:96-116` `_make_plan_gate` | 克隆 `_make_approval_gate`(`api/app.py:73-93`) |
| `api/app.py:342-364` `resolve_plan_approval` | 与 `resolve_approval`(`:317-339`)逐字节相同 |
| `api/tasks.py:211-237` `set_awaiting_plan_approval` | 重复 `set_awaiting_approval`(`:187-209`) |
| `cli/main.py:103-116` | 重复 `_make_approval_gate`(`:84-100`)的 y/N 逻辑 |
| `PlanApprovalPanel.vue` | 整体克隆 `ApprovalPanel.vue`(含 CSS) |

### D-2【中】Primitive Obsession / Repeated Switches

审批种类是裸字符串 `kind: str = "tool"`(`api/tasks.py:49`),以 `== "plan"` 过滤(`:172`);
状态同为裸字符串,在 `set_running`(`:181-184`)、`resume_task`(`api/app.py:381`)、
`App.vue`、`TaskList.vue` 四处 cascade。

### D-3【中】Mysterious(说谎的)Name

计划以 `tool_name="plan"` 存储(`api/tasks.py:223`),又在 `pending_summary` 作为
`"tool_name"` 暴露——该字段已不再命名其内容。

### D-4【轻】Data Clumps

`files`+`commands` 成对穿过 `set_awaiting_plan_approval`、SSE payload、面板 props 与
`TaskList`——而 `policy.models.Plan` 早已打包二者。

### D-5【轻】Speculative Generality / 失效指令

`reject_plan_gate(reason)` 接受被丢弃的 `reason`(`verify/plan_gate.py:31-33`,`_ = reason`);
`# noqa: ARG001`(`verify/plan_gate.py:25,35`)抑制的规则**不在** ruff `select` 中(空抑制)。

### D-6【轻】type-ignore 掩盖真实类型

`tests/agent/test_reasoning_channel.py:68` 的 `config: object` + `# type: ignore[arg-type]`,
及 `:119-120` 的 `# type: ignore[attr-defined]`(因 `_run` 返回 `object`)。

### D-7【轻】外观噪音

多余空行:`api/app.py:123-124`(三连)、`verify/loop.py:118-119`、`verify/report.py:94-95`。

---

## 3. 修复计划

| 编号 | 修复 | 影响面 |
|---|---|---|
| S-1 | session 延迟创建:拒绝路径零写入 + 测试补断言 | `verify/loop.py`、`store/session.py`、`tests/verify/test_plan_approval_loop.py` |
| S-2 | 计划完整落盘并在 resume 读回 | `verify/loop.py`、`store/models.py`(phase data)、loop 测试 |
| S-3 | 计划门事件由 gate 单点发出(loop 不发) | `verify/loop.py`、`api/tasks.py`、`api/app.py`、前端/测试 |
| S-4/S-5/S-6 | 文档/注释澄清,不改行为 | `docs/10-m10.md`、本文件 |
| D-1 | 抽 `_resolve_pending` / 统一 CLI 提示 / 共享 Vue 外壳 | `api/app.py`、`cli/main.py`、Vue |
| D-2/D-3/D-4 | `ApprovalKind` 字面量类型 + 修正字段语义 + 传 `Plan` | `api/tasks.py`、`api/app.py`、`sse.ts` |
| D-5 | 去掉死参数与空 noqa | `verify/plan_gate.py` |
| D-6/D-7 | 补类型标注、清空行 | 测试、`api/app.py`、`verify/loop.py`、`verify/report.py` |

> 修复过程与复测结果追加至本文末「修复记录」一节,并同步 `M10_PROGRESS.md`。
> **提交**:`7b787bd`(2026-09-10)。

---

## 4. 修复记录(2026-09-10)

修复顺序:**先 Spec(3 处安全保证)→ 再 Standards(收敛重复/坏味)→ 每步复测**。
每接入一个改动即跑相关测试;全部完成后跑 §1 的完整口径。

### 4.0 基线对比(修复前基线)

`git stash` 后跑 §1 命令即修复前口径:pytest **458 passed** / vitest **32** / ruff / mypy / vue-tsc 全绿。
即**五项检查修复前后都绿** —— 本审计发现的 3 处 Spec 问题**全部逃过了既有测试**,
这正是本次审计的价值所在(测试绿 ≠ 保证成立)。

### 4.1 Spec 轴修复

| 编号 | 修复 | 落点 | 回归测试 |
|---|---|---|---|
| S-1 | **目录延迟创建**:`SessionStore` 构造/`continue_session`/`records()` 零副作用,只在首次 `_append` 时 `mkdir`。`verify/loop.py` 把 store 的**落盘**推迟到门通过后的 `_open_session`,第 0 步只只读读记录;拒绝路径不再写 `plan_rejected` phase(写即产生文件) | `store/session.py`、`verify/loop.py` | `test_store.py::test_s3b_read_path_creates_no_directory`;`test_plan_approval_loop.py` 增 `.git/whycode-sessions` 目录断言 |
| S-2 | `plan_approved` phase 落盘**完整计划**(files+commands);新增 `_plan_from_records()` 在 resume 读回并**注入复用**,`_resolve_plan()` 统一「复用/注入/Planner 生成」三分支;resume 用 `resumed = bool(records) or plan_approved` 判定(空目录/空文件不算会话) | `verify/loop.py` | `test_resume_reuses_approved_plan_not_regenerates`(空 transport 证明不重跑 Planner);`test_approve_runs_loop_and_records_phase` 增 commands 断言 |
| S-3 | 去掉 `VerifiedLoop` 里的 `plan_approval` 事件(它没有 `approval_id`),事件改由 `api/tasks.py:set_awaiting_plan_approval` **单点发出**;`StepKind` 移除该字面量 | `agent/loop.py`、`verify/loop.py`、`api/tasks.py` | `test_plan_approval_api.py` 断言历史里 `plan_approval` **恰好一条**且带 `approval_id` |
| S-4 | 空计划绕过计划门:确认为**已知边界**,补注释 + `docs/10-m10.md` 声明(不改行为,避免 W6 只读分流回归) | `verify/loop.py`、`docs/10-m10.md` | — |

### 4.2 Standards 轴修复

| 编号 | 修复 | 落点 |
|---|---|---|
| D-1 | 抽 `_await_human_approval`(`api/app.py`,两类浏览器审批共用);抽 `_resolve_approval`(两审批端点共用);抽 `cli/main.py:_confirm`(两类 CLI 提示共用);动作区样式抽到 `web/src/styles/approval-buttons.css`(两面板共用) | `api/app.py`、`cli/main.py`、`web/src/**` |
| D-2 | 新增 `ApprovalKind`/`TaskStatus` 字面量类型;`kind`/`status` 不再裸字符串 | `api/tasks.py` |
| D-3 | 计划不再以 `tool_name="plan"` 冒充工具名:`_PendingApproval.plan` 承载被审 `Plan` 对象,`tool_name` 对计划审批为 `None` | `api/tasks.py`、`api/app.py`、`sse.ts`、`TaskList.vue`、`test_approvals_api.py` |
| D-4 | `files`+`commands` 的数据块改为传 `Plan`(SSE payload 仍展开为 files/commands,前端契约不变) | `api/tasks.py`、`api/app.py` |
| D-5 | 去掉 `reject_plan_gate(reason)` 死参数与两条空 `# noqa: ARG001` | `verify/plan_gate.py` |
| D-6 | `test_reasoning_channel.py` 改回真实类型(`LLMConfig`/`AssistantMessage`),删除 3 处 `# type: ignore` | `tests/agent/test_reasoning_channel.py` |
| D-7 | 清除三处多余空行 | `api/app.py`、`verify/loop.py`、`verify/report.py` |

### 4.3 复测结果(修复后)

| 命令 | 修复前 | 修复后 |
|---|---|---|
| `uv run pytest` | 458 passed | **460 passed**(+2) |
| `npx vitest run` | 32 passed | **32 passed** |
| `uv run ruff check .` | ✅ | ✅ All checks passed |
| `uv run mypy ...`(63 文件) | ✅ | ✅ Success: no issues found |
| `npx vue-tsc --noEmit` | ✅ | ✅ 无输出 |

**结论**:修复**在任何一项检查上都没有引入回归**;而修复前的「五项全绿」并不代表保证成立
—— S-1/S-2/S-3 三处安全/规格问题都逃过了这套测试(见 §4.0)。

**过程中自引入并修复的缺陷(记录以免重犯)**:

1. `if self.last_resumed and records:` 一度被误改为 `if resumed_approved:`,导致「无计划门的
   常规恢复」(崩溃/失败后 resume)不走预验证、重放模型 —— 被 `test_verified_loop_resume.py`
   三条用例与 `test_eval.py` 一条用例捕获,改为 `resumed = bool(records) or plan_approved`。
2. `_resolve_plan` 初版读 `self.store`(step 0 已不再赋值)—— 自查发现,改为显式传入 `records`。
3. mypy `union-attr`:store 由可选属性改为 `_open_session` 的局部返回收窄。

### 4.4 未处置项(有意保留)

- **S-6 范围蔓延**(`LEARNING_PLAN.md`、`PHASED_PLAN.md`、`PROJECT_VERIFICATION.md §3`):
  与 M10 三个软点无关,但属文档改动,留待 commit 时分组说明,不在此回退。
- **S-5** `tests/policy/test_plan_approval.py` 命名:覆盖已实质存在于
  `tests/verify/test_plan_approval_loop.py`,不另建文件(规范措辞已在 `docs/10-m10.md` 对齐)。
