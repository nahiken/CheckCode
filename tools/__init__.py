# tools
"""正式工具集: read_file / write_file / list_dir / run_command(Pydantic schema)。

- base.py     Tool 抽象 + 参数 Pydantic 模型 + 输出截断
- files.py    文件工具
- commands.py run_command(子进程执行 + 超时)
- registry.py 注册表(工具分发 + tools_spec)
- runner.py   统一安全入口: schema 校验 → 权限 → Hook → 审批 → 执行(M2 集成)
"""
