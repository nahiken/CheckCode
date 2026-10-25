# hooks
"""工具执行前后 Hook + 审计:参数 / 权限结论 / 耗时 / 结果,JSONL 落盘。

- base.py      ToolAuditEvent 审计事件模型(含 DecisionLayer 判定层)
- audit.py     AuditLog JSONL 追加日志 + 读回(before/after 两段式,默认注册表里的 Hook)
- registry.py  HookRegistry 按注册序分发 before/after;default_registry(audit) 复刻默认行为
"""
