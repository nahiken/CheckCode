# M10 Review 记录(双轴 code-review)

> 审查范围:推理通道配置 + 计划审批门(CLI/Web/Vue)+ 回滚覆盖。
> 审查方式:实现后自查 + 二轮对照 `progresses.md` 审计软点;此处汇总为
> Spec(规格符合度)+ Standards(编码规范)双轴记录。审查时间:2026-09-10。

## Spec 轴(规格符合度)

### (a) 与审计软点的对照(`progresses.md` §二)

| 审计软点 | 条文 | 验证 |
|---|---|---|
| #2「严格模式…生成计划**并审批**」不成立 | 计划生成但无审批端点/门/UI | ✅ `verify/plan_gate.py` + `verify/loop.py` 门插在 worktree 创建之前;CLI `_make_plan_gate`;Web `_make_plan_gate` + `POST /plan-approvals/{aid}` + `PlanApprovalPanel.vue` |
| #1 thinking 默认恒空 | 代码存在、默认不产生 | ✅ `LLMConfig.supports_reasoning` + `LLM_REASONING`;客户端推理模型省略 temperature;聚合链路已有 |
| #3 回滚无测试 | `force_branch` 零覆盖 | ✅ `tests/worktree/test_worktree_manager.py` +2(重置生效 / 活动 worktree 报错) |
| #3 测试命令非推断 | 来自 plan 声明/注入 | ⏸ 未改:属**有意设计**(命令由计划声明,非自动发现);措辞收窄即可 |
| #3 闭环内不自动回滚 | rollback 仅 API 调 | ⏸ 未改:**有意保留**——自动回滚会毁掉 RiskReport 依赖的证据 |

### (b) 关键语义决策

| 决策 | 理由 |
|---|---|
| 门插在**计划解析之后、worktree 创建之前** | 拒绝时零 worktree/零分支/零写入——这是「先审批后动手」的硬保证 |
| 拒绝 → `plan_rejected=True` 且 `passed` 恒真 | 拒绝是**用户主动的安全决策**,不是任务失败 |
| 计划门用**独立 SSE 事件** `plan_approval` + 独立状态 `awaiting_plan_approval` | 复用 `approval` 会破坏 `tests/api/test_approvals_api.py` 与 27 vitest 的既有断言;两事件语义不同(粗/细) |
| 两个 gate **并存**(计划门 + 逐工具门) | 不加「计划内写跳过第二道门」捷径——会削弱 ToolRunner 逐调用审批边界 |
| resume 复用 `plan_approved` phase,不重复询问 | 与「不重放已完成副作用」同源;spy gate 断言调用数不增 |
| `auto_approve=True` → 计划门直接放行 | 保持自动化路径(tests/smoke_real)不阻塞 |

## Standards 轴(编码规范)

### 修复过的问题

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| S1 | 误删 `_new_session_id` 函数头(编辑时把 def 行并入上一块) | `api/app.py` | 立即发现并恢复 def + docstring |
| S2 | `StepEvent.kind` 是受限 `Literal`,新增事件需登记 | `agent/loop.py:42` | 注册 `"plan_approval"`(mypy 捕获) |
| S3 | `api/tasks.py` 局部函数 `list()` 遮蔽内置 → 新签名 `list[str]` 报 valid-type | `api/tasks.py:211` | 签名改 `Sequence[str]`,加 `from collections.abc import Sequence`(mypy 捕获) |
| S4 | `render()` 三目嵌套行超长(E501) | `verify/report.py:110` | 拆成 `if/elif/else` 赋 `verify_state`(更可读) |

### 测试契约修正(非生产代码 bug)

| # | 现象 | 处置 |
|---|------|------|
| T1 | `pending_summary` 新增 `kind` 字段 → `test_list_contains_awaiting_and_pending` 精确 dict 比较失败 | 更新测试期望(加 `kind: "tool"` 与 `pending_plan_approvals == []`),属**有意加字段**的约定同步 |
| T2 | `force_branch` 对**活动 worktree 检出中的分支**报 `git branch -f` 失败 | 生产 rollback 在 loop **拆除 worktree 之后**调用(此时分支已 free);按真实契约重写测试:先 `remove(keep_branch=True)` 再 `force_branch`;另加「活动 worktree → 报错」负例 |

## 总结

- **Spec**:关闭审计 §二 的 #2 计划未审批、#1 thinking 空、#3 回滚无测试三项软点;两项有意保留(测试命令来源、闭环内不自动回滚)。
- **Standards**:4 处修复(S2/S3 由 mypy 捕获,S4 由 ruff 捕获,S1 自查捕获)、2 处测试契约同步。
- 教训:`StepEvent.kind` 与 `DecisionLayer` 同属「新值须登记」的受限字面量——新增事件/层时 mypy 会兜底,
  但**先看类型别名再写**能省一轮。

---

## 三轮:第三方双轴审计(2026-09-10)

实现完成后交给**新会话**做了一次独立双轴 code-review(见 `AUDIT_HANDOFF_M10.md` 交接单)。
发现与修复的完整记录在 **`tests/reviews/audit-M10.md`**;此处只记结论。

### 关键发现(自查未覆盖的盲点)

自查只验了「有没有做」,审计验了「**声称的安全保证是否成立**」——三处不成立:

| # | 声称 | 实际 | 性质 |
|---|---|---|---|
| S-1 | 拒绝 → **零 session 文件** | 门之前 `continue_session()` 已 `mkdir`,拒绝路径留下 `.git/whycode-sessions/` 目录;自证测试只写了断言注释、**没真断言** | 规格违反 |
| S-2 | resume **复用计划**、不重复询问 | 只复用「已批准」布尔量,计划仍由 Planner **重新生成** —— 可能执行从未审批的新计划 | 规格不符 |
| S-3 | 计划审批一个 SSE 事件 | loop 与 tasks **各发一个**;loop 那个没有 `approval_id`,前端取到它 → 面板空 id → 点击 404 | 实现缺陷 |

### 修复要点

- **教训(最重要)**:「拒绝零副作用」这类**安全断言**,测试必须断言**文件系统/进程状态**,
  而不是在 docstring 里写一句就完事。S-1 的自证测试原样保留了「零 session 文件」的措辞,
  却在断言里悄悄漏掉了它 —— 测试文本与实际覆盖脱节,比没写测试更危险(会给出虚假信心)。
- **状态机的「兄弟分支」要成对改**:`resumed` 判定从 `records` 改为 `plan_approved` 时,
  漏掉了「无计划门的常规恢复」分支,被 `test_verified_loop_resume.py` 捕获。
  改状态判定前先 grep 全部**读**该状态的调用点。
- **收敛重复**:`approval` 与 `plan_approval` 两套机制在 API/CLI/Vue 三处各写了一遍,
  审计后抽成 `_await_human_approval` / `_resolve_approval` / `_confirm` / 共用 CSS。
  坏味集中在「有意的同构复制」上,下次连写两份时就该先抽公共骨架。

### 修复后口径

pytest **458 → 460**、vitest 32、ruff / mypy strict / vue-tsc 全绿。详见 `audit-M10.md` §4。
commit **`7b787bd`**(44 files, +3671/-130)。
