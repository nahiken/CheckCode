# WhyCode M1 里程碑 — 测试设计与评估报告

- 日期: 2026-08-31
- 范围: M1 ReAct Agent Loop(`agent/types.py` · `agent/config.py` · `agent/client.py` · `agent/stream_collector.py` · `agent/tool_calls.py` · `agent/loop.py`)
- 环境: Windows 11 · Python 3.11 · uv 0.7.12 · pytest(dev 依赖)
- 基线: 既有 M0 31 个 + M1 26 个 = 57 个;本轮新增 **34** 个,合计 **91** 个,全部通过。

---

## 一、覆盖层次与用例清单

按「单元 → 集成 → 端到端」分层组织。状态:★=新增(本轮),○=已有。

### 单元测试(Unit) — 44 个

| 文件 | 用例 | 覆盖点 | 状态 |
|------|------|--------|------|
| `tests/agent/test_types.py`(7) | `test_assistant_with_tool_calls_serialized` | ★ assistant 带 tool_calls → id/type/function{name,arguments};空 content 序列化为 null |
| | `test_assistant_multiple_tool_calls_order_preserved` | ★ 多 tool_calls 顺序保留 |
| | `test_assistant_without_tool_calls_is_plain` | ★ 无 tool_calls → 纯 role+content,不含 tool_calls 键 |
| | `test_tool_message_serialized` | ★ tool 消息 → role=tool + tool_call_id + content |
| | `test_system_and_user_passthrough` | ★ system / user 透传 |
| | `test_full_mixed_history_sequence` | ★ 完整混合历史(system→user→assistant+tc→tool→assistant)结构与顺序 |
| | `test_empty_message_list_returns_empty` | ★ 空消息列表 |
| `tests/agent/test_llm_config.py`(10) | `test_defaults_when_nothing_configured` | ★ 全未配置时默认 base_url/model/api_key/max_steps/max_tokens/temperature/request_timeout |
| | `test_llm_over_deepseek_priority` | ★ LLM_*(通用)> DEEPSEEK_*(专用)优先级 |
| | `test_deepseek_fallback_when_llm_empty` | ★ LLM_* 空串视为未设置 → 回退 DEEPSEEK_* |
| | `test_explicit_params_override_env` | ★ 显式参数(base_url/api_key/model)> 环境变量 |
| | `test_max_steps_from_env` | ★ AGENT_MAX_STEPS → max_steps |
| | `test_explicit_numeric_override_current_bug` | ★ **缺陷记录**:显式 max_steps/max_tokens/temperature/request_timeout 抛 TypeError |
| | `test_empty_max_steps_env_current_bug` | ★ **缺陷记录**:AGENT_MAX_STEPS="" 抛 ValueError |
| | `test_client_kwargs_contains_required_fields` | ★ client_kwargs 输出字段 |
| | `test_config_is_frozen` | ★ frozen dataclass 不可变 |
| | `test_env_file_loaded_when_present` | ★ 真实 .env 加载(仅断言非空,不读密钥值) |
| `tests/agent/test_stream_client.py`(7) | `test_parse_text_delta` / `test_parse_tool_call_delta` / `test_parse_done_line_returns_none` / `test_parse_comment_and_empty_lines_return_none` / `test_parse_usage_only_block_returns_none` / `test_parse_non_sse_line_raises` / `test_parse_stream_chunk_is_pydantic` | ○ SSE 行解析:文本/工具增量、[DONE]、注释/空行、usage-only 块、非法行报错、返回类型 |
| `tests/agent/test_stream_collector.py`(17) | `test_aggregate_text_deltas` / `test_aggregate_tool_call_arguments` / `test_aggregate_multiple_tool_calls_in_order` / `test_empty_arguments_rejected` / `test_invalid_json_rejected` / `test_non_object_json_rejected` / `test_valid_object_parsed` | ○ 聚合文本、单工具参数、多工具按 index 排序;parse_tool_arguments 空/非法/非对象/合法 |
| | `test_whitespace_only_rejected` | ★ parse_tool_arguments 纯空白 → ValueError |
| | `test_whitespace_wrapped_valid_json_parsed` | ★ 前后空白包裹的合法 JSON 正常解析 |
| | `test_empty_object_accepted` | ★ `{}` 空对象合法 |
| | `test_typed_values_preserved` | ★ bool/null/数值/嵌套对象类型保留 |
| | `test_interleaved_multi_tool_arguments_out_of_order` | ★ 两个工具参数块乱序交错到达,按 index 各自聚合 |
| | `test_missing_id_falls_back_to_call_index` | ★ id 缺失 → 回退 `call_{index}` |
| | `test_missing_name_keeps_empty_string` | ★ name 缺失保留空串 |
| | `test_finish_reason_captured` | ★ finish_reason 被捕获 |
| | `test_content_and_tool_calls_in_same_chunk` | ★ 同一 chunk 同时含文本与工具增量 |
| | `test_parallel_tool_indices_lost_through_parse_current_bug` | ★ **缺陷记录**:真实流式路径 index 丢失,并行工具调用坍缩 |
| `tests/agent/test_tool_calls.py`(7) | `test_partition_serial_by_default` / `test_partition_concurrency_groups` / `test_partition_invalid_concurrency_raises` / `test_execute_partitions_serial_order` / `test_invalid_json_argument_becomes_error_result` / `test_dispatch_exception_becomes_error_result` / `test_tool_call_id_forced_from_request` | ○ 分组(串行/并发上限/非法参数)、执行回灌、非法 JSON→错误、dispatch 异常→错误、**tool_call_id 强制用请求 id** |

### 集成测试(Integration) — 10 个

| 文件 | 用例 | 覆盖点 | 状态 |
|------|------|--------|------|
| `tests/agent/test_loop.py`(10) | `test_single_turn_no_tools` | ○ 单轮无工具 → 直接最终回答 |
| | `test_read_write_read_back_loop` | ○ 读→写→读回多轮循环,dispatch 按模型顺序被调 |
| | `test_invalid_params_rejected_and_not_executed` | ○ 非法参数→错误 Observation,不执行,模型修正 |
| | `test_all_invalid_params_feeds_back_error_and_continues` | ○ 全非法→回灌错误 Observation→继续 |
| | `test_max_steps_limit` | ○ max_steps 上限生效,model_call 次数受约束 |
| | `test_full_event_sequence_read_write_finish` | ★ 读→写→完成三轮完整 StepEvent **顺序敏感**断言 |
| | `test_invalid_event_sequence_all_invalid_then_finish` | ★ 非法轮事件序列:model_call→invalid_params→all_invalid→model_call→finish |
| | `test_all_invalid_params_until_max_steps` | ★ 每轮全非法且不修正 → 持续回灌错误 Observation 直到 max_steps,dispatch 全程不被调用 |
| | `test_max_steps_single_call` | ★ max_steps=1:仅一次模型调用即终止,历史以 tool 消息结尾(固化终止语义) |
| | `test_loop_tool_call_id_forced_from_request_regression` | ★ **回归**:loop 层 dispatch 返回错误 id 仍强制用请求 id |

### 端到端测试(E2E) — 2 个

| 文件 | 用例 | 覆盖点 | 状态 |
|------|------|--------|------|
| `tests/agent/test_e2e_react.py`(2) | `test_full_react_flow_read_write_readback_final` | ★ httpx.MockTransport 模拟 读→写→读回→最终回答 四轮完整流程;校验消息历史结构(system→user→assistant/tool 交替→最终回答)、每对 assistant tool_calls 与 tool 消息 tool_call_id 精确匹配、读回内容、**发送给 provider 的请求 payload**(tools/tool_choice=auto/stream_options.include_usage/messages 结构) |
| | `test_e2e_tool_call_id_regression_dispatch_empty_and_wrong` | ★ 端到端回归:dispatch 返回空 id,完整链路回灌的 tool 消息仍用请求 id |

**汇总**:新增测试文件 `test_types.py`、`test_llm_config.py`、`test_e2e_react.py`(共 3 个);追加到既有文件 `test_stream_collector.py`(+10)、`test_loop.py`(+5)。新增 34 个,已有 57 个,共 **91** 个。

---

## 二、测试运行结果

| 检查项 | 命令 | 结果 |
|--------|------|------|
| 单元+集成+端到端 | `uv run pytest` | **91 passed**,约 11.9 s |
| Lint | `uv run ruff check .` | **All checks passed!** |
| 类型 | `uv run mypy agent` | **Success: no issues found in 7 source files** |
| 类型(新增测试,附注) | `uv run mypy tests/agent` | 仅剩 17 处既有测试的 strict 遗留错误,新增代码 0 处(见风险 1) |

- 分文件:test_stream_client 7 · test_stream_collector 17 · test_tool_calls 7 · test_loop 10 · test_types 7 · test_llm_config 10 · test_e2e_react 2 = 60(agent/);M0 各文件 31。
- 最慢用例:worktree 系列(0.7–0.9 s,真实 git 操作)与 demo_repo fixture 初始化——均为 M0 既有开销,与 M1 无关。
- 全程 0 失败;真实 `.env` 密钥未被任何断言输出或依赖具体值。

### 发现的功能缺陷(记录,未改动 `agent/` 交付代码)

| # | 位置 | 现象 | 触发用例 |
|---|------|------|----------|
| 1 | `agent/config.py::_replace` | `get_llm_config(max_steps=…)`/`max_tokens`/`temperature`/`request_timeout` 显式传参会抛 `TypeError: got multiple values for keyword argument`(字段同时按关键字与 `**kw` 传入) | `test_explicit_numeric_override_current_bug` |
| 2 | `agent/config.py::get_llm_config` | `AGENT_MAX_STEPS=""` 时 `int("")` 抛 `ValueError`,空环境变量未按「未配置」处理 | `test_empty_max_steps_env_current_bug` |
| 3 | `agent/client.py::parse_sse_line` | 解析 `tool_calls` 时未透传 `index`,所有工具增量 index 恒为 0。经 StreamCollector 聚合,**同一 SSE delta 中的并行工具调用会坍缩成一个**,arguments 被非法拼接、id 后写覆盖 | `test_parallel_tool_indices_lost_through_parse_current_bug`(已固化现状) |

缺陷 3 影响面最大:当前 `StreamCollector` 以 `ToolCallChunk.index` 为键的聚合逻辑本身正确,但真实流式链路喂入的数据已丢失 index,导致「单轮并行多工具调用」在真实 provider 流式输出下会错乱。单工具、逐轮串行场景不受影响。

---

## 三、关键结论与风险

### 关键结论

- **序列化契约正确**: `to_openai_messages` 对 assistant 带 tool_calls / tool 消息 / system / user 的序列化结构符合 OpenAI Chat Completions 协议,空 content 正确落为 null;多 tool_calls 顺序保留。
- **配置优先级正确**: LLM_* > DEEPSEEK_*、显式参数 > 环境变量、空串按未设置回退,`AGENT_MAX_STEPS` 映射到 max_steps——除缺陷 1/2 外均验证通过。
- **loop 事件流完备**: StepEvent 的 model_call→tool_exec_start→tool_result→(…)→finish / invalid_params→all_invalid 完整序列、max_steps、全非法参数持续回灌不执行 dispatch,全部通过。
- **tool_call_id 协议约束在两层均有回归保障**: `execute_partitions` 单元层(既有)+ `AgentLoop` 集成层(新增)+ 端到端完整链路(新增),dispatch 返回错误/空 id 均强制回填请求 id。
- **端到端多轮流程通过**: 读→写→读回→最终回答的消息历史结构、请求 payload(tools/stream_options/tool_choice)、每对 assistant/tool 消息 id 精确匹配,均符合预期。
- **已发现 3 个功能缺陷并固化**: 均以「断言现状行为」的绿色用例记录(缺陷 1/2/3),修复后需同步更新对应用例断言。

### 覆盖盲区与风险

1. **测试目录不在 mypy strict 目标内**: `mypy agent` 零告警,但 `tests/agent` 直接跑 mypy 仍有 17 处遗留错误(均为既有测试:未标注的 helper、`dict` 缺类型参数、`list[object]` 访问 `.kind`)。本轮新增代码已保持 strict 干净;建议后续把 tests 纳入 mypy 目标或收紧新增代码规范。
2. **真实流式链路并行工具调用不可用**(缺陷 3): `StreamCollector` 聚合逻辑正确但 `parse_sse_line` 丢 index。任何「单轮多个工具调用」在真实 provider 下都会出错,当前架构实际上是「单轮单工具」安全。M2 必须修复并补端到端并行用例。
3. **配置数值参数无法显式覆盖**(缺陷 1): 除 base_url/api_key/model 外,`get_llm_config` 的数值参数当前一传即崩;同时 max_tokens/temperature/request_timeout 不支持环境变量。建议补齐环境变量支持并修复 `_replace`。
4. **`StreamingClient` 错误路径未覆盖**: HTTP >=400 的 `httpx.HTTPStatusError` 分支、超时、网络错误均无测试;真实 API 验证脚本已手动跑通正常路径,但异常分支缺少自动化保障。
5. **`client_kwargs` 疑似死代码**: `StreamingClient` 直接读 `LLMConfig` 字段,`client_kwargs()` 仅被测试调用、未被业务消费,存在与 `stream_chat` payload 构造重复维护的风险。
6. **`.env` 真实密钥与测试耦合**: config 测试依赖项目根 `.env`(含真实 key)以 override=False 加载。CI 或克隆环境若缺少 `.env`,`test_env_file_loaded_when_present` 通过条件跳过(已做防御),但其它默认值测试依赖 monkeypatch 显式置值,尚属确定。
7. **max_steps 终止语义**: 达到上限时消息历史以最后一条 tool 结果结尾,无关闭性 assistant 回合;已用 `test_max_steps_single_call` 固化该语义,若产品预期「给出超时说明」需在 loop 层补充。
8. **`tool_choice` 细粒度未覆盖**: 仅验证了 tools 存在时默认 `tool_choice="auto"`;`none` / 强制指定某工具的分支未测试。

---

*本报告由 M1 测试设计阶段产出;新增测试文件:`tests/agent/test_types.py`、`tests/agent/test_llm_config.py`、`tests/agent/test_e2e_react.py`,并追加 `tests/agent/test_stream_collector.py`、`tests/agent/test_loop.py`。未修改 `agent/` 交付代码。*
