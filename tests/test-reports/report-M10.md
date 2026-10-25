# M10 测试评估报告

> 阶段:M10(推理通道配置 + 计划审批门 + 回滚覆盖)。评估时间:2026-09-10。
> 测试口径:CI 全离线 mock(不跑真实 LLM/Docker/浏览器/外部 MCP);本报告只记本地确定性套件。

## 测试规模

### Python(pytest)—— 427 → 458(+31)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/agent/test_reasoning_channel.py` | +13 | LLM_REASONING 解析(真/假值参数化);推理模型省略 temperature、非推理保留;流式 reasoning_content 聚合端到端 |
| `tests/verify/test_plan_approval_loop.py` | +6 | 拒绝→零 worktree/零分支/零 session/零命令执行;批准→正常闭环 + plan_approved phase;门收到计划透传;resume 不重复询问(spy 调用数);默认关闭不生效 |
| `tests/api/test_plan_approval_api.py` | +6 | 计划审批流(approve→done/deny→plan_rejected);端点 200/404/未知任务 404;awaiting_plan_approval 时 resume 拒绝 |
| `tests/worktree/test_worktree_manager.py` | +2 | force_branch 重置生效(分支回基线、diff 清空、主树不动);活动 worktree 上 force_branch 报错 |
| `tests/api/test_approvals_api.py`(改) | 0 | `pending_summary` 加 `kind` 字段契约同步 |
| **累计** | **+31** | **458 passed** |

### TypeScript(vitest)—— 27 → 32(+5)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `web/tests/planapproval.test.ts` | +5 | PlanApprovalPanel 渲染计划文件/命令;批准→POST plan-approvals;拒绝→approve=false;App 收 plan_approval 事件→状态/面板 |
| `web/tests/msw-handlers.ts`(改) | 0 | 加 plan-approvals handler |
| **累计** | **+5** | **32 passed** |

## 关键语义(测试锁定)

- **计划门零副作用**:拒绝时断言主仓库 `git status` 干净、HEAD 未动、无残留 worktree、
  分支未创建、Agent 未被调用(空 MockTransport 证明未进入写阶段)。
- **resume 不重复询问**:同一 session 第二次 run 时 spy gate 调用数不增(复用 `plan_approved` phase)。
- **回滚契约**:`force_branch` 须在 worktree 拆除(keep_branch=True)后调用;
  对活动 worktree 检出的分支会报错——测试按此真实契约编写。

## 关键指标

- **458/458 pytest、32/32 vitest 通过**;`ruff check .`、mypy strict(63 源文件)、vue-tsc 全清。
- 推理通道:仅验证「配置 + 聚合链路」;真实推理模型输出 `reasoning_content` 的端到端留
  `WHYCODE_SMOKE_REAL=1`(且注意部分推理模型无 Function Calling,见 docs/10)。

## 结论

M10 关闭审计 §二的 3 项软点(计划未审批、thinking 默认空、回滚无测试),测试 427→**458**、
27→**32** 全绿,三条静态检查干净。计划审批门在**任何写入之前**提供粗粒度人工决策,
与既有逐工具门正交;拒绝为零副作用的安全默认。
