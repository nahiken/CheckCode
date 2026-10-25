# WhyCode — 安全可控的 Coding Agent 工程工作台

面向本地代码仓库的安全型 Coding Agent 与浏览器协作控制台。针对 AI 修改代码时的
**越权执行、验证缺失、长任务中断、过程不可追踪** 四个问题，构建受控开发闭环：

```
需求分析 → 计划/权限审批 → Git Worktree 隔离修改 → 自动验证 → 风险报告 → 会话恢复
```

## 技术栈

Python 3.11 · asyncio · OpenAI 兼容流式 API · FastAPI · Pydantic · Git Worktree ·
MCP SDK · JSONL · Vue 3 · TypeScript · pytest · Vitest · Docker Compose · Nginx

## 核心能力

- **ReAct Agent Runtime**：手写流式 Chat Completions / Anthropic Messages 双协议客户端，
  聚合文本、思考、工具参数与 usage，处理参数跨分片截断。
- **受控工具执行**：文件路径沙箱、Allow–Deny–Ask 权限策略、运行时工具白名单、
  两段式 Hook 审计；写操作越过边界时请求人工审批。
- **可验证的修改闭环**：Git Worktree 隔离改动 → 跑测试 → Diff/风险报告 → 失败可回滚。
- **长任务恢复**：JSONL 持久化、会话 resume、副作用判定与 Trace 导出。
- **多 Agent 协作**：跨进程邮箱（OS 文件锁）、Agent 间寻址消息、并发安全的只读工具并发。
- **工作台**：FastAPI + SSE + Vue 3 审批面板、恢复、计划审批门；另有 Textual 终端工作台。
- **评测与交付**：固定任务集与指标汇总、CLI/wheel、Docker Compose 全栈。

**安全默认**：任务的 `auto_approve` 默认为 `false`。严格模式下计划先交人工审批；
普通模式下写操作越权同样请求确认。自动化路径（冒烟脚本/CI）需显式开启自动放行。

## 快速开始

```bash
# 1. 安装依赖(含开发依赖)
uv sync --all-extras

# 2. 配置模型(不要提交 .env)
cp .env.example .env
# 编辑 .env,填入 DEEPSEEK_API_KEY(或 ANTHROPIC_API_KEY)

# 3. 运行
uv run mewcode --help
uv run mewcode-tui      # 可选:终端工作台(需 textual extra)

# 4. 测试与检查
uv run pytest
uv run ruff check .
uv run mypy agent
```

## 配置

- 模型与环境变量：复制 [`.env.example`](.env.example) 为 `.env` 后填写；`.env` 已被 `.gitignore` 忽略。
- 装配配置：复制 [`whycode.toml.example`](whycode.toml.example)，声明 MCP server 与启用的 Skill，
  通过 `--config` 指定。文件缺失或全空 = 空装配（纯本地工具）。

## 容器化

```bash
DEEPSEEK_API_KEY=your-key docker compose up --build
# Web: http://localhost:8080   API: http://localhost:8000
```

## 目录结构

```
agent/      ReAct Agent Loop · StreamCollector · 上下文压缩
tools/      文件/命令工具集
policy/     白名单/黑名单/路径沙箱/Allow-Deny-Ask
hooks/      工具执行前后 Hook + 审计
worktree/   Git Worktree 隔离
verify/     测试执行 + 风险报告
store/      JSONL 持久化 · 会话恢复 · Trace
context/    上下文压缩(消息数 + token 水位)
mcp_client/ MCP 客户端(stdio/http)
skills/     Skill 加载与依赖校验
mailbox/    多 Agent 消息与共享状态
api/        FastAPI + SSE(任务/审批/回滚/恢复)
web/        Vue 3 前端(审批面板/恢复)
tui/        Textual 终端工作台
workbench/  工作台领域逻辑
eval/       固定任务集 · 指标汇总
assembly/   装配层(whycode.toml + MCP 生命周期 + Skill 选择)
tests/      单元/集成/端到端测试
scripts/    冒烟与验证脚本
```

## 真实运行冒烟（可选，opt-in）

以下脚本需真实 API key / Docker / 浏览器，**默认不跑**（CI 全部离线 mock）。
显式设 `WHYCODE_SMOKE_REAL=1` 才会执行：

```bash
# 1) 真实 Provider 闭环:run(auto-approve)→ resume 不重放 → 回滚
WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_real.py

# 2) Web 全栈:docker compose 起栈 → curl 创建/SSE/审批/resume 契约冒烟
WHYCODE_SMOKE_REAL=1 bash scripts/smoke_web.sh

# 3) (可选)浏览器驱动:真实点「允许/拒绝」与恢复(需 pip install playwright)
WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_browser.py
```

## 说明

当前实现重点面向受控代码修改任务。第三方 MCP、容器化全栈与生产环境稳定性
仍需按实际部署环境单独做端到端验证。
