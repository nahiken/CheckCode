# review-M11 — 真正的 Hook 注册表(双轴)

> 阶段:M11。审核:实现(①–② 工作流)完成后交另一会话做**独立双轴**评审
> (Standards 编码规范 + Spec 规格符合度),再主会话复核修复。
> 审计方法:代码通读 + 6 组实跑命令 + **4 组变异测试**(改代码验证测试是否真能变红,
> 全部改回并 md5 校验)+ `git stash` 冻结时钟**逐字节**对比基线。

## 1. Spec 轴(规格符合度)

M11 声称 6 条,逐条核验:

| # | 声称 | 判决 | 证据 |
|---|------|------|------|
| 1 | `run` 按注册序串行 await,容忍 sync Hook | ✅ | `hooks/registry.py` `run`;`tests/hooks/test_registry.py::test_hooks_run_in_registration_order_sync_and_async` |
| 2 | 每个 Hook 收**快照**(隔离就地改写) | ✅(修复后) | 见下「中-1」:原为浅拷贝,已改 `deep=True` + 变异验证 |
| 3 | before 抛错 → 中断+上抛 → 拒绝(不执行)+ layer `hook` | ✅ | `registry.py` `raise`;`tools/runner.py` `except … _reject(layer="hook")`;`policy/models.py` 登记 `"hook"`;变异验证:把 `_reject` 换成 `pass` → 两条测试同时变红 |
| 4 | after 抛错 → 吞掉 + 记账,链继续 | ⚠️→✅ | 逻辑符合;`except Exception` 不覆盖 `BaseException`(不改,加文档 carve-out);文案照会已修 |
| 5 | **兼容纪律:审计输出逐字节不变** | ✅ | 冻结 `time.monotonic` 跑 8 次 dispatch × 两 runner → 两份 audit.jsonl **md5 完全相同(2990 字节,不归一化)**;基线 `git stash` 后 `test_runner.py` 18 条原地全过 |
| 6 | ctor 加 `hooks`,默认 `default_registry(self.audit)`,保留 `self.audit` | ✅ | `runner.py` 签名/赋值;`grep` 确认生产代码 `hooks=` 零调用点(走默认);`_finish`/`_reject` 异步化**彻底**(全仓 12 处调用点全带 `await`) |

**核心结论:第 5 条(本里程碑最重要声称)经实测成立**,而非仅代码推断。

## 2. Standards 轴(规范/坏味)

| 项 | 处置 |
|---|---|
| 裸字符串 `kind == "before"` 与 `HookKind` 各写一遍 | 抽模块级 `_BEFORE: HookKind` |
| `HookRegistry.hooks(kind)` 与 `ToolRunner.hooks` 属性同名,读代码卡顿 | 改名 `hooks_for(kind)` |
| `_reject(layer: str)` + `cast` | 保留(既有 wart,非 M11 引入) |
| 无重复/死代码;docstring 全中文;类型惯用法与 `verify/loop.py` 一致 | ✅ |

## 3. 发现的问题与修复

### 中-1 浅拷贝 → 「扩展 Hook 不能反向污染」的承诺不成立(已修)

- **问题**:`event.model_copy()` 默认 `deep=False`;`ToolAuditEvent.args` 是 dict,**快照与 live 事件共享引用**。字段重绑(`event.granted=True`)被隔离,但 `event.args["path"]="HACKED"` 会污染 live 事件 → 进而污染 after 落盘的审计。
- **修法**:改 `event.model_copy(deep=True)`。新增测试 `test_snapshot_isolates_mutable_args`;**变异验证**:改回浅拷贝 → 该测试变红(`{'path':'HACKED'} != {'path':'a.py'}`),改回即绿。

### 中-2 「逐字节守卫」测试名不副实(已修)

- **问题**:`test_custom_registry_records_match_default_registry` 自称逐字节守卫,实则只比 3 个投影,且两侧走**同一份被测代码** → 任何共享路径退化同时作用于两侧,**结构上不可能被发现**(变异验证:把 `event.result = result_text` 改为 `= ""`,该测试仍绿;抓到它的是旁边的 `test_runner.py`)。
- **修法**:① docstring 降级为「接线等价守卫」;② **新增黄金测试** `test_audit_record_is_byte_stable`——冻结时钟 + 断言**全字段**(含 `result` 文案),把审计形态钉死。这是审计探针的固化。

### 轻-1 `logger.exception` 文案在被拒路径说谎(已修)

- **问题**:固定输出「after Hook 失败(**工具已执行**)」,但 `_reject → _finish` 也触发 after(工具根本没执行)。实测:blacklist 拒绝 + 抛错 after Hook → 打出「工具已执行」而 `granted=False`。
- **修法**:文案改「after Hook 失败(结果照常返回): tool=%s granted=%s」,并把「after 也会在被拒路径触发」写进模块 docstring。

### 轻-2 `except Exception` 不含 `BaseException`(不修,加文档)

- `CancelledError`/`KeyboardInterrupt` 会穿透。吞取消信号是反模式 → **不改代码**,在 docstring 加 carve-out:「只吞 `Exception`;`BaseException` 照常穿透」。

### 轻-3 无 logging 基础设施 → 审计失败静默(已在文档声明)

- `hooks/registry.py` 是全仓**唯一** `logger` 点,无 `basicConfig` → traceback 直落 stderr。若 `AuditLog.after` 自身失败(磁盘满/权限),审计记录**静默丢失**。默认注册表下极端罕见,已在 `default_registry` docstring 明说这是有意的取舍(不引入 logging 配置,避免库污染宿主)。

### 轻-4 文档未同步(已修)

`docs/02-tools-and-policy.md`(「由 `ToolRunner` 接线」→ 注册表;表加 `registry.py`)、
`docs/05-mcp-skills.md`(`audit.before/after` → `HookRegistry` 的 before/after)。

### 轻-5/6/7(已修/无碍)

- 测试里冗余默认值 `plan=None, rules=None` 删除。
- `default_registry` docstring 补「同时传 `audit=`/`hooks=` 请用同一个 AuditLog」。
- `verify/loop.py:412` 的 doc rot(指向已不存在的 `ToolRunner.last_event`)已修,commit message 单列说明。

## 4. 未覆盖风险(留待后续里程碑)

- `BaseException` 穿透无测试(有意不覆盖)。
- 无 golden 之前审计形态靠人肉探针;现已固化。
- `HookRegistry` 多实例并发隔离无测试(静态无共享状态,低风险)。

## 5. 双轴结论

**无重级问题**。中-1/中-2 为「文档承诺/测试命名与实现不符」,已用**变异测试**证明修复到位。
核心声称(逐字节兼容)以最强手段(frozen-clock md5)实测成立。修复后:
pytest **473 → 477**、ruff / mypy strict(64 文件)全清。
