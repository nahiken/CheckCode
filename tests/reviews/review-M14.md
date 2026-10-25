# M14 双轴审计

- 审计对象:commit `aa6a677`(跨进程邮箱 + Agent 间寻址消息)
- 审计方式:独立第三方,**全部结论以实跑输出为据**;对每个承重断言做变异测试(改坏 → 确认变红 → 改回 → md5 恢复)
- 审计环境:Windows 11 / Python 3.11.14(项目 `.venv`)/ `sys.platform == "win32"`(即 `msvcrt.locking` 分支)
- 审计结论日期:2026-09-11

---

## 结论(一句话 + 是否建议继续)

M14 的三项承重承诺——**锁内 reload 消除丢失更新**、**OS 文件锁真跨进程互斥**、**`os.replace` 双向写读重试**——经变异测试逐条证实为真且每条都有真进程测试兜底,**未发现 blocker**;但发现 **1 个中等正确性缺陷(`reply` 无幂等守卫,可覆写已 DONE 的回执并把 `attempts` 归零)**、**1 项规格未兑现(规格明确要求承重测试断言「总 attempts 有界」,实际该文件对 `attempts` 零提及)**、以及 5 条轻量问题。

**建议:可继续 M15**,但应先补 `reply` 的幂等守卫与「attempts 上界」断言——两者都是低成本高收益,且前者违反本模块自己反复强调的幂等纪律。

---

## 轴一:Standards(代码质量 / 一致性 / 可维护性)

**做得好的:**

- `mailbox/locking.py` 模块 docstring 把「为什么不用锁文件」的三个理由(陈旧锁、依赖、跨平台)写清楚了,且与 `FEATURE_COMPLETION_PLAN.md` §M14 的取舍一致——**设计理由落地在代码注释里,不是只在计划书里**。
- 平台分支的 mypy 处理(`fcntl: Any = None` / `msvcrt: Any = None`)有注释解释「为什么 Windows 上 strict 也能过」,这是有意识的可移植性工程,不是碰巧能跑。
- `MailboxStore.lock()` 用「懒打开 + 常驻句柄」并把 `close()` 显式给出,`close()` 语义在 docstring 里写明「不调用也由 OS 兜底」——诚实。
- `_critical` 的 asynccontextmanager 结构使「忘记同步」在结构上不可能:所有 8 个变更方法都走同一入口(`submit/claim/ack/nack/recover_deliveries/send_message/reply/claim_inbox`),这是**单一骨架**而非八处复制。符合 MEMORY 里记录的 M10 教训「有意的同构复制也要抽公共骨架」。
- 承重测试对「测试可能是空的」有自省:`test_all_worker_processes_are_used` 的 docstring 明确写「如果只有一个进程真的跑起来,『无重复认领』会因为没人竞争而白白通过 —— 测试是空的」,并用「每进程恰好领 10 条」把「真并发」变成可断言事实。这是高于平均水准的测试设计。

**做得不够的:**

- **`_is_inbox_candidate` 的 docstring 与实现不符**(见 轻-4)。docstring 描述了完整谓词(含 `recipient` 判定),函数体只有半句。
- **`reply` 是本模块唯一无幂等守卫的写方法**(见 中-1):`_submit` / `send_message` 都先查 `existing` 再返回,`reply` 直接 `self._tasks[reply_id] = rec`。同构纪律在此处断了。
- **非重入文件锁无快速失败**:同一 store 句柄嵌套取锁会阻塞满 `DEFAULT_LOCK_TIMEOUT=30s` 才抛错,没有 owner 标记或断言快速拒绝(见 轻-5)。当前无生产路径嵌套,属**潜伏**问题,但没有护栏防止未来引入。
- `state()` 无锁读取(仅 `__init__` 载入后的内存视图)。对 CLI `inbox` 这种短命进程没问题,但作为**公共 API** 暴露时没有标注「这是快照视图,可能陈旧」。属文档级建议。

---

## 轴二:Spec(是否真的兑现规格)

逐条核对 `FEATURE_COMPLETION_PLAN.md` 行 124–140:

| 规格要求 | 兑现? | 证据 |
|---|---|---|
| stdlib `fcntl.flock` / `msvcrt.locking`,不用 `filelock`/`portalocker`/`O_EXCL` 锁文件 | ✅ | `mailbox/locking.py:47-70`;全仓 grep `filelock\|portalocker` 仅命中注释;`pyproject.toml` 依赖表无它们(见附 A) |
| 锁 `.lock` 文件而非快照本身 | ✅ | `store.py:104` `lock_path = path.with_name(name + ".lock")`;`test_lock_file_is_separate_from_snapshot` |
| 修掉 `_persist` 整表序列化的丢失更新;每个变更方法锁内先 reload 再改 | ✅ | `queue.py:99-102`;变异测试 M1 变红;4 进程各投 15 条 → 60 条全在 |
| `TaskRecord` 加 `kind`/`sender`/`recipient`/`corr_id` | ✅ | `models.py:60-63`,全部有默认值 |
| `send_message` / `claim_inbox` / `reply` | ✅ | `queue.py:262/294/320` |
| lease/ack/nack/max_deliveries/recover **沿用不 fork** | ✅ | 未新增第二套状态机;`_release_and_maybe_dead` 被 nack 与 recover 共用;`_claim(inbox=True)` 复用同一条 claim 内核 |
| 承重测试:真多进程(spawn),断言无重复 claim、N 条全完成、快照可解析、**总 attempts 有界** | ⚠️ **部分** | 前三条有(3 连跑 6/6 绿);**「总 attempts 有界」无任何断言** —— 见 中-2 |
| 风险:`docs/14-*.md` 明说全量重写不可当 broker | ✅ | `docs/14-m14.md` §6 第 2 条原文:「**不要**据此宣称可扩展为 broker」 |

**规格兑现度:7/8 完全兑现,1 项部分(承重测试缺 attempts 上界断言)。**

---

## 发现的问题(按 严重/B1-blocker、中、轻、测试 分级)

### 【中-1】`reply()` 无幂等守卫:覆写既有回执(含 DONE)→ 回执复活 + `attempts` 归零

**现象:**`MailboxQueue.reply()` 与 `_submit`/`send_message` 不同,它**先不查既有记录**,直接 `self._tasks[reply_id] = rec`。当同一条父消息被重投并再次走 `process_inbox` 时,会用同一个 `f"{message_id}:reply"` 覆写回执记录——即使该回执**已经被收件人 ack(DONE)**。后果:① 已终结的 DONE 被回退成 PENDING(状态机倒流);② `attempts` 归零、`done_at` 清空(审计字段丢失);③ 发件人**第二次**收到同一条回执。

**证据(实跑):**
```
$ ./.venv/Scripts/python.exe - <<'EOF'
... bob claim_inbox + reply 后「崩溃」(不 ack) → 租约过期 recover → 再次 reply 同一 reply_id ...
EOF
reply after alice ack: done attempts= 1 done_at= 1000
recover -> 1 | m-1 = pending
REPLY RESURRECTED?: pending attempts= 0 done_at= None
alice receives reply a 2nd time: m-1:reply | attempts= 1
```
`DONE → PENDING`、`attempts 1 → 0`、`done_at 1000 → None` 三处同时发生。

**复现步骤:**
1. `send_message("m-1", …, sender="alice", recipient="bob")`
2. `claim_inbox("bob")` → `reply("m-1", "m-1:reply", …)`,然后**不 ack**(模拟 reply 与 ack 之间崩溃)
3. `claim_inbox("alice")` → `ack("alice","m-1:reply")` → 回执 DONE
4. 推进时钟 → `recover_deliveries()` → m-1 回 pending
5. `claim_inbox("bob")` → `reply("m-1", "m-1:reply", …)`(同一个 reply_id)
6. 观察 `state()["m-1:reply"]`:status/pending、attempts/0、done_at/None

**建议修法:**把 `reply` 改成与 `send_message` 同构的幂等写法——`existing = self._tasks.get(reply_id)` 命中即返回既有副本(不覆写),语义上「一条父消息的回执只生成一次」;如需允许「更新回执」,应新增显式方法并禁止回退终态。同时在 `process_inbox` 中,`deliver` 返回回执前先检查回执是否已存在,避免无谓的重复回执。

**缓解事实(诚实标注):**该缺陷需要「reply 与 ack 之间崩溃」+「父消息被 lease 重投」两个条件同时成立;而当前生产代码**从不调用** `recover_deliveries`(见「未覆盖/存疑」),所以眼下不会在生产触发。它仍是**队列库自身契约**下的真实缺陷,且违反本模块的幂等纪律。

---

### 【中-2】承重测试未断言规格明确要求的「总 attempts 有界」

**现象:**`FEATURE_COMPLETION_PLAN.md` 行 136–138 逐字要求 `test_cross_process.py` 断言四项,其中末项是「**总 attempts 有界**」。实测该文件对 `attempts` **零次提及**;全 `tests/mailbox/` 也**没有任何 `attempts <= N` 形式的上界断言**。现有最接近的是 `test_queue.py::test_recover_redelivery_to_dead_at_max_deliveries`,它跑固定 3 轮循环后断言 `status == "dead"`——**测的是症状(终态),不是规格(界)**。

**证据(实跑):**
```
$ grep -c "attempts" tests/mailbox/test_cross_process.py
0
$ grep -rn "attempts" tests/mailbox/*.py | grep "assert"
tests/mailbox/test_messages.py:168:        assert claimed[1].attempts == 2
tests/mailbox/test_persistence_recovery.py:55:        assert rec.attempts == 2
tests/mailbox/test_queue.py:33/62/128/173:  == 0 / == 1 / == 2 / == 2
```
—— 全部是**等值**断言,无一处是**上界**。

另:`docs/14-m14.md` §7 的承重测试描述也只列了「无重复 claim、4 进程各领 10 条、丢失更新探针、写读并发双向哨兵」,未列 attempts 界——**文档与测试同时漏了这一项,不是笔误**。

**为什么这不是吹毛求疵:**at-least-once 队列最危险的失效模式是「毒消息无限重投」(M6 审计 F1 就是这个),唯一的守卫是 `attempts` 上界。规格把它列为承重断言,恰恰因为它是**唯一能证明守卫在多进程争抢下仍然成立**的断言——单进程等值断言证不了「跨进程重投不会绕过 max_deliveries」。

**复现步骤:**`grep -c attempts tests/mailbox/test_cross_process.py` → 0。

**建议修法:**在 `test_cross_process.py` 增加一条真进程用例:N 个进程反复 claim 且**永 not ack**(模拟崩溃),期间穿插 `recover_deliveries`,结束后断言 `max(rec["attempts"] for rec in final.values()) <= max_deliveries`,且所有记录终态 ∈ {done, dead}。这样才把「有界」变成跨进程事实。

---

### 【轻-1】`submit` / `send_message` 共用 id 命名空间且无 `kind` 守卫 → 跨车道同 id 静默 no-op

**现象:**两条车道共用一个 `task_id` 命名空间,幂等检查只比对 id、不比对 `kind`。若同一个 id 先被用作消息、再被用作任务(或反之),第二次调用**返回既有记录**、不报错、也不创建——CLI 仍打印「已入队」。

**证据(实跑):**
```
submit('X') after message X exists -> kind = message | payload = {'text': 'msg'}
  -> task silently NOT created; claim() sees: None
send_message('Y') after task Y exists -> kind = task | payload = {'task': 'do work'}
  -> message silently NOT created; claim_inbox('b') sees: None
```

**复现步骤:**`q.send_message("X",…,kind=MESSAGE)` 后 `q.submit("X", {"task":"do work"})`,看返回值 `kind == "message"`。

**建议修法:**幂等命中时校验 `kind` 一致,不一致抛 `ValueError("id 已被另一车道占用")`;或给两条车道加 id 前缀命名空间。至少不要让 CLI 在什么都没入队时打印成功。

---

### 【轻-2】CLI `--to ""` 产生**永久不可投递**的孤儿消息

**现象:**`--to ""` 被存成 `recipient: ""`(不是 `None`),于是既不是广播、也不可能被任何真实 agent 领走——任何具名 agent 的 `claim_inbox` 都看不见它,而它又是 PENDING(`_recover_deliveries` 只处理 CLAIMED),**永远不会被重投**。它是一条静默黑洞。

**证据(实跑):**
```
$ ./.venv/Scripts/python.exe -m cli.main send --queue "$Q" --id m-empty --from alice --to "" --text "orphan?"
[send] m-empty →  已入队 ...          # 箭头后为空,回显已经露馅
$ ... | python -c "... snapshot ..."
"m-empty": {..., "kind": "message", "recipient": "", "status": "pending", ...}
$ python -c "claim_inbox('bob')"
claim_inbox('bob') -> m-bc | recipient= None      # bob 只拿到广播,m-empty 看不见
claim_inbox('') -> m-empty                        # 只有空 id 才拿得到
```

**复现步骤:**`mewcode send --queue Q --id m --from a --to "" --text x`,随后 `mewcode inbox --queue Q --agent bob` → 不显示;文件里却有一条 `recipient: ""` 的 pending 记录。

**建议修法:**`_handle_send` 里 `recipient = args.recipient or None`(空串归一为广播),或在 argparse 层拒绝空串。

---

### 【轻-3】CLI `--task` 与 `--to` 同时给:静默忽略 `--to`,且回显「广播」误导

**现象:**`--task` 分支先返回,`--to` 被无声丢弃(合理),但回显固定走 `broadcast=True` 分支打印「广播」——**任务不是广播**,而且 `--from` 也被丢弃。

**证据(实跑):**
```
$ ./.venv/Scripts/python.exe -m cli.main send --queue "$Q" --id t1 --from alice --to bob --text "hello" --task
[send] t1 广播 已入队 ...     # kind 实际是 task,回显却是「广播」
$ ... snapshot ...
"t1": {..., "kind": "task", "sender": null, "recipient": null, ...}
```
`topic`: `--task` 分支下 `sender`/`recipient` 全为 null,回显「广播」纯属误导。

**复现步骤:**见上。

**建议修法:**`--task` 分支回显「任务」;或在 argparse 用互斥组禁止 `--task` 与 `--to` 同用。

---

### 【轻-4】`_is_inbox_candidate` docstring 与实现不符

**现象:**docstring 写「非任务类,**且(FIFO 池无归属)或(定向给我)**」,但函数体只有 `return rec.kind != TaskKind.TASK`——后半个谓词实际在 `_next_inbox` 里。读代码的人会以为过滤在这里,而在这里做变异测试(见 M5)只会红一条用例。

**证据:**`mailbox/queue.py:132-134` 对照 `queue.py:145-151`。

**建议修法:**docstring 改为「只判类别;寻址判定见 `_next_inbox`」,或把 `recipient` 判定也搬进来(但需传入 `agent_id`)。

---

### 【轻-5】非重入文件锁无快速失败:同 store 句柄嵌套取锁阻塞满 30s

**现象:**`file_lock` 非可重入且无 owner 标记。同一 `MailboxStore` 句柄被嵌套取锁时,不会快速报错,而是**轮询到 `DEFAULT_LOCK_TIMEOUT=30s` 才抛 `MailboxLockTimeout`**。今天没有生产路径嵌套(已逐一核对:8 个 `_critical` 调用点全部是叶子,`with self._store.lock()` 在整个仓库只出现在 `queue.py:99` 一处),属**潜伏**风险。

**证据(实跑):**
```
nested via same store handle: TIMEOUT after 30.00s (non-reentrant)
2nd store instance TIMEOUT after 0.31s (mutual exclusion holds)
different lock file: both acquired concurrently (independent locks) - as expected
```

**复现步骤:**`s = MailboxStore(p); with s.lock(): with s.lock(): ...` → 30s 后抛错。

**建议修法:**`MailboxStore.lock()` 记录当前持有者(线程 id / 一个自增 token)与深度,同持有者嵌套时直接抛 `RuntimeError("store.lock() 不可重入")`——把 30s 静默等待变成**立刻可见的编程错误**。这比事后排查「为什么偶尔卡 30 秒」便宜得多。

---

### 【测试-1】CLI 测试未覆盖三个已知输入边界

**现象:**`tests/test_cli_mailbox.py`(11 条)覆盖了定向/广播/`--task`/跨进程幂等/必填项,但**没有** `--to ""`、`--task` + `--to` 同用、`inbox --agent ""` 三条——而这三条恰好都产生非预期行为(轻-2 / 轻-3)。

**证据:**`grep -n '""' tests/test_cli_mailbox.py` 无命中;上述三条边界仅由本次审计手工构造发现。

**建议修法:**补三条参数边界用例(空串归一/互斥/回显文案),把轻-2、轻-3 变成回归。

---

### 【测试-2】`reply` 覆写路径无测试覆盖

**现象:**`test_messages.py::TestReply` 只有「回到原发件人」「父不存在抛错」「父无 sender 抛错」三条,`test_worker_inbox.py` 也没有「同一条父消息被处理两次」的用例。中-1 因此无回归网。

**建议修法:**补 `test_reply_is_idempotent`:同一 `reply_id` 调两次 → 第二次不覆写;以及「回执 DONE 后父消息重投再回执 → 回执仍 DONE」。

---

## 已实证的「没问题」项(附证据,供后续复用)

1. **锁内 `reload` 真的在锁内,且真的承重**(变异 M1)。`queue.py:99-102` 的 `with self._store.lock(): self._load(); yield; self._persist()` 结构正确;删掉 `self._load()` 后 `test_cross_process.py` **4 条变红**(丢失更新探针实测 30 条只剩 9 条)。
2. **`_replace_with_retry` / `_read_with_retry` 不是伪造的问题,且两侧各自必要**(变异 M2/M2a/M2b)。在**完全无重试**的原始版本上实测(1 写 + 3 无锁读,150 写 / 3×500 读):
   ```
   NO-RETRY write failures: 54/150  Counter({'PermissionError': 54})
   NO-RETRY total read failures: 6/1500
   final snapshot size = 96 (expected 150 if no write failed)
   ```
   两侧分别单独去掉时,**恰好各自那一侧的哨兵用例变红、另一侧保持绿**(完美隔离)。开发者没有伪造问题;冲突确实是双向的。
3. **`TaskRecord` 新字段向后兼容**。手工构造无 `kind/sender/recipient/corr_id` 的 M6 时代 JSON,`MailboxQueue` 正常载入、`kind` 缺省为 `task`、可被 `claim` 正常领走,无需迁移脚本。
4. **车道隔离是双向的,且三处过滤都由测试兜底**(变异 M3/M4/M5)。`claim` 面对「只有消息+回执」的队列返回 `None`;`claim_inbox` 面对「只有任务」的队列返回 `None`;移除 `_next_pending` 的 kind 过滤 → 4 条红;移除 `_next_inbox` 的 `recipient` 过滤 → 2 条红;移除 `_is_inbox_candidate` 的 kind 过滤 → 1 条红。
5. **已 ack 的消息不会复活、dead 不会复活**——直接回应怀疑清单第 5 条。实测:acked 消息在 `recover_deliveries(now=999999)` 后仍 `done`(返回 0);dead 消息仍 `dead`。广播被重投 3 次后转 `dead`,**重复投递是 at-least-once 允许的,且被 `max_deliveries` 界定,不是无界重复**。
6. **`recover_deliveries` 对两条车道都正确**:任务重投时 `recipient` 保持 `None`(kind 守卫生效),消息重投时才清空定向。实测 `t-1 recipient=None` / `m-5 recipient=None`,二者都被正确回 pending。
7. **`_critical` 异常安全**:在临界区内抛异常时,快照**逐字节未变**(`_persist` 未执行),锁**立即释放**(下一次操作 0.000s 完成,无 30s 悬挂)。
8. **Windows `msvcrt` 的位置耦合被正确封装**。实测确认区间锁按 offset 匹配(锁住 byte[0] 后,锁 byte[1] 仍成功——耦合是真的),而 `_try_lock`/`_unlock` 双方都 `seek(0)` 锁 1 字节,位置一致;且在**错误位置 unlock 会抛 `OSError` 而不是静默释放**——即「位置不一致」会立刻暴露,不会变成静默的锁泄漏。
9. **无违规依赖**:全仓 `grep filelock\|portalocker` 只命中 `locking.py` 的注释;`pyproject.toml` 运行时依赖仍只有 pydantic/httpx/python-dotenv/mcp/fastapi/uvicorn。
10. **承重测试不 flaky**:`test_cross_process.py` 连续 3 次 `6 passed`(本机)。
11. **全量测试健康**:`pytest -q`(无随机序)退出码 0,收集 567 条——与「520 (M13) + M14 新增」一致。

---

## 变异测试记录(改了什么 → 是否变红 → md5 恢复证据)

| # | 改坏的源码 | 预期变红的用例 | 实测 | 恢复 |
|---|---|---|---|---|
| M1 | `queue.py::_critical` 删掉锁内 `self._load()` | `test_cross_process.py` 丢失更新探针 | ✅ **4 failed, 2 passed**(30 条只剩 9 条) | ✅ md5 `bc024bed…` |
| M2 | `store.py` **两侧**重试都改成直接 `raise` | `TestWindowsReplaceContention` 两条 | ✅ **2 failed** | ✅ md5 `2288dbb9…` |
| M2a | `store.py` **只**去写侧重试 | 只 `test_locked_writer_survives_unlocked_readers` | ✅ **1 failed, 1 passed**(另一侧保持绿) | ✅ md5 `2288dbb9…` |
| M2b | `store.py` **只**去读侧重试 | 只 `test_unlocked_reader_survives_locked_writer` | ✅ **1 failed, 1 passed**(另一侧保持绿) | ✅ md5 `2288dbb9…` |
| M3 | `queue.py::_next_pending` 删掉 `rec.kind == TaskKind.TASK` | 车道隔离 4 条 | ✅ **4 failed, 20 passed** | ✅ md5 `bc024bed…` |
| M4 | `queue.py::_next_inbox` 删掉 `recipient is None or == agent_id` | 定向寻址 2 条 | ✅ **2 failed, 14 passed** | ✅ md5 `bc024bed…` |
| M5 | `queue.py::_is_inbox_candidate` 改成 `return True` | `test_claim_inbox_does_not_take_tasks` | ✅ **1 failed, 23 passed** | ✅ md5 `bc024bed…` |

**恢复证据(审计结束时):**
```
$ md5sum mailbox/queue.py mailbox/store.py mailbox/locking.py mailbox/models.py
bc024bed6238923eb12044483a91fa60 *mailbox/queue.py
2288dbb90c8f2c43f4308dae9e88c77d *mailbox/store.py
9672b90158a50db2d4342498e32e6391 *mailbox/locking.py
6603e11845063f00a821a9524fe1f2a5 *mailbox/models.py
$ git status --porcelain mailbox/
(空)
```
`mailbox/` 工作区 git 状态干净;审计期间临时创建的 `measure_noretry.py` 已删除。

---

## 未覆盖 / 存疑(诚实边界)

1. **`recover_deliveries` 没有任何生产调用点。** 全仓搜索只命中 `mailbox/queue.py`(定义)与 `tests/`。`cli/main.py` 的常驻 worker 循环只做 `process_once` + `process_inbox`,**从不回收过期租约**。因此「崩溃恢复 + lease 重投」目前是**库能力**而非**产品行为**:一个 worker 在 claim 后崩溃,其任务会永久停留在 `claimed`,不会有人重投。这是 M6 遗留、非 M14 引入,但它直接决定了中-1 的爆炸半径(今天为零),也影响 M14「消息复用同一套 lease 重投」这句对外表述的强度——**建议在 `docs/14-m14.md` §6 边界里补一句**。
2. **未在 POSIX 上跑过 `fcntl` 分支。** 本审计环境是 Windows,所有锁结论都是 `msvcrt` 分支的实测。`fcntl.flock` 分支(含「`flock` 与 `open` 不互斥,故 POSIX 上重试循环是空转」这一断言)只有代码审读,无实跑。若 CI 有 Linux runner,建议在那里跑一次 `tests/mailbox/test_cross_process.py`。
3. **`lease_until is None` 的 CLAIMED 记录会永久卡住。** `_recover_deliveries` 有 `rec.lease_until is not None` 守卫;一份被外部工具改写成 `status=claimed` 但无 `lease_until` 的旧快照,该记录既不会被重投也不可再 claim。属极端手工损坏场景,仅记录。
4. **`process_inbox` 的 `reply()` 不在 `try` 内。** 第 110–119 行只把 `deliver(record)` 包在 try 里,`await self.queue.reply(...)` 在其外。若 `reply` 抛错(当前需要父消息 `sender is None`,而 `send_message` 的 `sender` 是必填参数,故**当前不可达**),异常会穿出 `process_inbox`,消息留在 claimed 且无 nack 记录。属**防御性缺口**,非现存 bug。
5. **未做长时间压力测试。** 承重测试是「单次真并发争抢」量级(24/40 条、4 进程)。未验证在数十秒持续争抢、或 8+ 进程下是否出现锁饥饿或 `MailboxLockTimeout`。本机 3 连跑稳定,但不等于长时间稳定。

---

## 附:实际跑过的命令与关键输出

### A. 基线与依赖

```
$ git log --oneline -3
aa6a677 M14: 跨进程邮箱(OS 文件锁 + 锁内 reload)+ Agent 间寻址消息
689327b M13: 生产启用 parallel_safe 并发…
a4b0ee8 M12: run_id + Trace JSONL + 运行摘要 + artifact 端点…

$ grep -rn "filelock\|portalocker" --include=*.py --include=*.toml . | grep -v /.venv/
./mailbox/locking.py:7:- 不为单一调用点引入运行时依赖(`filelock`/`portalocker`);…

$ sed -n '/dependencies/,/]/p' pyproject.toml
dependencies = [ "pydantic>=2.7", "httpx>=0.27", "python-dotenv>=1.0",
                 "mcp>=2.1.1", "fastapi>=0.115", "uvicorn>=0.30" ]
```

### B. 基线测试

```
$ ./.venv/Scripts/python.exe -m pytest tests/mailbox -p no:randomly
67 passed in 37.06s

$ ./.venv/Scripts/python.exe -m pytest -q -p no:randomly     # 全量
（退出码 0,进度条 8 行全绿）
$ pytest -q --co | (per-file 汇总求和)
files 61  total_collected 567
```

### C. 变异测试(逐条)

```
# M1
$ python - <<'EOF'  # 删掉 _critical 里的 self._load()
EOF
MUTATED: removed in-lock self._load()
$ pytest tests/mailbox/test_cross_process.py -p no:randomly
FAILED TestCrossProcessClaim::test_no_task_claimed_twice_across_processes
FAILED TestCrossProcessClaim::test_all_worker_processes_are_used
FAILED TestCrossProcessLostUpdate::test_concurrent_submitters_lose_nothing
FAILED TestCrossProcessLostUpdate::test_submitters_and_claimers_share_one_snapshot
E  AssertionError: 投递总数不符: 9      # 期望 30
4 failed, 2 passed in 18.37s
$ cp /tmp/queue.py.bak mailbox/queue.py && md5sum mailbox/queue.py
bc024bed6238923eb12044483a91fa60  ✓

# M2(两侧都去)
NO-RETRY write failures: 54/150  Counter({'PermissionError': 54})
NO-RETRY total read failures: 6/1500
final snapshot size = 96 (expected 150 if no write failed)
$ pytest ...::TestWindowsReplaceContention -p no:randomly
2 failed in 7.53s

# M2a(只去写侧)
1 failed, 1 passed in 7.86s
# M2b(只去读侧)
1 failed, 1 passed in 14.29s

# M3
4 failed, 20 passed in 1.21s
# M4
2 failed, 14 passed in 0.90s
# M5
1 failed, 23 passed in 0.99s
$ cp /tmp/queue.py.bak mailbox/queue.py && md5sum mailbox/queue.py
bc024bed6238923eb12044483a91fa60  ✓
```

### D. 向后兼容(M6 旧快照)

```
$ python - <<'EOF'   # 写入无 kind/sender/recipient/corr_id 的记录
EOF
LOADED OK: {"t-old": {..., "kind": "task", "sender": null, "recipient": null, "corr_id": null}, ...}
claim -> t-old
kind of old rec = task | sender = None | recipient = None
```

### E. 车道隔离与重投语义

```
(a) acked msg revived? done recovered= 0
(b) dead msg: dead recovered= 0
(c) broadcast delivery count: 3 ['b', 'c', 'd'] | attempts= 3 | status= dead
(d) claim() on msg+reply lane: None | claim_inbox(b) -> m-4
(e) recovered both: 2
    t-1: pending recipient= None
    m-5: pending recipient= None

$ grep -c "attempts" tests/mailbox/test_cross_process.py
0
```

### F. `reply` 覆写(中-1)

```
reply after alice ack: done attempts= 1 done_at= 1000
recover -> 1 | m-1 = pending
REPLY RESURRECTED?: pending attempts= 0 done_at= None
alice receives reply a 2nd time: m-1:reply | attempts= 1
```

### G. 命名空间冲突(轻-1)

```
submit('X') after message X exists -> kind = message | payload = {'text': 'msg'}
  -> task silently NOT created; claim() sees: None
send_message('Y') after task Y exists -> kind = task | payload = {'task': 'do work'}
  -> message silently NOT created; claim_inbox('b') sees: None
```

### H. CLI 边界(轻-2 / 轻-3)

```
$ python -m cli.main send --queue "$Q" --id t1 --from alice --to bob --text hello --task
[send] t1 广播 已入队 ...                    # kind 实际是 task
$ python -m cli.main send --queue "$Q" --id m-empty --from alice --to "" --text "orphan?"
[send] m-empty →  已入队 ...                 # 箭头后为空
$ python -m cli.main inbox --queue "$Q" --agent ""
[inbox] m-empty (定向) from=alice: orphan?
[inbox] m-bc (广播) from=alice: hi all
$ # 具名 agent 领不到:
claim_inbox('bob') -> m-bc | recipient= None
claim_inbox('') -> m-empty
```

### I. 锁语义

```
nested via same store handle: TIMEOUT after 30.00s (non-reentrant)
2nd store instance TIMEOUT after 0.31s (mutual exclusion holds)
different lock file: both acquired concurrently (independent locks) - as expected

$ python -c "msvcrt 区间锁位置耦合探针"
locked byte[0] via handle A
  -> lock at offset 1 SUCCEEDED (range locks are per-offset: coupling is real)
  -> re-lock byte[0] FAILED as expected: [Errno 13] Permission denied
unlock at offset 5 raised: [Errno 13] Permission denied
  -> byte[0] still locked after wrong-offset unlock: [Errno 13] Permission denied
```

### J. 异常安全

```
raised inside _critical: unknown message to reply to: ghost
snapshot unchanged after exception: True
lock re-acquired in 0.000s -> no lock leak
final keys: ['t-1', 't-2']
```

### K. 稳定性 + 最终完整性

```
$ for i in 1 2 3; do pytest tests/mailbox/test_cross_process.py -p no:randomly; done
...... [100%]   ×3   (6 passed each)

$ md5sum mailbox/queue.py mailbox/store.py mailbox/locking.py mailbox/models.py
bc024bed6238923eb12044483a91fa60 *mailbox/queue.py
2288dbb90c8f2c43f4308dae9e88c77d *mailbox/store.py
9672b90158a50db2d4342498e32e6391 *mailbox/locking.py
6603e11845063f00a821a9524fe1f2a5 *mailbox/models.py
$ git status --porcelain mailbox/
(空)
```
