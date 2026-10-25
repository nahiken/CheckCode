# M6 Review 记录(双轴 code-review)

> 审查范围:`git diff 2625c74...HEAD`(M6 核心提交 `a4b3db0`)
> 审查方式:并行 Standards(编码规范)+ Spec(规格符合度)两个子代理
> 审查时间:2026-08-31。修复提交:`a7ac65b`(M6 双轴 review 修复)。

## Spec 轴发现

### 真缺口(验收点落空)

| # | 类型 | 规格依据 | 描述 |
|---|------|---------|------|
| F1 | missing(严重) | design-M6 §1.3「单任务最大投递次数;超过 → dead(避免无限重投毒消息)」+ §4 要点「max_deliveries 防毒消息无限重投」 | **lease 崩溃重投不设上限**:`_recover_deliveries` 回 pending 时**不检查 attempts**,`_claim` 也不检查。实测 max_deliveries=3 下循环「claim+租约过期 recover」6 次,attempts=6 仍 pending——「防无限重投」仅对 nack 成立,崩溃型毒消息(worker 反复崩溃、从不 nack)可无限重投。修复:recover 时 attempts ≥ max_deliveries → **dead** |
| F2 | missing(交付缺失) | PHASED_PLAN M6「文档:docs/06-multi-agent.md」+ LEARNING_PLAN 阶段 6「产出:mailbox/、docs/06-multi-agent.md」 | `docs/06-multi-agent.md` 未产出(docs/ 止于 05)。修复:阶段收尾写原理文档(本表记录,另任务跟踪) |
| F3 | wrong(低危耐久性) | design-M6 §1.5「要么旧要么新」 | store `save`:fsync 文件后 os.replace,未 fsync 父目录——断电窗口可能只落旧快照、丢最后一次变更。修复:os.replace 后 best-effort fsync 父目录 |
| F4 | caveat(已声明) | design-M6 §2.3「claim 原子性:asyncio.Lock 保护内存态」 | claim 原子性仅**单进程内**;跨进程共享同一快照文件无互斥(整快照 last-write-wins)。属已声明范围限制(进程内满足验收「两个 Agent 无竞态」)。**不改**(扩进程互斥是阶段 8 范围) |

### 确认成立(不需修)

- 崩溃重投路径完整:P2/P4/P5(重建→recover→再 claim,attempts 递增)经真实磁盘验证。
- ack/nack 归属校验 + 状态守卫正确,与 design 语义表一致;attempts 语义(claim+1、nack 保留)一致。
- 原子存储:mkstemp 同目录 + fsync + os.replace 成立,S6 验证无 .tmp 残留。
- scope creep:无。diff 仅 mailbox/ + 测试 + design-M6,无越权改动。

## Standards 轴发现

### 硬违规(违反既有约定)

| # | 气味 | 位置 | 描述 | 修复 |
|---|------|------|------|------|
| S2 | 异常消息非 ASCII | `mailbox/store.py` L56/L58/L62 三处 `raise ValueError(f"…中文…")` | Windows GBK 控制台乱码 + pytest.match 无法匹配(M5 review 确立「异常消息用 ASCII」) | 改英文措辞 |
| S4 | 死代码/投机抽象 | `mailbox/queue.py` L27 `_MAX_SCAN`(从未引用)+ `mailbox/models.py` `MailboxSnapshot`(无消费方) | 定义后无任何使用 | 删除 |

### 判断性(已修)

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| b-4 | 状态机缺口 | `_recover_deliveries` 不查 max_deliveries | **同 Spec F1,一并修复** |
| b-3 | 封装泄漏(Feature Envy) | `_claim` 返回活的内部 `TaskRecord`,worker 可在锁外改写并绕过 _persist | 返回 `model_copy(deep=True)` 快照,隔离内部态 |
| b-2 | Duplicated Code | `async _claim` 辅助在 test_queue / test_persistence_recovery 逐字重复 | 抽 `tests/mailbox/conftest.py` 共享 fixture |
| b-1 | Duplicated Code | 测试中 22 处状态字面量散落 4 文件 | **不改**:对序列化 wire 格式断言属正常,抽取反而引入间接层 |

## 修复后验证

- **全量测试 + ruff/mypy(strict)全绿**。
- F1 新增测试:`lease 重投达 max_deliveries → dead`(崩溃型毒消息不再无限重投)。
- F3 目录 fsync 加 best-effort(Windows 无法 os.open 目录,try/except 包裹)。
