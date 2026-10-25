# M15 双轴审计报告 — `allowedTools` 作为运行时工具白名单

> 审计对象:commit `7d9368e`(M15)。工作区基线 = HEAD `966b64a`。
> 审计员:独立第三方子代理(`general-purpose`,新会话)。方法:**实跑 + 变异测试 + A/B 对照**(父提交 worktree)。
> **工作区状态**:审计结束时工作区仅剩原有未跟踪产物,所有变异已还原。

---

## §1 结论摘要

M15 的主体设计**正确**,规格 5 条中 4 条完全达标,核心兼容纪律(S1)经实跑确认成立。但有**一处真缺陷**:

| # | 判决 | 严重度 | 一句话 |
|---|---|---|---|
| **A** | ❌ **真缺陷·回归** | **重** | `api/app.py` 把 `from assembly.build import build_assembly` 放在 `_run_task()` 函数体内。首次导入耗时 **3.4–3.5s**(连带 `mcp_client` + `mcp` SDK),在 `asyncio.create_task` 之后**同步阻塞事件循环**,每个 API 任务的首次工具调用被推迟 3.5s。A/B 证明:父提交 `d406814` 该测试**通过**,HEAD 该测试**失败**。 |
| **B** | ⚠️ 测试方法学 | 中 | `test_sandbox_still_first_with_allowlist` **不能失败** —— 变异把 skill 层挪到沙箱之前,测试仍绿。它证明不了它声称的层序(单侧断言)。文档把它当作层序证据属**过度声明**。 |
| **C** | ⚠️ 存疑·集成缺口 | 中 | API 路径 `build_assembly(repo, None, ...)` 硬编码 `config_path=None`:API **永不加载** `whycode.toml`,`[skills].allow` / `[mcp.servers]` 对 Web 路径全部失效,而 `_TaskRequest.skills` 的注释写的是「whycode.toml `[skills].allow` 的请求级覆盖」—— 一个没有基线的覆盖口。 |
| **D** | ⚠️ 规范 | 轻 | 并集语义使「技能 A 声明的工具对技能 B 也可用」,与用户「`allowedTools` 是本技能的白名单」直觉有落差(规格明文要求并集,故非缺陷,但值得写进文档风险栏)。 |
| **E** | ⚠️ 规范 | 轻 | `build_assembly` 的 `allow=[]` 折叠为「全部可见技能」,而 `_TaskRequest.skills=None` 同义 → API **无法表达「空 allowlist」**。 |
| **F** | ⚠️ 规范 | 轻 | `assembly/build.py:88` 用 `|=` 原地改 dataclass 字段;`SkillSelection` 非 frozen,复用 `AssemblyContextManager` 时会二次并集。当前调用方各建各的实例,暂不可达,属预防性建议。 |
| **G** | ⚠️ 规范 | 轻 | `skills/loader.py:51` 的新错误消息把「当前可用工具」全量拼进字符串。200 个注册工具 → 消息长 2266 字符。 |
| **H** | ⚠️ 测试缺口 | 轻 | CLI worker 路径(`cli/main.py:392`)与 API 路径的接线**均无测试**(`tests/api/` 零改动)。 |

**没有发现**:任何逃逸通道、任何既有断言被削弱、任何 M5 语义漂移。
`tests/skills/test_loader.py` 在 M15 中**一字未改**(`git show --name-status` 仅列出 `A tests/skills/test_loader_allowlist.py`)。

---

## §2 Spec 逐条核对表

### 条款 1 — `skills/loader.py` 接受裸本地工具名;保留 `VALID_PREFIXES`;既有错误消息与断言不动;加 `_is_local_tool` → **✅ 达标**

- `skills/loader.py:27` `VALID_PREFIXES = ("mcp:", "skill:")` 保留;`:30` `_is_local_tool`;`:79` `Skill.allowed_local_tools`(property,只挑裸名)。
- 错误消息(`:53-55`)**含** `"必须以 mcp:/skill: 前缀声明"` → 仍匹配 `match="mcp:"`。

```
$ ./.venv/Scripts/python.exe -m pytest tests/skills/test_loader.py tests/skills/test_loader_allowlist.py -q
...................                                                      [100%]
```

- `git show 7d9368e --name-status -- tests/skills/` 输出只有 `A  tests/skills/test_loader_allowlist.py`,**没有 `M tests/skills/test_loader.py`**。

### 条款 2 — `SkillSelection.allowed_local_tools: set[str] | None`;`None`=不限制;`set`=并集 → **✅ 达标**

- `assembly/skills.py:38` 字段;`:41-54` `skill_tool_allowlist`;`:113` 装配时计算。
- 并集由 `tests/assembly/test_skills.py::test_allowlist_is_union_of_declaring_skills` 断言 `== {"read_file","write_file"}`,随套件全绿。

### 条款 3 — `policy/evaluator.py` ctor 加 `tools_allow`,位于沙箱/黑名单**之后**、规则/计划**之前**,返回 `deny` 且 layer=`"skill"`,并在 `DecisionLayer` 注册 → **✅ 达标**(行为正确;但「之后」这一侧无测试把关,见 §4)

- `policy/evaluator.py:46` ctor 参数;`:57` 存储;`:85-90` 检查块(位于黑名单 `:76-81` 之后、规则 `:91` 之前)。
- `policy/models.py:15` 注册 `"skill"`。
- 实跑分层:

```
list_dir -&gt; deny skill          # 只读 + strict 空计划 + 白名单外 → skill 层(不是 plan)
write_file -&gt; deny skill
no-allowlist list_dir -&gt; permit mode   # 反证:skill 层是唯一拒绝来源
```

### 条款 4 — `tools/runner.py` / `verify/loop.py`(含只读 runner)/ CLI 逐层透传;`None` → 完全无变化 → **✅ 达标**

生产构造点逐个核对(`grep -rn "ToolRunner(\|PolicyEvaluator(\|VerifiedLoop(\|build_assembly(" --include=*.py . | grep -v ^./tests/`):

| 构造点 | 透传? | 证据 |
|---|---|---|
| `tools/runner.py:63,79` | ✅ | `tools_allow` → `PolicyEvaluator` |
| `verify/loop.py:427-435`(主 runner) | ✅ | `tools_allow=self.allowed_local_tools` |
| `verify/loop.py:366-374`(只读 pass runner) | ✅ | 同上 |
| `cli/main.py:215-229`(run/resume) | ✅ | `allowed_local_tools=allowed_local_tools` |
| `cli/main.py:383-392`(worker) | ✅ | `allowed_local_tools=asm.skills.allowed_local_tools` |
| `api/app.py:192` | ✅(构造后赋值) | `loop.allowed_local_tools = ...`;`run()` 内 `:223` 才 `_build_runner`,读到的是新值 |
| `policy/planner.py:79` | ⬜ **有意不传** | `docs/15-m15.md:162`、`M15_PROGRESS.md` 明文声明为设计意图(规划要用只读工具探明计划) |
| `eval/runner.py:110`、`scripts/smoke_real.py:58`、`scripts/verify_deepseek.py:113` | ⬜ 不传 = `None` | 与「兼容纪律」一致(评测基线/repro 脚本保持原样) |

**兼容纪律实跑(本项是 S1 的核心):**

```
$ ./.venv/Scripts/python.exe -c "...assemble_skills(Path('.'), user_dir=None)..."
skills: ['explain']
allowed_local_tools: None
is None: True
```

`skills/builtin/explain/SKILL.md` 确无 `allowedTools`,`skill_tool_allowlist` 对「全都没声明」返回 `None` →
**默认 CLI 路径零变化成立,S1 担忧不成立。**

### 条款 5 — 措辞:工具级白名单而非 OS 沙箱 → **✅ 达标**

`tools/runner.py:9-11`、`skills/loader.py:12-13` 均明写「这是**工具级白名单**(限工具名),不是 `run_command` 意义上的 OS 沙箱」。

---

## §3 Standards 问题清单

1. **【重】`api/app.py:188` 函数体内 import 造成 3.5s 事件循环阻塞**(= §1-A)。建议移到模块级;若为规避循环导入,可改为复用已导入的 `assembly.skills` 或在 app 启动时预热一次。
2. **【中】`api/app.py:190` `config_path=None` 硬编码**:与 CLI 的 `--config` 能力不对称,且 `_TaskRequest.skills` 的注释自称「whycode.toml `[skills].allow` 的请求级覆盖」,但该路径下没有 config 可覆盖。(MCP 亦同此问题,非 M15 引入,但 M15 的注释把它说成覆盖语义。)
3. **【轻】`assembly/build.py:87-88` 原地修改**:`skills.allowed_local_tools |= set(extra_tools)`。`SkillSelection` 非 frozen;若将来复用同一个 `AssemblyContextManager` 或缓存 `SkillSelection`,并集会累积。建议 `skills.allowed_local_tools = skills.allowed_local_tools | set(extra_tools)`。
4. **【轻】`skills/loader.py:51` 错误消息无上界**:`allowed = ", ".join(sorted(registered_tools))`。实测 200 工具 → 2266 字符。建议截断。
5. **【轻】字段命名与内容不符**:`SkillSelection.allowed_local_tools` 里实际装的是「本地 ∪ MCP 工具名」。docstring 解释了,但字段名会让调用方误以为只含本地工具。

---

## §4 变异测试记录(S3)

**改前 / 改后 md5**(`hashlib.md5(open('policy/evaluator.py','rb').read()).hexdigest()`):

| 阶段 | md5 |
|---|---|
| 变异前(基线) | `4ff07d6411f05d4b0a05be329ff30c43` |
| 变异 A′ 后 → 还原 | `4ff07d6411f05d4b0a05be329ff30c43` ✅ |
| 变异 B 后 → 还原 | `4ff07d6411f05d4b0a05be329ff30c43` ✅ |
| 变异 C 后 → 还原 | `4ff07d6411f05d4b0a05be329ff30c43` ✅ |
| **最终(审计结束)** | **`4ff07d6411f05d4b0a05be329ff30c43`** ✅ |

> 注:该 md5 与 `git show 7d9368e:policy/evaluator.py` 的 `1a87e2c8503401d3cbc195256aabfbe0` 不同,原因是本机 `core.autocrlf=true` 使 blob 为 LF(6222 B)而工作区为 CRLF(6351 B)。审计前后均以**工作区字节**为准,且 `git diff --stat policy/evaluator.py` 全程为空。

| # | 变异 | 期望变红的测试 | 实测 | 判决 |
|---|---|---|---|---|
| **A′** | skill 检查块挪到**沙箱之前** | `test_sandbox_still_first_with_allowlist` | **10 passed(未变红)** | ❌ **变异存活 = 该断言单侧** |
| **B** | skill 检查块挪到**规则之后** | `test_allowlist_denies_even_with_permit_rule` | FAILED `assert 'rules' == 'skill'` | ✅ 有把关 |
| **C** | skill 检查块挪到**计划/模式之后** | `test_empty_allowlist_denies_everything`、`test_allowlist_denies_even_with_permit_rule` | 2 FAILED | ✅ 有把关 |

**A′ 的可观察差异**(说明这不是无效变异,而是测试**选材错误**):当一次调用**同时**违反白名单与沙箱时,层归属会从 `sandbox` 变成 `skill`:

```
$ （A′ 生效时）evaluate('read_file', {'path': '../escape.py'}) with tools_allow={'write_file'}
layer= skill (expected sandbox; mutation attributes it to skill)
```

**根因**:`test_sandbox_still_first_with_allowlist` 用 `tools_allow={"read_file"}` 且调用 `read_file` —— 该工具**在白名单内**,skill 层永不触发,无论层序如何都只看得到沙箱。`test_blacklist_wins_over_allowlist` 同理。**测试集里不存在任何「工具既不在白名单、又踩硬边界」的用例**(grep 全仓确认)。

**修复建议**(一行):把 `tools_allow` 改成不含被调工具,使 skill 层与硬边界**都想拒** —— 此时只有层序正确才返回硬边界的 layer。

> 另:`M15_PROGRESS.md` 与 `docs/15-m15.md` 把该测试当作「在沙箱之后」的证据,属**过度声明**,需同步修正措辞。

---

## §5 确认「没问题」的项 + 证据

| 项 | 结论 | 证据 |
|---|---|---|
| **S1 兼容纪律** | **成立**(`None` 而非 `set()`) | 实跑:`allowed_local_tools: None / is None: True` |
| **S4 空集名副其实** | **成立** | 实跑 `PolicyEvaluator(tools_allow=set())` 对 4 个已知工具 + 未知工具**全部** `deny/skill` |
| **S5 既有断言未被削弱** | **成立** | `git show 7d9368e --name-status -- tests/skills/` 只有 `A test_loader_allowlist.py`;`tests/assembly/test_skills.py` 的删除行只有 3 行(docstring + 2 import),无断言删除 |
| **S8 错误消息兼容** | **成立** | 新消息含 `mcp:`;`tests/skills/test_loader.py` 19 项全绿 |
| **S7 只读 pass 层归属** | **成立(layer=skill,非 plan)** | 实跑:`strict + Plan() + allow={read_file}` 下 `list_dir -> deny skill`;去掉白名单后同一调用 `-> permit mode` |
| **S6 `None` 时跳过并集** | **语义一致** | `build.py:87` 的 `if ... is not None` 与 `None`=不限制对齐,无空集/None 混淆 |
| **代码规范** | **全清** | `ruff check` → `All checks passed!`;`mypy` strict → `Success: no issues found` |
| **定向套件** | **全绿**(除 §1-A) | `tests/assembly tests/skills tests/policy tests/tools tests/verify tests/test_cli_allowlist.py` → 227 passed |
| **S2 生产接线点覆盖** | **基本齐全** | 见 §2 条款 4 的表;唯一「有意的洞」是 Planner,已有文档背书 |

---

## §6 存疑与未验证边界

1. **【重·已定位,机制完整】§1-A 的回归** —— A/B 证据:
   ```
   父提交 d406814（worktree /tmp/m15base2）:
   $ python -m pytest "tests/api/test_approvals_api.py::TestInteractiveApproval::test_approve_resolves_and_runs"
   .                                                                        [100%]   ← 通过
   HEAD 7d9368e:
   FAILED tests/api/test_approvals_api.py::TestInteractiveApproval::test_approve_resolves_and_runs
   ```
   时间线实测:`runner task created [0.000]` → `FakeLoop ctor [0.000]` → **`FakeLoop.run enter [3.344]`**,即 3.3s 空档正好落在 `api/app.py:188-190` 的 import/装配段。
   import 成本实测:`assembly.build` 首导入 **3.47s / 3.38s**(两次);`-X importtime` 显示 `assembly.mcp 3.33s` ← `mcp_client.client 2.65s` ← `mcp` SDK(约 3.7s)。测试的 `_wait_status(timeout=3.0)` 因此必然超时。**该失败 100% 可复现(单测、整文件、套件三种方式均失败)。**
   **未验证**:该 3.5s 是否已在生产 user-journey 中被观察到(无真实 API key,未跑端到端)。
2. **【中】API 是否应加载 `whycode.toml`** —— 判断是**集成缺口而非 M15 规格违背**(规格只要求「CLI/Web 逐层透传」,已做到)。但注释与实现不符。仓库所有调用方都显式传 `config_path`(CLI `--config` / worker payload),`None` 是 API 的一贯选择;是否要改属产品决策。
3. **【存疑】并集语义的安全含义** —— 判断:**实现与规格一致,规格本身有风险但非缺陷**。规格条款 2 明文写「并集」;M15 额外引入的「只有声明过才收窄」规则**不与规格冲突**(规格只定义了 `None`/`set` 的含义,没规定「全都没声明」落到哪个值)。**未验证**:多技能并集导致的跨技能工具扩散是否可被利用(需多技能部署 + 真实 LLM,本轮无此环境)。
4. **【测试缺口】未验证的接线点** —— CLI worker 路径与 API 路径的透传**无测试**。已精读确认代码正确,但缺自动化守卫:若日后有人删掉 `api/app.py:192`,套件不会变红(API 测试的 repo 里没有声明 `allowedTools` 的技能,装配出来本就是 `None`,该行是空操作)。
5. **【测试方法学】`test_readonly_pass_also_honours_allowlist` 测的是「接线」不是「行为」** —— 只断言 `{"read_file"} in captured`(夹住 `ToolRunner` 工厂记录 kwargs),**没有断言任何工具真的被拒**。属**覆盖缺口**而非「假保证」(写路径的行为由 `test_write_file_call_rejected_by_skill_layer` 真刀真枪覆盖,只读 pass 的行为已由审计实跑独立确认)。建议补「只读 pass 内 `list_dir` 被 skill 层拒」的行为断言。
6. **未跑**:全量 `pytest -q` 与 `vitest`。审计以定向套件(227 项)+ A/B 父提交对照替代。

---

## §7 修复记录(主会话,审计后)

| 编号 | 修复 |
|---|---|
| **A(重)** | `api/app.py` 的 `from assembly.build import build_assembly` **移到模块级**;`_run_task` 内**遗留的另一处**函数体导入(`from verify.prepare import prepare_repo`,纯 stdlib、无阻塞风险)也一并上提,使该模块**零函数体重导入**。复测 `tests/api` → **60 passed**。新增 `tests/api/test_allowlist_api.py::test_api_module_has_no_heavy_function_body_imports` 作**永久守卫**(AST 检查,非计时)——**变异复验**:把 `assembly.build` 挪回函数体 → 立刻变红(`_run_task() line 174: assembly.build`) |
| **B(中)** | `tests/policy/test_skill_allowlist.py` **重写层序断言**:新增 `test_sandbox_wins_when_both_skill_and_sandbox_would_deny` 与 `test_blacklist_wins_when_both_skill_and_blacklist_would_deny` —— 被调工具**不在白名单内**,使 skill 层与硬边界**都想拒**,只有层序正确才返回硬边界的 layer。**变异 A′ 复验:两条立刻变红**(`- blacklist / + skill`),还原后 `git diff --stat` 为空 |
| **C(中)** | `_TaskRequest.skills` 注释改为「API 路径不读 whycode.toml,故这是本路径唯一的技能入口」;`_run_task` 处补注释说明 `config_path=None` 与 CLI 缺 `--config` 等价。**行为不变**(属文档口径修正,是否给 API 加 `--config` 等价入参记为后续项) |
| **E(轻)** | `build_assembly` 的 `allow` 三态语义写进 docstring(`None` 与 `[]` **同义**,均为「不筛选」) |
| **F(轻)** | `assembly/build.py` 的 `|=` 改为 `\|`(不原地改 dataclass 字段) |
| **G(轻)** | `skills/loader.py` 抽 `_summarize_tools()`:超过 `_SUMMARY_LIMIT=10` 个工具时折叠为「前 10 个, …(共 N 个)」 |
| **H(轻,原为测试缺口)** | **已补守卫**:`tests/api/test_allowlist_api.py` 新增 3 条 —— ① 仓库里有声明 `allowedTools` 的技能 → 白名单必须到达 `VerifiedLoop`(变异:删掉赋值即红);② 无声明 → `None` 不限制;③ AST 守卫禁止 `api/app.py` 函数体重导入。**CLI worker 路径**仍无守卫(记为遗留项) |
| **§6-5(测试缺口)** | `test_readonly_pass_also_honours_allowlist` 改为**行为断言**(`list_dir` → `layer="skill"` + `granted is False`);原接线断言拆出为 `test_readonly_pass_runner_receives_allowlist` |
| D | **保留为诚实边界**(并集语义的跨技能扩散已写进 `docs/15-m15.md`) |

**修复后口径**:pytest **625 passed**;ruff / mypy strict(71 文件)全清。
