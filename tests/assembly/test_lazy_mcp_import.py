"""M15 审计修复的回归测试(第二轮 code review):装配层。

- C4:`assembly.build` 不得在模块级导入 `assembly.mcp`(其传递依赖是 mcp SDK,
  会让**每个**导入 assembly.build 的进程白付约 1s 启动成本,而 API 路径从不连 MCP)。
"""

from __future__ import annotations

import ast
import inspect

import assembly.build as build_mod


def test_mcp_import_is_lazy_not_module_level() -> None:
    """审计 C4:`assembly.mcp` 只能在**函数体内**(守卫分支)导入。

    `assembly.api import MCPAssembly` 会拉入 `mcp_client.client → mcp` SDK(≈1s),
    而 `MCPAssembly` 只在 `config.mcp_servers` 非空时用得到 —— API 路径
    (`build_assembly(repo, None, ...)`)永远用不到。模块级导入会让 `import api.app`
    无谓地多花约 1s 并载入上百个 mcp.* 模块(实测修前 1.40s → 修后 0.84s)。

    用 AST 而非计时:计时受机器负载影响,这里要守的是**结构性事实**。
    """
    tree = ast.parse(inspect.getsource(build_mod))
    # 模块顶层(body 直接子节点)不得出现 `from assembly.mcp import ...`
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "assembly.mcp":
            raise AssertionError("assembly.mcp 在模块级被导入(应放进 __aenter__ 的守卫分支)")
        if isinstance(node, ast.Import) and any(
            a.name.startswith("assembly.mcp") for a in node.names
        ):
            raise AssertionError("assembly.mcp 在模块级被导入")


def test_importing_build_does_not_load_mcp_sdk() -> None:
    """行为面:导入 `assembly.build` 本身**不**应把 mcp SDK 拉进 sys.modules。

    与上面的 AST 检查互补(AST 守「写的形状」,这条守「实际后果」)。
    """
    import subprocess
    import sys

    code = (
        "import assembly.build, sys; "
        "mods=[m for m in sys.modules if m=='mcp' or m.startswith('mcp.')]; "
        "print(len(mods))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "0", f"导入 assembly.build 载入了 {out.stdout.strip()} 个 mcp 模块"
