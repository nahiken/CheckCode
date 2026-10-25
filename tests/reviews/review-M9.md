# M9 Review 记录(双轴 code-review)

> 审查范围:W5→`2588735`、W4→`e473dbc`、W6→`a312089`、W2→`e1f8478`、W3→`2e243dc`、
> W1→`a9f58ea`、W7→`74552c8`、W8→`e132a89`(M9 全 8 项)。
> 审查方式:每 W 实现后独立 review(工作流第 ④ 步)+ 二轮自查;此处汇总为 Spec(规格符合度)+
> Standards(编码规范)双轴审计记录。审查时间:2026-09-09。状态:**全部关闭**(见修复表)。

## Spec 轴(规格符合度)

### (a) 与 plan / PROJECT_VERIFICATION 的差距对照

| # | 规格依据 | 验证 |
|---|---------|------|
| M1 | 8 项差距逐项关闭 | ✅ 代码侧 W5/W4/W6/W2/W3/W1 各独立提交,测试从 410→427 全绿;W7 文档、W8 脚本 |
| M2 | #1 浏览器审批走 **Web/API**,loop 不加 park | ✅ `api/app.py:_make_approval_gate`(task_id,auto_approve)捕获 handle;`api/tasks.py:_PendingApproval` Future 挂起;agent/loop 零改动(验证:git diff 无 agent/loop.py) |
| M3 | #2 Trace「store 加可选 ts」取舍 | ✅ `store/models.py:StoreRecord.ts`,缺省 None 兼容旧文件;`verify/trace.py` 离线重建 |
| M4 | #3 装配层 CLI 为主,Web 留 seam | ✅ `assembly/` 全落地 + `--config`;VerifiedLoop `extra_tools` 注入即 seam(文档标注) |
| M5 | #4 `parallel_safe` 独立于 read_only | ✅ `tools/base.py` 新字段;read_file/list_dir True;分区测试含 mixed `[read,write,read,read]@2` |
| M6 | #5 字符估算作用于**当前**列表 | ✅ `estimate_tokens` + `compress_to_budget` 双触发(文档 §取舍笔记3) |
| M7 | #6 空计划→只读 pass 安全默认 | ✅ `_run_readonly` + strict 空 plan deny;eval 注入非空 plan 守卫验证 |
| M8 | #7/#8 脚本 opt-in、文档清过期口径 | ✅ `WHYCODE_SMOKE_REAL=1` 守卫(未设 exit 0 实测);0.89/0.73 仅带日期存档 |

### (b) 实现中偏离/修偏记录(均闭环)

| # | 预期 | 实际偏差 | 处置 |
|---|------|---------|------|
| M9 | W4 分区 `[read,write,read,read]@2` → `[[read,read],[write],[read,read]]` | 算法实产 `[read],[write],[read,read]`(单读未达批量上限即在写屏障前 flush) | 按算法真实语义更新测试期望(文档记录,非 bug) |
| M10 | resume「复用 demo_repo 新拷」惯性 | 必须复用 `handle.repo`(会话记录在仓库内,新拷会丢 store) | `_run_task(resume=True)` 分支:handle.repo 存在即复用 |

## Standards 轴(编码规范)

### 修复过的问题(实现期自查/二轮 review 捕获)

| # | 气味 | 位置 | 修复 |
|---|------|------|------|
| S1 | **资源泄漏(async 关闭从未 await)** | `assembly/mcp.py` | AsyncExitStack 改 `push_async_callback(_make_close_guard)`;`__aenter__` 抛错手动 `aclose` 已连的;测试「all clients closed after exit」锁死 |
| S2 | **模块遮蔽(`import api.app as api_app` 绑到 FastAPI 实例)** | `api/__init__.py` + 测试 | `from api.app import app` 把父包同名属性设成实例遮蔽子模块 → 测试用 `importlib.import_module("api.app")` 拿真模块(注释说明) |
| S3 | **单事件循环契约不明** | `tests/api/test_approvals_api.py` | Future/后台协程须**同 loop** 才 resolve;TestClient portal 下审批流 vs resume 编排分别用 HTTP 轮询/async 直驱;文档注释明确 |
| S4 | 未用变量 / 行过长(E501) | W6 审计解析、W8 脚本 | ruff 修;脚本不属 tests 白名单,手动拆行 |
| S5 | 类型标注(Future、dict[str,Tool]、import-not-found) | W1/W3/W8 | mypy strict;playwright 标 `type: ignore[import-not-found]`(可选依赖) |

### 判断性观察(留档,未强制)

| # | 气味 | 说明 |
|---|------|------|
| b-1 | `TaskHandle.pending` 的 Future 无超时 | 浏览器不 resolve 则 gate await 悬挂;单用户 dev 工作台可接受(文档标注),生产需加超时兜底 |
| b-2 | SSE 无 `Last-Event-ID` | 迟到订阅靠 history 回放,进程内;跨重启不持久(M8 已知边界,未扩大) |
| b-3 | `scripts/smoke_*.py` 非标准测试 | opt-in 手动运行,不进 pytest 收集(命名/守卫双重排除) |

## 总结

- **Spec**:8/8 差距关闭 + 2 偏差闭环(M9 算法语义、M10 resume 复用 repo)。
- **Standards**:5 修复(S1 泄漏最重、S2 遮蔽最隐蔽)+ 3 判断性留档。
- 最重教训:async 生命周期(close/loop)在单测里最易「看着对、跑着漏」——S1 由 exit-path 测试暴露;
  S2 是 Python 包级 `app` 命名与子模块同名的真实陷阱。
