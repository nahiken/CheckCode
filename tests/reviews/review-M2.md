# M2 Review 记录(双轴 code-review)

> 审查范围:`git diff e3cb12e...f8922f1`(M2 核心提交)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 修复提交:`6c9d8da`(review 修复),`9ffa78b`(清理误提交临时文件)
> 修复后验证:**165 tests passed**,ruff/mypy(strict,47 文件)全绿,真实 DeepSeek 严格模式端到端通过

## 二轮 Review(M2 完整范围 `e3cb12e...a3ef33c`,修复提交)

> 双轴并行复审完整 M2(含 review 修复提交)。发现 6 项,全部修复于 commit `M2-bis`。

### Standards 轴(4 项)

| # | 气味(严重度) | 位置 | 成因 | 修复 |
|---|--------------|------|------|------|
| S1' | **Middle Man + Spec 承诺未接线**(med) | `hooks/audit.py` `before/after` 是 `append` 纯转发且**从未被调用**;`runner` 只在 `_finish` 里 `audit.append` | 定义了两段式 API 但 runner 未接线 | 接线:`dispatch` 审批后、执行前调 `audit.before`;`_finish` 作 after 落盘最终记录。已执行调用 now 产生 before+after 两条,被拒仅一条(granted 区分) |
| S2' | **截断语义分裂**(med) | `tools/commands.py` 失败路径内联 `content[:MAX*2]` 且不置 `truncated`;成功路径用 `truncate_output` | 两条截断规则 | 统一用 `truncate_output`,成功/失败一致(now `truncated` 字段保留) |
| S3' | **Primitive Obsession**(med) | `layer: str` 自由字符串,层名散落 evaluator/runner | 判定层未收敛 | `policy.models.DecisionLayer` Literal;`PermissionDecision.layer`/`ToolAuditEvent.layer` 用它;runner `cast` 兼容 |
| S4' | **命名冲突**(low-med) | `agent/types.py` 与 `tools/base.py` 两个 `ToolResult`,仅靠别名区分 | 领域概念重名 | `agent.types.ToolResult` → `AgentResult`(工具实现返回值保留 `tools.base.ToolResult`);同时删 `tool_call_id` 死字段(由 `_run_single` 绑定请求 id,工具层不填写) |

> 另:两个 `ToolResult` 结构上已近乎相同(S7 重构后),rename 一步到位避免混淆。

### Spec 轴(2 项,均已修复)

| # | 类型 | 规格依据 → diff 位置 | 描述 |
|---|------|----------------------|------|
| P1' | partial(**真 gap**) | `PHASED_PLAN`「执行前后 Hook + 审计」→ `hooks/audit.py` 定义了 `before/after` 但 runner 从不调用;`docs/02` 与 runner docstring 声称两段式 | 执行前 Hook 死代码。修复:runner 接线(见 S1'),docs/02 同步描述「已执行→before+after 两条;被拒→仅一条 granted=False」 |
| P3' | wrong(边缘真 bug) | `ListDirArgs.path` 默认 `"."`,但 runner 用原始 args 评估 → 合法 `list_dir {}` 被沙箱误判越界 | 校验模型应用默认值后评估。修复:`model_validate` 后 `validated.model_dump()`,用校验后参数做评估/执行/审计 |

### 已确认不需修复

- **P2(planner 复制隔离)**:`build_plan` 直接操作传入 workdir 无 copytree——只读是行为保证(deny-path + read_only 调度),隔离由调用方负责(verify_deepseek 已先复制到临时目录)。**仅重写 docstring 如实描述**,不引入复制。
- **P4(并发热度)**:M1 `max_concurrency`(默认 1)已覆盖,LEARNING_PLAN 半项记为已文档化决策,不改。

### 二轮验证

- **167 tests passed**(165 + 新增 2:两段式审计、list_dir 默认参数),ruff/mypy strict(47 文件)全绿。

## 一轮 Review(范围 `e3cb12e...f8922f1`,修复提交 `6c9d8da`)

### Standards 轴发现(6 项)

| # | 问题 | 成因 | 修复 |
|---|------|------|------|
| S1 | `tools/commands.py` docstring 与实现矛盾:声称「抛 SubprocessTimeoutError / 不走 shell」,实际 `except subprocess.TimeoutExpired` 且 `shell=True` | 先写文档后写代码,未同步 | 重写 docstring,准确描述「以 shell 字符串执行 + 超时返回错误 ToolResult」 |
| S2 | `Tool._sandbox` 死代码(从未被调用,三个文件工具直接调 `resolve_in_sandbox`) | 纵深防护设计时留了抽象入口但未接线 | 删除 `_sandbox`,统一走 `policy.sandbox.resolve_in_sandbox` |
| S3 | `runner.py` 结果构造重复:9 次 `AgentToolResult(tool_call_id="", content=..., is_error=...)` 模板 + 9 次 `_finish` 调用 | 被拒路径各自拼装,未抽助手 | 抽 `_reject(event, started, message, layer)` 统一被拒路径;`_finish` 签名简化为 `(event, started, result, result_text)` |
| S4 | `rules: list[Any]` 类型削弱(evaluator 已是 `list[Rule]`) | 早期宽泛签名 | 全链改 `list[Rule]` |
| S5 | 工具分类硬编码字符串集合 `PATH_TOOLS`/`WRITE_TOOLS` + `tool_name == "run_command"`,新增工具需同时改 evaluator | 分类知识放错地方(权限层 vs 工具层) | `tools_spec_from` 输出 `read_only`/`path_key` 分类字段;**单一来源** = Tool 类声明;evaluator 从 spec 读取 |
| S6 | 黑名单漏掉 `rm ../foo` / `rm foo` 相对路径删除 | pattern 只覆盖「flags 后紧跟 `/` 或 `~`」 | 重写 rm pattern:保留系统路径拦截 + 新增「未标记路径删除」拦截;带 `--`/`./` 前缀视为项目内安全(黑名单本质不可靠,主防线是审批) |

## Spec 轴发现(5 项)

| # | 问题 | 成因 | 修复 |
|---|------|------|------|
| P1 | **执行前后 Hook 未两段式**。规格要求「工具执行前后统一触发」,实现只有一次性 `audit.append`;runner docstring 自称 before/after 与代码不符 | 简化实现时把两段合并成一段 | `AuditLog` 实现 `before(event)` / `after(event)` 两段 Hook;runner 在审批后执行前调 `before`,完成后调 `after`(README 注明:审计不依赖异常路径,拒绝与成功都被独立记录) |
| P2 | **严格模式 Planner 缺失**。规格要求「只读 Planner 生成计划 + 审批」,但 Plan 由脚本硬编码 | 未实现规格核心 | 新增 `policy/planner.py`:`build_plan()` 复制工作区 → 只读调度(全部工具 read_only → permit,写被拒)跑一轮 Agent → 解析 `[计划]` 文本为 `Plan(files + commands)`;`verify_deepseek.py --strict` 真实调用通过 |
| P3 | **严格模式 run_command 恒被拒**(真 bug)。`plan.allows(str(args.get("path","")))`,run_command 无 path → 恒 False → strict 写一律 deny,而任务要求「运行 pytest」 | Plan 只声明文件白名单,未覆盖命令 | `Plan.commands` 命令白名单 + `allows_command()`(前缀匹配);evaluator 第 4 层区分:path 工具查文件、run_command 查命令 |
| P4 | 审计语义不清:非 Ask 场景 `approved` 恒为 False 易误导;只存 `result_preview`(200 字符),规格要求「结果」 | 审计模型字段不精确 | 加 `asked: bool`(是否进入审批);非 Ask 用 `asked=False` 区分,Ask 用 `approved`;`result_preview` 改 `result` 存完整结果 |
| P5 | 并发热度控制未显式实现 | LEARNING_PLAN 阶段 2 要求,但 loop 已有 `max_concurrency` 分组上限 | 记为「loop 层 `max_concurrency`(默认 1,全串行)已提供并发上限控制」,写入 docs/02 说明 |

## 范围蔓延(Spec 轴,判定为合理保留)

- `Rule.pattern` 命令子串匹配 + 顺序覆盖语义:规格仅言「Allow/Deny/Ask 规则」,实为「配置化规则引擎」的必要表达力,保留。
- `truncate_output` 输出截断:M4 规格的「超长输出裁剪」提前实现,属渐进式,保留。

## 已满足的规格点(不需修复)

- 参数 JSON Schema 校验(M1 review 遗留:空对象缺必填字段 → runner `schema` 层拦截)✓
- 越权写被拒(normal 无审批器 → ask 默认拒绝,文件不变)✓
- 路径逃逸 / 高危命令 / 严格模式测试 ✓
- 审计落盘(完整结果 + 耗时 + 权限结论)✓
- 真实 DeepSeek:normal 越权审批 + strict Planner 全链路 ✓
