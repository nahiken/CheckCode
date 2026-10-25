# M5 Review 记录(双轴 code-review)

> 审查范围:`git diff 642324b...HEAD`(M5 核心提交 `2e43b33` + review 修复 `f853d1b`)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 修复提交:`f853d1b`
> 修复后验证:**278 tests passed**,ruff/mypy(strict,85 文件)全绿,真实 stdio + streamable-http MCP server 双重验证

## Spec 轴发现

### 真缺口(验收点落空)

| # | 类型 | 规格依据 | 描述 |
|---|------|---------|------|
| P1 | missing(真 bug) | PHASED_PLAN「技能缺失依赖被拒」→ `skills/loader.py` | **依赖预检未接线**:`resolve_dependencies` 定义了但 `load_all` 从不调用 → 声明 `allowedTools: [mcp:not_registered]` 的 project/user skill 照样加载,验收点落空。修复:`load_all` 接收 `registered_mcp`,project/user 级缺依赖 → **拒绝加载** |
| P2 | wrong(真 bug) | design §1.1「连接/握手按指数退避重试」→ `mcp_client/retry.py` | **握手超时不走退避**:`_connect_once` 把 `initialize()` 包进 `with_timeout` → 抛 `McpCallTimeout`(is_retryable=False),绕开 `call_with_retry`。连接错误重试、握手超时不重试,分类自相矛盾。修复:`call_with_retry(retry_timeouts=True)` 仅连接阶段允许超时重试(连接无业务副作用,幂等安全) |
| P3 | missing(真缺口) | design §2.1「Streamable HTTP 零测试」 | **HTTP 协议零测试**:`streamable_http_client` 分支存在但全部测试仅覆盖 stdio。修复:新增 `TestHttpConnection`(真实 streamable-http server,握手/发现/调用/超时不重放) |
| P4 | missing(真缺口) | design §1.3「内置(包内 skills/builtin/)」 | **内置级 Skills 未落地**:无 `skills/builtin/`,`builtin_dir` 默认 None。修复:新增 `skills/builtin/explain/SKILL.md` + 测试确认内置级加载 |

### 确认成立(不需修)

- **超时不自动重放端到端成立**:`call_tool` 超时→`McpCallTimeout`→bridge `async_run` 转 error `ToolResult`→runner 返回 `is_error`。全链路无重试路径。
- **MCP 工具走完整权限/审计链**:schema→policy→approval→`audit.before/after`,e2e 验证 denied 不执行、granted 落盘。
- 轻微 scope creep:`frontmatter.py` 支持引号/逗号分隔/续行等 YAML 变体(超规格,无害)。

## Standards 轴发现

### 硬违规(违反既有约定)

| # | 气味 | 位置 | 描述 | 修复 |
|---|------|------|------|------|
| S1 | 超时多源 | `tools/runner.py` async 分支 | async 工具直接 `await async_run()` 无 `asyncio.wait_for` → `tool_timeout`(120s)失效;MCP 工具自身 `timeout`(30s)旋钮 → 三套超时并存 | runner 统一 `tool_timeout` 兜底(async/sync 两路径);删 `McpTool.timeout` |
| S2 | Mysterious/死代码 | `mcp_client/client.py` `_process` | 从不赋值的字段,`close()` 里仅置 None | 删除 |
| S3 | 误用异常语义 | `call_tool` 未连接抛 `ConnectionFailed` | ConnectionFailed 是 retryable,调用方若用 `call_with_retry` 包装会无意义重试 | 保持(未连接是确定性错误,retry 无害但无意义)——改为不重试语义,由上层直接处理 |

### 判断性(已修)

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| S4 | Duplicated Code | `frontmatter.py` `_parse_list_item`/`_strip_quotes` 重复引号剥离 | 收拢 `_strip_quotes` 复用 |
| S5 | 命名混用 | `MCPClient`/`McpTool`(MCP vs Mcp 大小写) | 统一 `Mcp*` 命名 |
| S6 | 冗余别名 | `MAX_MCP_OUTPUT_CHARS = MAX_OUTPUT_CHARS` | 删除,直接引用 base 常量 |
| S7 | assert 守卫 | `client.py` `_connect_once` 的 `assert command is not None` | 改用显式 raise(兼容 `-O`) |

## 修复后验证

- **278 tests passed**(M5 新增 45:16 单元 retry/bridge + 8 stdio 集成 + 2 HTTP 集成 + 4 e2e + 15 skills),ruff/mypy(strict,85 文件)全绿。
- 真实 stdio + streamable-http MCP server 双重验证:握手/发现/调用/健康检查/超时不重放。
- 技能缺失依赖被拒(验收点):`test_missing_dependency_gates_loading`。
