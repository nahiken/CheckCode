# M4 Review 记录(双轴 code-review)

> 审查范围:`git diff dff4524...HEAD`(M4 核心提交 `32cb4d9`)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 修复提交:`1000a74`(一轮)+ `(待补)`(二轮)
> 修复后验证:**233 tests passed**,ruff/mypy(strict,72 文件)全绿,真实 DeepSeek 恢复验证通过

## 二轮 Review(M4 review-fix 范围 `32cb4d9...HEAD`,修复提交)

> 双轴复审 review 修复提交(`1000a74`)。双轴一致确认一个真 bug + 若干边缘。

### Spec 轴(2 项)

| # | 类型 | 描述 |
|---|------|------|
| S5' | wrong(**真 bug**) | **last_event 单槽错配**:`_record_call` 复用 `runner.last_event`,但 AgentLoop 在 execute_partitions 全部返回后才逐条 `_record_call`,`last_event` 只存最后完成的调用 → 同一步骤多工具时,除最后一个外所有 ToolCallRecord 的审批字段错配(被拒工具 A 记为 granted=true)。修复:**audit 随 AgentResult 透传**(`_finish` 附 event.model_dump()),每个工具拿自己的事件;删 last_event |
| S6' | wrong(minor) | **test_v1 断言空洞**:`"mode":"normal"` 是 pydantic 默认值恒序列化,`"tool_name":"write_file"` 修复前已有——P1 回归保护虚设。修复:断言 `"granted":true` + `"decision":"permit"`(真实 dispatch 落盘值)。另 P3 的 role=tool 消息未截断(超长输出进 session) → 抽 `truncate_for_store` 严格截断 |

### Standards 轴(2 项判断)

| # | 气味 | 描述 | 修复 |
|---|------|------|------|
| S7' | Divergent Change(judgement) | `_default_path` 与 create 公式不一致(8 vs 10 字符),docstring 声称「同公式」不实 | 统一:分支名 `removeprefix("sess-")[-10:]` 与 create 同源 |
| S8' | 私有导入(judgement) | `from tools.base import _TRUNC_SUFFIX` | 抽 `truncate_for_store` 收拢严格截断,调用方不再私有导入 |

### 二轮验证

- 双轴一致确认 audit 透传修复正确(每个工具自己的审批现场,不再错配)。
- 修复后 **233 tests passed**,ruff/mypy(strict,72 文件)全绿。

## 一轮 Spec 轴发现(3 项,均为真 bug)

| # | 类型 | 规格依据 → diff 位置 | 描述 |
|---|------|----------------------|------|
| P1 | wrong(**真 bug**) | design-M4 验收点1「审批边界全部持久化」、LEARNING_PLAN 70-71 → `verify/loop.py` `_record_call` 只填基本字段 | **审批边界未落盘**:`ApprovalRecord` 定义了但从不写入,`_record_call` 写的 ToolCallRecord 不含 mode/decision/layer/asked/approved/granted。修复(一轮):`ToolRunner` 附 `event` 随 AgentResult 透传,`_record_call` 复用填审批字段。二轮 S5' 发现 last_event 单槽错配 → 改随 AgentResult.audit 透传(见二轮) |
| P2 | missing(真 bug) | design-M4 验收点4「逼近窗口上限时压缩」→ `context/compression.py` 定义了 `compress_history` 但 `agent/loop.py` 从不调用 | **上下文压缩未接线**:只有 `ContextWindow.observe` 记账,没有实际压缩。修复:`_call_model` 在 `len(messages) > window_size` 时压缩早期历史(成对剔除旧回合) |
| P3 | wrong(真 bug) | design R1「已完成→干净重放」→ `verify/loop.py` `_persist_event` 不处理 tool_result | **tool 结果消息不落盘**:store 永无 role=tool 消息,`recover_events` 的 `tool_ids_with_result` 恒空 → 所有已完成写被误判 pending。修复:`_record_call` 补 `MessageRecord(role="tool")`;回归测试 `test_v1` 断言 `"role":"tool"` 落盘 |

## Standards 轴发现(3 项硬违规 + 判断性)

### 硬违规(违背项目惯例)

| # | 气味 | 位置 | 成因 | 修复 |
|---|------|------|------|------|
| S1 | Duplicated Code(硬) | `store/effect.py` `_READ_ONLY_TOOLS` 硬编码 | 违反「工具分类单一来源」(tools_spec 已声明 read_only) | 改从 `build_tools` 注册表派生只读集合(lru_cache),不二次硬编码 |
| S2 | Duplicated Code(硬) | `verify/loop.py` `_wt_path_for` 与 `worktree/manager.create` 拼路径公式 | 路径公式散落 | `manager._default_path(branch)` 单一来源;`create_from_branch` 用同名公式(确定性锚点),`create` 保持随机后缀(唯一性) |
| S3 | Duplicated Code(硬) | `store/session.py` `record_tool_call` 重实现截断 | 复制 `tools/base.py truncate_output` 算法+常量 | 复用 `truncate_output`,留出后缀长度保证严格 ≤ 上限 |

### 判断性(已修)

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| S4 | Speculative Generality | `ApprovalRecord`(全项目无实例化)、`self._base_commit`(赋值未读)、`_build_agent_loop` 未用参数、`event_source` 死变量 | 删 ApprovalRecord(P1 改为 ToolCallRecord 承载审批边界);删 `_base_commit`;精简 `_build_agent_loop(runner)` |
| S5 | Mysterious Name | `agent/loop.py` 实例 `_emit` 与闭包 `_emit` 同名遮蔽 | 闭包改名 `_broadcast`(分发到 record_event + 调用方 events) |

## 已满足的规格点(不需修复)

- 恢复不重放副作用(复用分支 → 先重验证 → 通过则模型零调用)✓
- 超长工具输出落盘裁剪 ✓
- CLI resume(--session / 会话缺失报错)✓
- kill 后恢复 + 续跑完成(真实 DeepSeek 双重验证)✓
- 会话落盘位置:repo/.git/whycode-sessions/(worktree 外,git status 恒净)✓

## 修复后验证

- **233 tests passed**,ruff/mypy(strict,72 文件)全绿。
- 真实 DeepSeek:完整 run(24 行会话记录)+ resume(复用分支,重验证通过,模型零调用)。
- 回归测试:`test_v1`(审批字段 + tool 消息落盘)、`test_s8`(严格截断)。
