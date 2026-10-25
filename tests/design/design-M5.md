# M5 测试设计:MCP + Skills + 可靠性

> 工作流①产物(先设计再实现)。阶段 M5 按 `LEARNING_PLAN.md` 阶段 5 + `PHASED_PLAN.md` M5 里程碑。
> 设计目标:每项验收点先定测试(红)→ 实现(绿)→ 补边界。

## 1. 功能范围

### 1.1 MCP 客户端(`mcp_client/`)

官方 SDK(`mcp>=2.1.1`)包装,Stdio + Streamable HTTP 双协议,封装为对上层统一接口。

- **连接**:`MCPClient`(stdio 或 http)→ initialize 握手(携带 client_info)→ 工具发现
  (list_tools → 映射为 `McpTool(name, description, input_schema, server_label)`)。
- **调用**:`call_tool(name, args) -> str`(结果序列化:text/图像/结构化 content)。结果**截断**到
  `MAX_MCP_OUTPUT_CHARS`(默认 8000,复用 `truncate_for_store` 语义)。
- **可靠性**:
  - `health_check`:握手成功后 ping,可达返回 True;超时/异常返回 False。
  - **指数退避重试**:针对「可重试失败」(连接/握手/临时网络错误),`RetryPolicy(max_attempts=3, base_delay=0.05, max_delay=0.5)` 指数退避。
  - **超时业务调用不自动重放**:`call_tool` 超时(ReadTimeout)只抛错,**不重试**——与 M4 恢复语义衔接
    (超时意味着"可能已执行",自动重放会重复副作用)。`is_retryable` 判定器区分「连接层重试」与「业务调用不重试」。

### 1.2 MCP 工具接入 ToolRunner(`mcp_client/bridge.py`)

- `McpTool(Tool)`:Pydantic 参数模型(schema 由 MCP input_schema 转换)+ `read_only=False`(MCP 工具
  **默认非只读**,保守不可重放)+ 可选 `path_key`(规范 schema 中带 `path` 属性的工具 → 受路径沙箱约束)。
- `MCPToolRegistry`:多 server 工具的合并注册表(单一来源)。
- `McpToolHandler.async_call`:`await call_tool(...)`(MCP 是 async 的;AgentLoop dispatch 契约同步 Tool.run → 用 executor 包 async 调用)。
- **接入位置**:`ToolRunner.__init__(..., extra_tools=None)` 合并到 `self.tools`;
  `tools_spec_from` 已按 Tool 声明自动带 read_only/path_key(权限层不用改)。
- **权限/审计/落盘**:自动生效——MCP 工具走 ToolRunner 统一路径 → audit.before/after、M4 ToolCallRecord、拒绝边界。

### 1.3 Skills(`skills/`)

- **三级加载**:built-in(包内 `skills/builtin/`)/ project(`<repo>/.whycode/skills/`)/ user(`<user>/.whycode/skills/`),
  顺序 built-in → project → user(靠后者覆盖同名)。
- **SKILL.md frontmatter**:`---` 块解析 `name`/`description`/`allowedTools`(每行一个)。
- **Fork Skill 强制 allowedTools**:project/user 的 skill 未声明 `allowedTools` → **拒绝加载**(报依赖不足)。
- **依赖预检**:`allowedTools` 项必须以 `mcp:`/`skill:` 前缀命名(如 `mcp:filesystem`);依赖不满足 → 拒绝。
- `Skill`(name/description/allowed_tools/kind/source);`SkillRegistry.load_all(repo)` 聚合三级的可见 skill + 依赖状态。

## 2. 测试设计

### 2.1 `tests/mcp_client/`(单元,零网络,不启动真实 server)

**test_client_retry.py** —— 可靠性核心
- `RetryPolicy.should_retry(kind, attempt)`:连接层失败(ConnectionFailed)第 1/2 次重试,第 3 次(达 max_attempts)放弃
- `is_retryable(exc)`:ConnectionFailed → True;ReadTimeout/TimeoutError → **False**(超时不重放)
- `backoff_delay`:指数增长(0.05, 0.1, 0.2 → cap 0.5)
- `retry_async` 或策略驱动的调用:连接失败重试 N 次成功;持续失败抛最终错误

**test_client_handshake.py** —— 握手与发现(伪 MCP server,控制循环内联)
- `list_tools` 结果 → `McpTool` 元数据(name/description/input_schema/server 来源)
- initialize 返回 server capabilities 后 session 可用

**test_client_http_retry_removed.py** —— 超时不重放
- `call_tool` 抛 Timeout → 不重试,直接抛错(即便 RetryPolicy 配置为重试,超时也豁免)

**test_bridge.py** —— MCP 工具接入
- `McpTool.args_model` 由 input_schema 派生(required 字段)
- `McpTool.read_only == False` 默认、`path_key` 推导(有 `path` 属性)
- `MCPToolRegistry` 合并多 server;重名工具冲突 → 报错(拒绝歧义)

### 2.2 `tests/mcp_client/test_integration.py`(集成)

**stdio 真实 server**:启动一个最小 MCP server(`mcp` SDK 的 server 端,inline 定义 tools:
add/tolower/read_echo——echo 从给定 path 读文件)。断言:
- 连接 + initialize + 发现到 3 个工具
- `call_tool("add", {...})` → "3"
- `call_tool` 超时(server 端 sleep > timeout)→ 抛 Timeout,**不重放**(server 端计数未重复)
- `health_check` → True;`close()` 后进程终止

### 2.3 `tests/mcp_client/test_e2e.py`(端到端)

**ToolRunner 接入 + AgentLoop**:
- `ToolRunner(extra_tools=MCPToolRegistry([...]).build())` → `tools_spec` 含 MCP 工具
- MCP 写工具(strict + 计划命中)→ permit + 执行(MCP server 端落地)+ audit 落盘(granted/decision)
- MCP 写工具(strict + 未命中计划)→ deny,**server 端未执行**
- AgentLoop + MCP 工具:mock model 发起 `mcp:add` 调用 → 结果回灌 → 模型看到结果(全链路)

### 2.4 `tests/skills/`(单元)

**test_frontmatter.py**
- 合法 SKILL.md → Skill(name/description/allowed_tools)
- 缺 allowedTools → 空列表(保留;仅 project/user 级校验)

**test_loader.py** —— 三级加载
- built-in + project + user 各自目录 → 聚合;user 覆盖同名 project
- project/user 的 Fork Skill 缺 `allowedTools` → **拒绝加载**(报错含 skill 名)
- allowedTools 含 `mcp:xxx` / `skill:yyy` → 预检通过;含裸名字/`file:` → 拒绝

**test_registry.py**
- `SkillRegistry.load_all(repo)` → skills + 拒绝列表;`get(name)`
- 依赖校验:`resolve(skill, registry)` 检查 mcp/skill 依赖可满足

### 2.5 回归

- 全量 `uv run pytest` 保持全绿(既有 233 不回归);ruff/mypy(strict) 全绿。

## 3. 验收点对照(`PHASED_PLAN.md` M5)

| 验收点 | 测试 | 结果 |
|--------|------|------|
| 官方 SDK 客户端 Stdio+HTTP;initialize 握手、工具发现 | test_integration(stdio)+ test_client_handshake | ✅ |
| 超时/健康检查/指数退避;**超时业务调用不自动重放** | test_client_retry + test_client_http_retry_removed | ✅ |
| 接外部 MCP server 发现并调用工具 | test_integration + test_e2e(ToolRunner 接入) | ✅ |
| skills 三级加载 | test_loader | ✅ |
| Fork Skill 强制 allowedTools + 依赖预检 | test_loader(拒绝)+ test_registry(预检) | ✅ |
| 技能缺失依赖被拒 | test_loader/test_registry | ✅ |

## 4. 设计要点

1. MCP 与普通 HTTP 工具的本质区别:标准协议 initialize 握手 + 工具发现 + 生命周期/恢复语义;三方 server 工具被权限层/审计层统一管理。
2. 指数退避与"超时不重放":连接层重试(幂等安全)vs 业务调用(可能已执行)重试——副作用边界与 M4 恢复语义一脉相承。
3. Skill 依赖校验:Fork Skill 强制声明 allowedTools,依赖(mcp:/skill:)预检,缺失即拒。
