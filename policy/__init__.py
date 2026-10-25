# policy
"""分层权限:路径沙箱 · 高危命令黑名单 · 规则 · 计划白名单 · 运行模式兜底。

- models.py     数据模型(Plan / Rule / PermissionDecision / RunMode)
- sandbox.py    项目路径沙箱(拒绝 ../ 与绝对路径逃逸)
- rules.py      高危命令黑名单(最后防线,要点:黑名单不可靠)
- evaluator.py  分层判定链
"""
