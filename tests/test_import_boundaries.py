"""导入边界守卫:`api/__init__.py` 不得有任何副作用。

背景(M16 之后的拆包):此前 `api/__init__.py` 有 `from api.app import app`,导致
两个问题 ——

1. **包属性遮蔽**(M9 review S2):父包的 `app` 属性被设成 FastAPI **实例**,
   遮蔽同名子模块,`import api.app as api_app` 绑到实例而非模块,monkeypatch 打不中。
2. **无辜的传递依赖**:任何 `from api.tasks import TaskManager` 都会先执行包
   `__init__` → 拉起 `api.app` → `import fastapi`。终端工作台(`tui/`)只需要任务
   注册表,不该被迫依赖 Web 框架。

修法是把 `api/__init__.py` 清成纯 docstring。本文件用**结构性事实**(不是计时)
把两条都钉住 —— 计时受机器负载影响,这里要守的是「导入图长什么样」。
"""

from __future__ import annotations

import subprocess
import sys
import types


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """在**独立解释器**里跑一段导入代码(进程隔离,不受本进程已导入模块干扰)。"""
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )


def test_api_package_import_does_not_pull_fastapi() -> None:
    """`import api` 本身不得载入 fastapi —— 包 `__init__` 必须是纯声明。"""
    r = _run(
        "import sys, api; "
        "leaked=[m for m in sys.modules if m=='fastapi' or m.startswith('fastapi.')]; "
        "print(len(leaked))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0", f"import api 载入了 {r.stdout.strip()} 个 fastapi 模块"


def test_api_tasks_import_does_not_pull_fastapi() -> None:
    """行为面:终端工作台只需 `api.tasks`,不该为它拉进整个 FastAPI。

    这是拆包的核心收益 —— `TaskManager` 本身零 Web 依赖(只依赖 asyncio 与
    `agent.loop` / `policy.models`),故它必须能被无 Web 框架的进程导入。
    """
    r = _run(
        "import sys, api.tasks; "
        "leaked=[m for m in sys.modules if m=='fastapi' or m.startswith('fastapi.')]; "
        "print(len(leaked))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0", f"import api.tasks 载入了 {r.stdout.strip()} 个 fastapi 模块"


def test_api_app_binds_to_module_not_instance() -> None:
    """⚠️ 承重:`import api.app` 必须绑到**模块**,不是 FastAPI 实例。

    这是 M9 review S2 的直接回归守卫。此前包 `__init__` 的 `from api.app import app`
    会把父包属性设成实例,使 `import api.app as api_app` 拿到实例 —— 于是
    `monkeypatch.setattr(api_app, ...)` 全部打不中真实模块(缺陷表现为「测试改了但
    没生效」,极难排查)。
    """
    import api.app  # noqa: F401  (导入即被测;名字不用)

    assert isinstance(api.app, types.ModuleType), (
        "api.app 绑到了非模块对象 —— 包属性遮蔽回归了(检查 api/__init__.py 是否又加了 re-export)"
    )
    # 模块上应当有 ASGI 应用(Dockerfile 的 `api.app:app` 依赖它)
    assert hasattr(api.app, "app")


def test_workbench_is_transport_agnostic() -> None:
    """`workbench` 包不得依赖 Web 或终端框架 —— 它是两端共享的中间层。"""
    r = _run(
        "import sys, workbench.report, workbench.rollback, workbench.session_id; "
        "bad=[m for m in sys.modules "
        "     if m.split('.')[0] in ('fastapi', 'starlette', 'textual', 'rich')]; "
        "print(len(bad))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0", f"workbench 载入了 Web/终端框架: {r.stdout.strip()} 个"


def test_tui_package_import_does_not_pull_textual() -> None:
    """`import tui` 本身不得载入 Textual —— 与 `api/__init__.py` 同一条纪律。

    Textual 首次导入约 0.5–1s;CLI/API/Eval 路径不该为它买单(pyproject 把它放在
    `tui` optional extra 而非硬依赖,正是为此)。若哪个模块在**模块级**导入了
    Textual,这条会红 —— 那时应把导入移进函数体或守卫分支。

    **只断言 `textual`,不断言 `rich`**:rich 是 httpx 的既有传递依赖
    (`httpx/__init__.py` 无条件导入 `httpx._main`,后者导入 rich 做 CLI 进度条;
    而 httpx 是**核心**依赖、与 TUI 无关)。故「rich 已载入」不构成违规信号。
    """
    r = _run(
        "import sys, tui; "
        "bad=[m for m in sys.modules if m=='textual' or m.startswith('textual.')]; "
        "print(len(bad))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0", f"import tui 载入了 {r.stdout.strip()} 个 textual 模块"


def test_tui_kernel_does_not_import_textual() -> None:
    """`tui` 的**内核**模块(gates/session/actions)不得导入 Textual。

    这是分层的可测性保证:内核零框架依赖,所以能用纯 asyncio 测试(毫秒级),
    不必启动 pilot。若哪天有人在内核里 import textual,这条会红。
    """
    r = _run(
        "import sys, tui.gates, tui.session, tui.actions; "
        "bad=[m for m in sys.modules if m=='textual' or m.startswith('textual.')]; "
        "print(len(bad))"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "0", f"tui 内核载入了 {r.stdout.strip()} 个 textual 模块"
