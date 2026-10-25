# M15 测试评估报告

> 阶段:M15(`allowedTools` 作为运行时工具白名单)。评估时间:2026-09-11。
> 基线提交:M14 末(`d406814`)。测试口径:CI 全离线;本报告只记本地确定性套件。
> 「622」为 M15 + 审计修复之后的**全项目口径**。

## 测试规模

### Python(pytest)—— 581 → 625(+44)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/policy/test_skill_allowlist.py` | **+12** | `None` 同构(用 `==` 比完整 `PermissionDecision`)/白名单内放行(layer 仍是 mode)/未命中 deny+layer/空集全拒/压过计划/压过 permit 规则/**层序承重两条(同时违反白名单与硬边界 → 必须报硬边界)**/沙箱与黑名单的基础边界/未知工具 |
| `tests/skills/test_loader_allowlist.py` | **+7** | 裸名在集合内合法/集合外拒/前缀形式不受影响/**不传集合 → M5 语义逐字不变**/registry 校验两条/`resolve_dependencies` 两条 |
| `tests/tools/test_runner_allowlist.py` | **+6** | 白名单内可用/未列写工具 **deny 且文件未落盘**/审计 `layer=skill`/`AgentResult.audit` 带 skill/沙箱边界不受影响/`None` 同构(剔除墙钟字段) |
| `tests/assembly/test_skills.py` | **+6** | 全无声明 → `None`/并集/前缀不进白名单/**空集 ≠ None**/内置不参与收窄/`skill_tool_allowlist` 纯函数 |
| `tests/verify/test_verify_allowlist.py` | **+8** | 闭环内 **write_file 被 skill 层拒且零写入**/白名单外工具被拒 → 错误 Observation 落盘且模型走完/**白名单外工具仍在 `tools_spec` 里(有意设计,钉成契约)**/**只读 pass 行为断言(list_dir → skill)**/只读 pass 接线断言/白名单内写入生效/`None` 不变 |
| `tests/test_cli_allowlist.py` | **+2** | **`_run_loop` 把 asm 的白名单传给 `VerifiedLoop`(接线守卫)**;`--skills` 解析(可重复 + 缺省 `None`) |
| `tests/api/test_allowlist_api.py` | **+3** | **API 路径接线守卫**(有声明 → 白名单到达 loop;无声明 → `None` 不限制);**AST 守卫:禁止 `api/app.py` 函数体内导入重模块**(审计缺陷 A 的永久回归网) |
| **全项目累计** | **625 passed** | M15 后 |

### TypeScript(vitest)—— 32(不变)

M15 是后端/CLI 的权限层能力,前端不涉及(`_TaskRequest.skills` 是新增**可选**字段,
缺省即旧行为,故不需要前端配套改动)。

## 测试方法学(本阶段最有价值的部分)

### 1. 兼容纪律要用「等值断言」而不是「行为大致相同」

`None` = 不限制是本次的**兼容承诺**。测试用**最严的形式**证明它:

```python
def test_none_means_unrestricted(tmp_path):
    ro = _ev(tmp_path).evaluate("read_file", {"path": "a.py"})
    assert ro == _ev(tmp_path, tools_allow=None).evaluate("read_file", {"path": "a.py"})
```

`PermissionDecision` 是 pydantic 模型,`==` 比的是**全字段**(decision/layer/reason)。
比「都 permit」强得多 —— 层归属或理由措辞变了都会红。

`tests/tools/test_runner_allowlist.py::test_none_leaves_behavior_unchanged` 同理比
`AgentResult.audit` 的**全部字段**(仅剔除 `duration_ms` 这个墙钟测量,否则必然 flaky)。

### 2. 层序断言必须是**双边的**(否则是假保证)

初版层序测试是**单侧**的:

```python
ev = _ev(tmp_path, tools_allow={"read_file"})
d = ev.evaluate("read_file", {"path": "../escape.py"})   # ← 被调工具在白名单内
assert d.layer == "sandbox"
```

**审计用变异测试证明它测不出东西**:把 skill 检查块挪到沙箱**之前**,该测试仍然通过 ——
因为被调工具在白名单内,skill 层**永不触发**,无论层序如何都只看得到沙箱。

改成**双边**(被调工具**不在**白名单,使 skill 层与硬边界**都想拒**):

```python
ev = _ev(tmp_path, tools_allow={"write_file"})            # read_file 不在白名单
d = ev.evaluate("read_file", {"path": "../escape.py"})    # 既越界、又不在白名单
assert d.layer == "sandbox"                               # 只有层序正确才成立
```

**变异复验**:再挪一次 skill 层 → 两条立刻变红(`- blacklist / + skill`)。

> 这与 M13 的教训同源(「涉及不得共存的断言要双端检查」)。**通用形式**:
> 测「A 先于 B」时,输入必须让 **A 和 B 都想命中**,否则测的是「只有 A 能命中」。

### 3. 把「有意的设计决定」钉成契约

`verify/loop.py:499` 传给 `build_agent` 的是**未过滤**的 `runner.tools_spec` ——
白名单外的工具仍留在模型的工具表里(调得动 → 被拒 → 拿到可读错误 → 自己换路)。

这不是疏漏,是**有意设计**(白名单是每任务动态的收窄,不是 Agent 的静态能力边界)。
但「有意」如果只写在注释里,下一个人「顺手优化」就把它改掉了。所以写成测试:

```python
async def test_denied_tools_remain_visible_to_model(...):
    assert "write_file" in spec_names   # 白名单外 —— 仍在 schema 里
```

### 4. 「测了接线」不等于「测了行为」

只读 pass 的初版测试夹住 `ToolRunner` 工厂记录 kwargs:

```python
assert {"read_file"} in captured     # ← 只证明 runner 拿到了白名单
```

**审计指出这属于「测了症状没测规格」** —— 执行路径断了(比如 `evaluator` 没转发 `tools_allow`)
它照样绿。改成断言**工具真的被拒**:

```python
assert call.layer == "skill"          # 不是 plan(只读 pass 的 strict 空计划也想拒它)
assert call.granted is False
```

接线断言被拆出来单独保留(`test_readonly_pass_runner_receives_allowlist`)—— 两者互补:
**接线**测「线接上了没有」,**行为**测「接上之后真的生效吗」。

### 5. 变异测试(证明测试真的能失败)

| 变异 | 期望变红 | 实测 |
|---|---|---|
| skill 检查块挪到**沙箱之前** | 层序测试 | ✗ 初版**存活**(单侧断言)→ 修好测试后 ✅ 变红 |
| skill 检查块挪到**规则之后** | `test_allowlist_denies_even_with_permit_rule` | ✅ FAILED `assert 'rules' == 'skill'` |
| skill 检查块挪到**计划/模式之后** | 空集全拒 + permit 规则 | ✅ 2 FAILED |

`policy/evaluator.py` 变异前后 md5 `4ff07d6411f05d4b0a05be329ff30c43` 一致(审计员复核)。

### 6. **回归**:测试**必须**盖住「改动引入的既有路径退化」

审计抓到一处真回归(严重度**重**):`api/app.py` 把 `from assembly.build import build_assembly`
放在 `_run_task()` **函数体内**。该模块的传递依赖(`assembly.mcp → mcp_client.client → mcp` SDK)
首次导入约 **3.4s**,而它发生在 `asyncio.create_task` 之后 —— **同步阻塞事件循环**,
把每个 API 任务的首次工具调用推迟 3.4s。既有测试 `_wait_status(timeout=3.0)` 因此必然超时。

**A/B 对照**(审计员做的,这是关键手法):

```
父提交 d406814:  tests/api/...::test_approve_resolves_and_runs  → PASSED
HEAD 7d9368e:    同一测试                                        → FAILED
```

**为什么本地的定向测试没抓到**:我在开发中只跑了 `tests/policy|skills|tools|assembly|verify`
与 `tests/api` 的**单个文件**(`test_approvals_api.py` 单独跑时,`assembly.build` 已被
别的 import 预热,3.4s 被摊掉了)。**全量套件的收集阶段会 import 全部模块**,所以只在
全量运行时才暴露 —— 这正是「改完必须跑全量」的实证。

修复:import 移到模块级(`api/app.py` 文件头)。**并补一条永久守卫** ——
`test_api_module_has_no_heavy_function_body_imports` 用 **AST** 检查 `api/app.py`
的函数体内有没有导入重模块(`assembly` / `mcp_client` / `mcp`)。

> **为什么用 AST 而不是计时**:先写的是「心跳协程量测事件循环停顿」的计时版,
> 但它在测试里根本跑不起来(心跳协程还没被调度,`_run_task` 就同步跑完了),
> 而且计时天然受预热影响。**要守的是结构性事实** —— 重导入必须在模块级 ——
> AST 检查是确定性的、且不 flaky。变异复验:把 `assembly.build` 挪回函数体 → 立刻变红。

> **可复用的判据**:改动了**模块级 import 图**(新增跨层 import)时,即使逻辑测试全绿,
> 也要跑一遍受影响**子系统**的全目录(而非单文件)+ 一次全量。

## 静态检查

- `ruff check .` → All checks passed
- mypy strict → **71** 源文件无问题

## 为什么有些东西**没有**测(诚实边界)

- **多技能并集的跨技能工具扩散**:规格明文要求并集,故「技能 A 声明的 `read_file` 对技能 B
  也可用」是设计使然;其**可被利用性**需多技能部署 + 真实 LLM 交互,不在本轮范围。
- **CLI worker 路径**(`cli/main.py:392`)的透传仍**无自动化守护**:已精读确认代码正确,
  但若日后有人删掉那行,套件不会变红(测试仓库里没有声明 `allowedTools` 的技能,
  装配出来本就是 `None`,赋值是空操作)。**API 路径的同类缺口已在审计后补上守卫**
  (`tests/api/test_allowlist_api.py`),worker 路径列为遗留项。
- **API 路径不读 `whycode.toml`**(`config_path=None`):与 CLI 缺 `--config` 时等价,
  故 Web 任务无法启用仓库内声明的 MCP/技能。当前无实际影响,记为后续项。
- **`allowedTools` 不提供 OS 级隔离**:白名单允许 `run_command` 时 `shell=True` 照旧;
  白名单的边界 = `ToolRunner` 的边界。
