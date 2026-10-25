# M14 测试评估报告

> 阶段:M14(跨进程邮箱 + Agent 间寻址消息)。评估时间:2026-09-11。
> 基线提交:M13 末(`689327b`)。测试口径:CI 全离线;本报告只记本地确定性套件。
> 「581」为 M14 之后的**全项目口径**。

## 测试规模

### Python(pytest)—— 520 → 581(+61)

| 文件 | 本段新增 | 覆盖内容 |
|---|---|---|
| `tests/mailbox/test_lock.py` | **+8** | 进程内互斥(不排他即超时)/超时上报/异常路径释放/锁文件与快照分离/句柄常驻复用;**真跨进程**:子进程持锁 0.8s 期间父进程必须超时、释放后可重取 |
| `tests/mailbox/test_messages.py` | **+21** | 定向只有收件人可见/广播任一人领且只领一次/FIFO/幂等(`send_message`)/`corr_id` 缺省与显式/**车道隔离双向**/回执回到父 sender 且 corr_id 贯穿/回复不存在的消息→ValueError/回复无 sender 的记录→ValueError/**重投失去定向**/广播重投仍广播/毒消息 dead/消息经快照持久化 |
| `tests/mailbox/test_cross_process.py` | **+7** | **承重(真多进程)**①24 条 4 进程争抢:无重复认领、无遗漏、全 done;②4 进程**各恰好领 10 条**(要求都真参与);③**丢失更新探针**:4 进程各投 15 条 → 必须 60 条;④投递与认领**交错**并发:30 条状态自洽;⑤**写侧哨兵**:无锁读下写者零失败;⑥**读侧哨兵**:持续写下读者零失败;⑦**attempts 上界**(审计中-2):3 进程只 claim 不 ack + 穿插 recover → `max(attempts) <= max_deliveries` |
| `tests/assembly/test_worker_inbox.py` | **+9** | 消息投递并 ack/deliver 返回值成为回执且发件人能领到/不传 deliver 只 ack/空箱不报错/deliver 异常→nack 回队且记错/**回执失败→nack(不穿出)**/回执落快照/任务车道不吃消息/两车道各取所需 |
| `tests/test_cli_mailbox.py` | **+16** | send:定向(跨进程可见快照)、广播、`--task` 进任务车道、**跨进程幂等**、缺 `--id`→2、**空 `--to` 归一为广播**、**`--task`+`--to` 拒绝**、**跨车道同 id 报错**、`--task` 回显「任务」;inbox:按收件人过滤、**只读**(查完仍 pending)、陌生人只见广播、无广播时为空、忽略任务、缺 `--agent`→2、空 `--agent` 只见广播 |
| `tests/mailbox/test_lock.py`(补) | +2 | **嵌套取锁立刻 RuntimeError**(审计轻-5)、嵌套报错后深度不泄漏 |
| `tests/mailbox/test_messages.py`(补) | +5 | **`reply` 幂等**(不覆写)、**已 DONE 回执不复活**、跨车道 id 冲突 2 条、同车道仍幂等 |
| `tests/mailbox/_mp.py` | **新(非测试)** | spawn 子进程入口(spawn 只能 pickle 模块级函数) |
| **全项目累计** | **581 passed** | M14 后 |

### TypeScript(vitest)—— 32(不变)

M14 是后端/CLI 的跨进程能力,前端不涉及。

## 测试方法学(本阶段最有价值的部分)

### 1. 「同进程并发」证明不了文件锁

文件锁保护的是**另一个进程**的临界区。单进程里根本没有「另一个进程」,
所以 `asyncio.gather` 无论怎么测都只能证明 `asyncio.Lock`。
承重测试一律 `multiprocessing.get_context("spawn")` 起真进程(Windows 无 fork)。

副作用:子入口必须是**模块级函数**(spawn 按「模块名 + 函数名」重新 import),
故单独建 `tests/mailbox/_mp.py`,不在测试文件里定义闭包。

### 2. 「所有进程都分到活」不能靠调度运气

初版写的是「每个进程都领到 >0 条」——**实测失败**:一个快进程一路领到底,
另外三个空手而归。这不是 bug,是调度运气,这样的断言会 flaky。

改成**每个进程恰好领 10 条**(`limit=10`):40 条要分完,就**必须**四个进程都真的参与争抢。
把「运气」变成「确定性事实」。

### 3. 变异测试(证明测试真的能失败)

| 改坏什么 | 期望变红 | 实测 |
|---|---|---|
| 去掉 `_critical` 里的锁内 `self._load()` | 丢失更新探针 | ✓ 60 条 → **40** 条,且 `os.replace` 直接抛 `PermissionError` |
| 去掉写侧重试 `_replace_with_retry` | 写侧哨兵 + 认领测试 | ✓ 变红 |
| 去掉读侧重试 `_read_with_retry` | 读侧哨兵 | ✓ 变红 |

三次变异改完都已 `md5sum` 校验恢复原状。

> 注:去掉锁内 reload 时,失败**首先表现为 `os.replace` 的 PermissionError**,
> 而不是干净的「条数不对」。这本身是个发现:并发缺陷的**症状**未必是它的**本质**,
> 只看「报了什么错」会误判根因。

### 4. 双向哨兵(实测教训)

Windows 上 `os.replace` 与 `open` 互斥,且冲突**双向**:

| 版本 | 写失败 | 读失败 |
|---|---|---|
| 原始(无重试) | 119/150 | 0 |
| 只加写侧重试 | 17/150 | **15/1500** |
| 两侧都加 | 0/150 | 0/1500 |

第二步说明:**只修一侧会把故障推到另一侧**。所以「写能扛住读」和「读能扛住写」
是**两个独立保证**,必须各有一条测试,不能因为前者通过就假定后者成立。

### 5. 测试先写,逮到真 bug

`_next_pending` 初版没过滤 `kind`,worker 会把 agent 间消息当任务领走执行。
`test_claim_does_not_take_messages` 在实现后立刻变红 → 当场修。

## 静态检查

- `ruff check .` → All checks passed
- mypy strict → **71** 源文件无问题(70 → 71,新增 `mailbox/locking.py`)

> `mailbox/locking.py` 的跨平台 import 需把**未导入的那个模块**显式标注为 `Any`:
> mypy 在 Windows 上把 `else` 分支判为不可达且拒绝解析 `fcntl`。已实测
> `--platform win32` 与 `--platform linux` 下 strict 均通过。

## 为什么有些东西**没有**测(诚实边界)

- **真实网络文件系统 / 多机器**:OS 锁依赖本地文件系统语义,跨网络盘不成立 —— 不在测试范围。
- **锁内阻塞对事件循环的影响**:单次毫秒级,未做性能基准(有意的规模取舍,见
  `docs/14-m14.md` §6)。
- **`msvcrt.locking` 的进程级语义**:测试在 Windows 上跑(即 `msvcrt` 分支);
  POSIX 分支(`fcntl`)的逻辑由 CI 环境覆盖,本地未跑。
- **`recover_deliveries` 无生产调用点**(审计补充):`cli/main.py` 的 worker 循环只做
  `process_once`/`process_inbox`,从不回收过期租约 → 「崩溃恢复 + lease 重投」目前是
  **库能力而非产品行为**。承重测试是**直接驱动队列 API** 来验证该能力的(M6 遗留)。
