# M4 测试设计(JSONL 持久化 + 会话恢复)

> 工作流第①步:先设计测试,再 TDD 实现。
> 覆盖:`store/`(JSONL 落盘/读回/恢复推导)+ `context/`(上下文压缩)+ loop 挂接 + 恢复闭环 + CLI resume。

## 验收点(M3 记忆中的「下一步」)

1. **JSONL 落盘**:消息 / 工具调用 / 运行状态 / 审批边界全部持久化。
2. **恢复不重放**:kill 进程后恢复,**已完成副作用操作跳过**,只重放只读操作。
3. **超长工具输出落盘裁剪**:落盘内容截断(不把整条巨大输出写进 store 或上下文)。
4. **上下文压缩**:逼近窗口上限时压缩早期历史(`context/`)。
5. **验收场景**:手动 kill → 恢复 → 不重放写 → 继续完成。

## 分层设计

| 模块 | 职责 |
|------|------|
| `store/models.py` | 记录模型(header/message/tool_call/approval/phase/usage) |
| `store/session.py` | SessionStore:JSONL 追加写 / 读回 / 损坏容忍 / 恢复推导 |
| `store/effect.py` | 副作用判定器(`is_read_only`):恢复重放边界的**唯一来源** |
| `context/compression.py` | 窗口模拟 + 早期历史压缩(不含 tool 结果) |
| `agent/loop.py` | 挂接:session id + `record_event` 回调 + token 记账 + 可重入 `run()` |
| `verify/loop.py` | 挂接:复用既有 branch、恢复时跳过已完成的写操作、超长输出落盘 |
| `cli/main.py` | `mewcode resume` 真实恢复 |

## 测试设计

### 一、`tests/store/test_store.py`(单元)

**目标:验证纯存储层。** 不依赖 git/LLM。

| # | 用例 | 断言 |
|---|------|------|
| S1 | `record_header + record_message` 追加 → 读回 | JSONL 逐行、字段完整、顺序保持 |
| S2 | header 后追加 message → 读回含 header | header 与消息共存 |
| S3 | 读回时可 `continue_session` → 追加跨进程记录 | 顺序拼接,不覆盖旧记录 |
| S4 | 损坏行容忍(中间插 `{broken`) | 读回跳过损坏行,返回 `(records, skipped_count)` |
| S5 | `tool_call` 记录含工具参数 + 判定层 | 字段可解析,approval/边界可追溯 |
| S6 | 空 store / 不存在文件 | 读回空列表 |
| S7 | `record_tool_call(overridden=True)` | 覆写标记可解析(M4 恢复语义) |
| S8 | 截断工具输出落盘 | 落盘内容 ≤ 上限 + 截断标记 |

### 二、`tests/store/test_effect.py`(单元)

**目标:副作用判定的**唯一来源**,恢复重放边界就靠它。**

| # | 用例 | 断言 |
|---|------|------|
| E1 | `is_read_only("read_file")` | True |
| E2 | `is_read_only("write_file")` | False |
| E3 | `is_read_only("run_command")` | False(命令可能含副作用,一律不可重放) |
| E4 | `is_read_only("list_dir")` | True |

### 三、`tests/store/test_recovery.py`(单元)

**目标:恢复推导逻辑——不重放已完成副作用操作。**

| # | 用例 | 断言 |
|---|------|------|
| R1 | 全部已恢复(无 pending 写) | `recovery_plan` = 不需要恢复,新运行干净重放 |
| R2 | pending 读(读未完成/无结果) | 从 pending 读恢复(重新喂给模型) |
| R3 | pending 写(写已落盘但无 tool_result 回灌) | **跳过**,回灌合成的「已跳过副作用」tool result |
| R4 | `overridden=True` 记录 | 恢复模型时**排除**对应 assistant tool_calls(防重复回灌) |
| R5 | 复合场景:一轮读(恢复)+ 一轮写(跳过) | 模型上下文 = 已读结果 + 跳过提示,无重放写 |

### 四、`tests/context/test_compression.py`(单元)

**目标:上下文压缩。**

| # | 用例 | 断言 |
|---|------|------|
| C1 | 未超限 → 原样返回 | 消息数不变、内容不变 |
| C2 | 超限 → 压缩早期 assistant 消息 | 含 tool_calls 的消息被剔除(早于压缩点),system/user 保留 |
| C3 | 压缩结果可 `to_openai_messages` | 不抛异常,序列化正常 |

### 五、`tests/agent/test_loop_session.py`(集成)

**目标:AgentLoop 挂接 —— 会话持久化 + 记账 + 可重入。**

复用 `helpers.make_mock_loop` 或 build_agent + MockTransport。

| # | 用例 | 断言 |
|---|------|------|
| L1 | `build_agent(..., session_id=)` 传入 → loop 持有 | 构造函数可注入 |
| L2 | run 产生 StepEvent → `record_event` 回调收到(含 usage/token) | 事件流可消费 |
| L3 | 可重入 run:先跑一轮(无 tool)→ 再跑一轮 | 第二次调用成功(会话记录追加,不重置) |

### 六、`tests/verify/test_verified_loop_resume.py`(集成 + 端到端)

**目标:恢复闭环 + 不重放副作用(核心验收)。** 复用 M3 的 mock transport 模式(真实 pytest 在 worktree 内)。

| # | 用例 | 断言 |
|---|------|------|
| V1 | 正常 run 后 store 落盘 | `.git/whycode-sessions/` 下存在 session 文件,含 header/消息/tool/phase |
| V2 | 已有相同 `(session_id, branch)` 的 store → resume=True | 复用既有 branch,不新建 worktree |
| V3 | **不重放写**(核心):上一 session 已有已完成写(读+写+读)+ 失败;resume 后 mock 只收到**读**与**文本**,无第二次写 | worktree 内文件一次落盘,恢复不回滚不重放 |
| V4 | resume 后 `_load_store` 可见 session 消息 | 复盘/调试可读取 |
| V5 | 超长工具输出落盘裁剪 | store 内记录内容 ≤ 上限 + 截断标记 |
| V6 | 真实 LLM 端到端脚本可跑(手测) | kill 后 resume 继续完成 |

### 七、`tests/test_cli.py` 扩展

| # | 用例 | 断言 |
|---|------|------|
| CLI1 | `resume` 无参数 | 打印帮助/提示,不崩溃 |
| CLI2 | `resume --session <id>` 无 store | 报错退出码 1,提示不存在 |

## 质量门

- `uv run pytest -q` 全绿(预计 201 + ~40 → ~241)
- `uv run ruff check .` / `uv run mypy .` 全绿(strict)
- 真实 DeepSeek:`scripts/verify_loop_deepseek.py` 支持 `--resume`(M4 挂接),手测 kill 恢复

## 边界与风险

- **恢复复用既有 branch**:恢复时**不调用** `manager.create()`,而是以既有 branch 重建 Runner/loop——避免新建 worktree 丢已写文件。
- **判定器是唯一来源**:`store/effect.py` 的 `is_read_only` 定义重放边界;任何新工具接入必须在此登记。
- **上下文压缩**记 `rounds_used`/`messages` 计数,不记 token(阶段 8 再量化);窗口默认按 `AGENT_CONTEXT_WINDOW` 环境变量可调。
- **record_event 回调**:`loop.run` 加 events 参数的纯扩展——既有测试零破坏(不传即可),为阶段 7 SSE 预埋。
