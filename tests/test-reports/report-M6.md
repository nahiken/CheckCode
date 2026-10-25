# M6 测试评估报告

> 阶段:M6 Multi-Agent Mailbox + 共享状态(`mailbox/`)。
> 审查评估(工作流第 ⑤ 步)由独立子会话/子代理执行。
> 评估时间:2026-08-31。基线提交:`a4b3db0`(M6 核心)+ review 修复(待提交)。

## 测试规模

| 层级 | 文件 | 用例数 | 覆盖内容 |
|------|------|-------|----------|
| 单元 | `tests/mailbox/test_store.py` | 7 | 原子快照读回/空文件/建父目录/覆盖写/损坏报错/无 .tmp 残留/合法 JSON |
| 单元 | `tests/mailbox/test_queue.py` | 22 | submit 幂等/FIFO claim/lease 标记/ack 归属校验/nack 重投与 dead/recover 重投/encapsulation |
| 集成 | `tests/mailbox/test_concurrency.py` | 5 | N worker 并发 claim 无重复/超量 claim 拿不到/claim 原子性/并发 ack+nack 一致/两 worker 无竞态 |
| 集成 | `tests/mailbox/test_persistence_recovery.py` | 5 | 重建保留 pending/崩溃后 lease 重投可再 claim/ack 不重投/租约内不重投/worker 交接全部 done |
| **合计** | **4 文件** | **39** | M6 新增(原 35 + review 回归 4) |

全项目累计:**317 tests passed**(M5 278 + M6 39),ruff/mypy strict 94 文件无问题。

## review 修复(双轴,1 严重缺口 + 2 硬违规 + 3 判断性)

- **F1(Spec)/b-4(Standards)严重缺口**:`_recover_deliveries` 不检查 attempts → **lease 崩溃重投可无限重投毒消息**。修复:抽 `_release_and_maybe_dead`(nack + lease 重投两条路径共用 attempts 判定),达 max_deliveries → dead。新增 2 回归测试。
- **S2 硬违规**:store.py 异常消息含中文(GBK 乱码)→ 改英文措辞。
- **S4 硬违规**:`_MAX_SCAN` 死常量 + `MailboxSnapshot` 无消费方 → 删除。
- **b-3 封装泄漏**:`_claim`/`_submit` 返回活的内部 TaskRecord → `model_copy(deep=True)` 隔离 + 2 回归测试。
- **b-2 Duplicated Code**:`_claim` 辅助重复 → 抽 `tests/mailbox/_helpers.py`。
- **F3 低危耐久性**:store save 后 best-effort fsync 父目录(Windows 降级)。
- **F4(已声明不改)**:claim 原子性仅单进程内(asyncio.Lock),跨进程互斥属阶段 8 范围。

## 验收对照

| 验收点 | 测试 | 结果 |
|--------|------|------|
| `mailbox/` claim/ack/nack + 原子任务存储 | test_store + test_queue | ✅ 状态机全语义 + 原子快照 |
| 消息重投 / 共享任务同步 / 中断恢复 | test_queue(recover)+ test_persistence_recovery | ✅ 真实磁盘重建验证 |
| 并发 claim 竞态 | test_concurrency(C1/C3) | ✅ asyncio.Lock 原子性 |
| ack/nack 语义 | test_queue(Q4–Q7) | ✅ 归属校验 + dead 判定 |
| 两个 Agent 共享任务状态无竞态 | test_concurrency(C1/C5) | ✅ gather 真并发无重复 |
| 崩溃后消息可重投 | test_persistence_recovery(P2/P4/P5) | ✅ at-least-once |
| **防毒消息无限重投(review 新增)** | test_queue(2 个 recover-to-dead) | ✅ nack 与 lease 重投共用上限 |

## 关键指标

- 修复后回归:317/317 通过,零失败。
- 质量门:ruff(E/F/I/UP/B/SIM)+ mypy strict 全绿,94 source files。
- 并发验证:4 worker / 6 任务并发 claim 无重复;两 worker 消费 10 条无重叠(gather 真并发)。
- 崩溃恢复:claim 后不 ack(模拟崩溃)→ 磁盘重建 → lease 过期 recover → 另一 worker 接手 → 全部 done,零丢失。

## 结论

M6 全部验收点通过。Mailbox 的 claim/ack/nack + 原子快照存储 + lease 重投 + 中断恢复 +
并发竞态均有测试 + 真实磁盘重建证据。review 双轴发现的最严重缺口(lease 崩溃重投绕过
max_deliveries 可无限重投)已修复并有回归测试。下一里程碑 M7(浏览器工作台 FastAPI+SSE+Vue3)。
