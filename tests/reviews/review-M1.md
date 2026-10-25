# M1 代码审查记录

> 审查方式:新开子会话对 M1 提交(c171b90 + 2398341)执行 /code-review + 独立测试评估。
> 状态:问题已分类。高优先级已修复(M2/M4 修复提交)。F9(usage 块被丢弃)已由 M8 token 采集解决。
> 延迟项(F14/F15)属后续里程碑收口,非 M1 阻塞。

## 高优先级 — 已确认

### 1.【真实 bug】parse_sse_line 丢失 tool_call index,并行多工具调用坍缩合并
- **问题**:`agent/client.py` 解析 tool_calls 增量时只取 id/name/arguments,不读 `index`。导致 ToolCallChunk.index 恒为 None → StreamCollector 把所有工具调用归并到 index 0,参数被非法拼接、id 后写覆盖。
- **成因**:解析层未透传 index;现有测试直接构造 `ToolCallChunk(index=...)` 绕过解析层,单工具默认 index=0 恰好正常,故漏检。
- **解决**:`ToolCallChunk(..., index=tc.get("index"))`。已由测试子会话记录为 defect 回归测试(`test_parallel_tool_indices_lost_through_parse_current_bug`),修复后翻转断言。
- **影响**:真实 DeepSeek 并行多工具调用会出错;单工具/串行不受影响。

### 2.【安全】dispatch 无路径沙箱,模型可路径穿越
- **问题**:`scripts/verify_deepseek.py` 的 read_file/write_file 用 `workdir / args["path"]` 直接拼路径,`../` 或绝对路径可逃逸工作目录。
- **成因**:`parse_tool_arguments` 只校验"合法 JSON 对象",不校验路径安全。
- **解决**:M1 在 dispatch 侧强制 `resolve()` 后必须位于 workdir 内,拒绝 `..`/绝对路径(路径沙箱在 M2 正式化)。

## 中优先级 — 待修复

### 3. 非法+合法参数混排时回灌顺序与模型调用顺序不一致
- **问题**:`agent/loop.py` 先回灌全部 error_results,再执行 valid_calls;若顺序为 [合法a, 非法b],实际回灌 [b错误, a结果],与 assistant tool_calls 顺序相反。
- **解决**:按 assistant.tool_calls 原顺序构造结果列表再统一回灌。

### 4. finish_reason="length" 被当作最终答案
- **问题**:`StreamCollector` 收集了 finish_reason,但 loop 从不读取。max_tokens 截断的半截回答被当作最终答案。
- **解决**:loop 检查 finish_reason,为 "length" 生成 StepEvent 并触发重试/提示。

### 5. config._replace TypeError + 空 env ValueError
- **问题**:`_replace` 手写字段拷贝,frozen dataclass 下传参重复报 TypeError;`AGENT_MAX_STEPS=""` 抛 ValueError。
- **解决**:改 `dataclasses.replace`;空 env 视为未设置。

## 低优先级 — 记录

- F6:每次模型调用新建 AsyncClient,无连接复用(优化项)。
- F9:usage 块被丢弃(token 用量不可得,M4 评测需要,暂缓)。
- F11:空 api_key 仍发请求(库调用方缺前置校验)。
- F13:config docstring 与行为不一致。
- F14:ToolResult.tool_call_id 成为死字段,dispatch 契约待收口。
- F15:并发执行时共享文件写竞态(M2 启用并发时处理)。

## 复测
- 修复后:uv run pytest 全绿、ruff/mypy 通过、真实 DeepSeek 调用重验。
