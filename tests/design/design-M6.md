# M6 测试设计:Multi-Agent Mailbox + 共享状态

> 工作流①产物(先设计再实现)。阶段 M6 按 `LEARNING_PLAN.md` 阶段 6 + `PHASED_PLAN.md` M6 里程碑。
> 设计目标:每项验收点先定测试(红)→ 实现(绿)→ 补边界。
> 核心语义:at-least-once 投递 + 幂等任务设计;claim/ack/nack 可靠消费;崩溃后消息可重投。

## 1. 功能范围(`mailbox/`)

### 1.1 问题:多个 Agent 共享任务队列

多个 Agent(worker)需要共享一个任务队列,且满足:

1. **一条消息只能被一个 worker 同时处理**(并发 claim 无竞态)。
2. **消息不丢**:worker 崩溃/超时后消息要能**重新投递**(at-least-once)。
3. **状态可恢复**:进程重启后队列状态从磁盘重建,未完成的任务继续。

### 1.2 术语与语义

| 术语 | 语义 |
|------|------|
| `submit(payload)` | 投递一条任务(带幂等键 `task_id`) |
| `claim(worker_id)` | **原子领取**:从 pending 取一条 → `claimed`,带租约 `lease_until` |
| `ack(worker_id, task_id)` | 处理成功 → `done`。校验:仅持有该任务的 worker 可 ack |
| `nack(worker_id, task_id, error)` | 处理失败 → 回 `pending` 重投;达 `max_deliveries` → `dead` |
| lease(租约) | claim 后限时确认;到期未 ack 视为 worker 崩溃 → `recover_deliveries` 重投 |
| `recover_deliveries(now)` | 扫描超租约的 `claimed` → 回 `pending`(消息重投核心) |
| `max_deliveries` | 单任务最大投递次数;超过 → `dead`(避免无限重投毒消息) |

### 1.3 任务状态机(要点:状态机幂等设计)

```
            submit                claim                ack
  (新) ─────────────► pending ─────────────► claimed ───────────► done
                      │  ▲  ▲                  │  ▲
         nack(未超限)  │  │  │   lease 过期      │  │
           ┌──────────┘  │  └───────────────────┘  │
           │             │        (重投)           │
      nack(超限)         │                        done 后不可再变
           └────────► dead                        (幂等终点)
```

- **claim 原子性**:并发 claim 必须互斥(`asyncio.Lock` 保护内存态),保证一条任务同一时刻至多被一个 worker 持有。
- **at-least-once 的代价**:lease 过期重投可能造成**重复处理**(worker 崩溃前可能已执行副作用)→ 需要任务侧幂等。
- **幂等键 `task_id`**:`submit` 相同 `task_id` 返回既有记录,**不重复入队**;已 `done` 的任务重复提交不重跑。`exactly-once = at-least-once + 幂等消费者`(消费者以 `task_id` 为幂等锚点,M4「不重放副作用」的呼应)。

### 1.4 模块划分

| 模块 | 职责 |
|------|------|
| `mailbox/models.py` | `TaskStatus`(StrEnum)+ `TaskRecord` + `MailboxSnapshot`(Pydantic) |
| `mailbox/store.py` | `MailboxStore`:**原子快照**持久化(写临时文件 + `os.replace`);崩溃后重建 |
| `mailbox/queue.py` | `MailboxQueue`:submit/claim/ack/nack/recover_deliveries + 内存态 + 持久化 |

### 1.5 为何任务存储用「原子快照」而非 M4 的「JSONL 事件日志」

| 维度 | M4 会话(JSONL append) | M6 任务(原子快照) |
|------|----------------------|-------------------|
| 语义 | 事件日志:记录「发生了什么」 | 当前态:记录「现在是什么」 |
| 读回 | 需要重放全部行得到状态 | 一次 JSON 读出完整状态 |
| 崩溃安全 | 追加写,最多丢最后一行 | `os.replace` 原子替换,要么旧要么新 |
| 适用 | 会话流/审计(要历史) | 队列(要一致视图) |

**要点**:事件日志的价值在「可重放/可审计」;队列的价值在「一致性视图」。原子快照天然保证读到的是一致状态,无需重放;append 日志天然适合只追加的历史流。两者不是替代,是语义不同。

## 2. 测试设计

### 2.1 `tests/mailbox/test_store.py`(单元,零队列逻辑)

| # | 用例 | 断言 |
|---|------|------|
| S1 | `save(记录)` → `load()` | 记录字段完整读回、状态一致 |
| S2 | 空文件/不存在 → `load()` | 空 dict |
| S3 | 父目录不存在 → `save` | 自动建父目录 |
| S4 | 覆盖写两次 → `load` | 最新状态(不残留旧记录) |
| S5 | 损坏 JSON → `load` | 抛 ValueError(清晰 message) |
| S6 | `load` 后改 dict → 再 `save` | 原子写:临时文件 `.tmp` 不残留 |

### 2.2 `tests/mailbox/test_queue.py`(单元,内存态,clock 注入)

| # | 用例 | 断言 |
|---|------|------|
| Q1 | `submit` → 状态 | pending,attempts=0,created_at 已设 |
| Q2 | `claim` 取最先提交的 | FIFO(按 created_at 排序) |
| Q3 | `claim` 副作用 | claimed_by=worker、attempts+1、lease_until=now+lease_seconds |
| Q4 | `ack` 成功 | → done,done_at 已设 |
| Q5 | `ack` 非持有 worker | 返回 False,**状态不变**(防误确认) |
| Q6 | `nack`(未超限) | → pending 重投,可再次 claim,attempts 保留 |
| Q7 | `nack` 达 `max_deliveries` | → dead |
| Q8 | lease 过期未 ack → `recover_deliveries` | → 回 pending(消息重投) |
| Q9 | lease 未过期 → `recover_deliveries` | 不重投(worker 仍在处理) |
| Q10 | done/dead 不被 claim | claim 跳过,返回下一可用 |
| Q11 | `submit` 重复 task_id(已 done) | 返回既有记录,不重复入队(幂等键) |

### 2.3 `tests/mailbox/test_concurrency.py`(集成,asyncio 并发)

| # | 用例 | 断言 |
|---|------|------|
| C1 | N 个 worker 并发 claim N 条任务 | 每任务恰好一个 worker,无重复 task_id |
| C2 | 并发 claim 超任务数 | 额外 claim 返回 None |
| C3 | 同一任务被 claim 后并发再 claim | 第二个拿不到(原子性) |
| C4 | 并发 ack/nack 混合 | 最终状态一致:done/dead/pending 总数=任务数,无丢失 |
| C5 | 两个 worker 独立消费全部任务 | 每任务被处理且 ack,共享状态无竞态 |

### 2.4 `tests/mailbox/test_persistence_recovery.py`(集成,崩溃恢复)

| # | 用例 | 断言 |
|---|------|------|
| P1 | 提交一批 → save → 重建 queue | 任务还在(pending) |
| P2 | claim 后「崩溃」(不 ack)→ 重建 → recover(lease 过期) | 任务重投,可再次 claim → **at-least-once**(允许重复投递) |
| P3 | ack 后重建 | done 保持,不重投 |
| P4 | 重建时 claimed 未过期 | 不立即重投;等 lease 过期后再 recover → 重投 |
| P5 | 端到端:worker A 崩溃,B 接手 | 全部任务最终 done(中断恢复,不丢任务) |

### 2.5 回归

- 全量 `uv run pytest` 保持全绿(既有 278 不回归);ruff/mypy(strict) 全绿。

## 3. 验收点对照(`PHASED_PLAN.md` M6)

| 验收点 | 测试 | 结果 |
|--------|------|------|
| `mailbox/` claim/ack/nack + 原子任务存储 | test_store + test_queue(Q1–Q11) | ✅ |
| 消息重投 / 共享任务同步 / 中断恢复 | test_queue(Q8)+ test_persistence_recovery(P2/P4/P5) | ✅ |
| 并发 claim 竞态 | test_concurrency(C1/C3) | ✅ |
| ack/nack 语义 | test_queue(Q4–Q7) | ✅ |
| 两个 Agent 共享任务状态无竞态 | test_concurrency(C1/C5) | ✅ |
| 崩溃后消息可重投 | test_persistence_recovery(P2/P4) | ✅ |

## 4. 设计要点

1. **at-least-once vs exactly-once**:claim+ack+lease 保证「至少一次」——崩溃/超时未 ack 必重投,可能重复;`exactly-once = at-least-once + 幂等消费者`,幂等锚点是 `task_id`(与 M4「不重放已完成副作用」呼应)。
2. **任务状态机的幂等设计**:`pending → claimed → done / (重投) pending → dead`;claim 原子 + ack 归属校验 + lease 过期重投 + `max_deliveries` 防毒消息无限重投——每个状态转移都有明确触发与不变量。
3. **原子快照 vs 事件日志**:任务队列用原子快照(`os.replace`,一致性视图),会话流用 JSONL append(可重放审计)——按「语义要历史还是要当前态」选型。
