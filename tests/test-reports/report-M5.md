# M5 测试评估报告

> 阶段:M5 MCP + Skills + 可靠性(mcp_client/ + skills/)。
> 审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`2e43b33`(M5 核心)+ review 修复 `f853d1b`。

## 测试规模

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/mcp_client/test_retry.py` | 9 | 退避指数/封顶/尝试上限;连接失败可重试/超时不可重试/未知不可重试;重试后成功/到限放弃/超时单次 |
| 单元 | `tests/mcp_client/test_bridge.py` | 7 | input_schema→参数模型(required);read_only/path_key 默认;多 server 合并/重名拒绝 |
| 集成 | `tests/mcp_client/test_client_integration.py`(stdio 6 + http 2) | 8 | stdio 握手/发现/调用/健康检查/超时不重放/close 幂等/命令缺失报错;**HTTP 握手/发现/调用/超时不重放** |
| 端到端 | `tests/mcp_client/test_e2e.py` | 4 | ToolRunner 合并 MCP 工具;计划命中放行+审计落盘;计划未命中拒绝(server 端未执行);AgentLoop 全链路 |
| 单元 | `tests/skills/test_frontmatter.py` | 6 | 完整/最小/无 frontmatter/缺 name/body 保留/引号变体 |
| 单元 | `tests/skills/test_loader.py` | 11 | 三级加载+user 覆盖;缺 allowedTools 拒绝;**依赖预检 gate 加载**;内置级落地;resolve_dependencies |
| **合计** | **5 文件** | **45** | M5 新增 |

全项目累计:**278 tests passed**(M4 233 + M5 45),ruff/mypy strict 85 文件无问题。

## review 修复(双轴,4 真缺口 + 3 硬违规 + 4 判断)

- **P1 依赖预检未接线**(验收点落空)→ `load_all` 接收 `registered_mcp`,project/user 级缺依赖拒绝加载
- **P2 握手超时不走退避** → `call_with_retry(retry_timeouts=True)` 仅连接阶段允许超时重试
- **P3 Streamable HTTP 零测试** → 新增 `TestHttpConnection`(真实 server)
- **P4 内置级 Skills 未落地** → `skills/builtin/explain/SKILL.md` + 加载测试
- **S1 async 工具无 runner 级超时**(tool_timeout 失效)→ runner 统一兜底 + 删 `McpTool.timeout`
- **S2/S5/S6/S7** 死字段删除、命名统一、冗余别名删、assert 改显式 raise

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| 官方 SDK 客户端 Stdio+HTTP;initialize 握手、工具发现 | `test_connect_handshake_and_discover` + `TestHttpConnection` | ✅ 双协议真实 server |
| 超时/健康检查/指数退避;**超时业务调用不自动重放** | `test_retry` + `test_timeout_not_replayed`(stdio+http) | ✅ 超时单次不重试 |
| 接外部 MCP server 发现并调用工具 | `test_e2e`(ToolRunner 接入 + AgentLoop) | ✅ 权限/审计/落盘全链 |
| skills 三级加载 | `test_three_levels_merged_user_overrides` | ✅ 靠后覆盖同名 |
| Fork Skill 强制 allowedTools + 依赖预检 | `test_fork_*_rejected` + `test_missing_dependency_gates_loading` | ✅ 缺失即拒 |
| 技能缺失依赖被拒 | `test_missing_dependency_gates_loading` | ✅ |

## 关键指标

- 修复后回归:278/278 通过,零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,85 source files。
- 真实 MCP server 双重协议验证:stdio(子进程管道)+ streamable-http(127.0.0.1 随机端口)。

## 结论

M5 全部验收点通过。MCP 官方 SDK 双协议客户端(握手/发现/调用/健康检查/退避/超时不重放)、
MCP 工具接入 ToolRunner 全权限审计链、Skills 三级加载 + Fork 强制 allowedTools + 依赖预检
均有测试 + 真实 server 双重证据。下一里程碑 M6(Multi-Agent Mailbox)。
