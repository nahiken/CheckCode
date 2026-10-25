"""workbench —— 与传输层无关的工作台共享逻辑。

**为什么需要这个包**:同一套任务编排语义要被**两个**前端复用 —— Web
(`api/app.py`,FastAPI + SSE)与终端(`tui/`,Textual)。把这些逻辑留在 `api/app.py`
里会造成两种后果之一:要么终端被迫依赖 FastAPI(它不需要 HTTP),要么两边各抄一份
(必然随时间漂移 —— 而这里的 `rollback_guard` 承载的是安全守卫,抄漏一条就是漏洞)。

**本包不导入 FastAPI,也不导入 Textual。** 它只依赖领域层(`verify` / `worktree` /
`policy`)。`api/` 与 `tui/` 各自在此之上叠加自己的传输/交互层。
"""
