"""tui —— WhyCode 终端工作台(Textual)。

与 Web 工作台(`api/`)功能对等:任务列表 / 对话流 / Diff / 报告 / 交互审批 / 恢复 / 回滚。

**分层约定**:`gates.py` / `session.py` / `actions.py` **不导入 Textual** —— 编排逻辑
与 UI 框架解耦,因此可以用纯 asyncio 测试(毫秒级),不必启动 pilot。Textual 只出现在
`app.py` / `screens/` / `widgets/`。

**本模块刻意不做任何 re-export**(与 `api/__init__.py` 同一条纪律):导入 `tui` 本身
不应连带载入 Textual —— 见 `tests/test_import_boundaries.py`。
"""
