# M4 测试评估报告

> 阶段:M4 JSONL 持久化 + 会话恢复(store/ + context/ + 恢复闭环)。
> 审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`32cb4d9`(M4 核心)+ review 修复。

## 测试规模

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/store/test_store.py` | 18 | JSONL 落盘/读回/损坏容忍/跨进程继续/工具记录/overridden/裁剪;副作用判定;恢复推导(全/读/写/覆写/复合);恢复上下文 |
| 单元 | `tests/context/test_compression.py` | 5 | 窗口内原样/超限成对压缩/可序列化/配对完整性/窗口记账 |
| 集成 | `tests/agent/test_loop_session.py` | 3 | session_id 注入/record_event 回调/可重入 run |
| 集成 | `tests/verify/test_verified_loop_resume.py` | 5 | store 落盘/resume 复用分支/不重放写/store 可见/超长裁剪(含 P1/P3 回归断言) |
| 集成 | `tests/test_cli.py`(新增 2) | 2 | resume 需 --session/会话不存在报错 |
| **合计** | **5 文件 +2** | **33** | M4 新增 |

全项目累计:**233 tests passed**(M3 201 + M4 32),ruff/mypy strict 72 文件无问题。

## review 修复(双轴,3 真 bug + 3 硬违规 + 二轮 1 真 bug)

- **P1 审批边界未落盘** → audit 随 AgentResult 透传(一轮 last_event,二轮 S5' 改随结果透传,修多工具错配)
- **P2 上下文压缩未接线** → `_call_model` 超窗压缩
- **P3 tool 结果消息不落盘** → `_record_call` 补 role=tool 消息(严格截断 via `truncate_for_store`)
- **S1** effect 单一来源化;**S2** 路径公式收敛 manager(二轮统一分支后缀);**S3** 复用 truncate_output
- **S4/S5** 删死代码(ApprovalRecord/_base_commit)、`_emit` 遮蔽改名 `_broadcast`
- **S5'**(二轮)last_event 单槽错配 → audit 随 AgentResult 透传;test_v1 断言空洞 → 钉 `granted:true`+`decision:permit`

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| JSONL 落盘消息/工具调用/运行状态/审批边界 | `test_v1_store_landed_after_run` | ✅ header/message/tool_call/phase 全落盘 |
| 恢复不重放已完成副作用 | `test_v3_no_replay_of_completed_write` | ✅ resume 复用分支重验证通过,**模型 handler 零调用** |
| 恢复推导边界(只读重放/写跳过/覆写剔除) | `test_r1~r5` + `test_build_resume_seed_overridden_excluded` | ✅ 全场景 |
| 超长工具输出落盘裁剪 | `test_s8` + `test_v5` | ✅ 含后缀不超上限 + truncated 标记 |
| 上下文压缩(成对剔除) | `test_c2/c4` | ✅ assistant+tool 结果成对剔除,system/user 保留 |
| CLI resume | `test_cli_resume_*` | ✅ 需 --session;会话缺失报错退出 1 |
| **验收场景:kill 进程 → 恢复 → 不重放写 → 继续完成** | `test_v2/v3` + 真实 DeepSeek | ✅ 见下 |

## 真实 LLM 端到端验证

两次真实 DeepSeek 调用成功:

1. **完整 run**(`--auto-approve`):Agent read_file → write_file(修复 divide/fib)→ 真实
   pytest 通过(1 轮,1515ms)→ 会话 24 行记录(header/phase/message/tool_call 全落盘)。
2. **resume**(`--session sess-b456fb8db9 --auto-approve`):复用既有分支 → 重验证通过
   (954ms)→ **模型未被再次调用**(无文本/无工具调用)→ 写操作零重放 → 报告完整。

## 关键指标

- 修复后回归:233/233 通过,零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,72 source files。
- 会话记录落盘位置:repo/.git/whycode-sessions/(worktree 之外,git status 恒净)。

## 结论

M4 全部验收点通过。JSONL 持久化、恢复不重放副作用、超长输出落盘裁剪、上下文压缩、
CLI resume 均已实现并有测试 + 真实调用双重证据。恢复语义的**核心**:分支即会话锚点,
已完成写提交到分支,恢复基于现状继续而非重做。下一步 M5(MCP + Skills + 可靠性)。
