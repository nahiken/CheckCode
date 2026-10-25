"""M15 接线守卫:API 路径必须把装配出的白名单交给 VerifiedLoop。

M13 的教训是「算法对了但接线点漏了」。API 路径(`api/app.py::_run_task`)与 CLI 的
`_run_loop` 是两个独立接线点,各有各的守卫:
- CLI 侧在 `tests/test_cli_allowlist.py`;
- API 侧在本文件。

**为什么需要它**:API 测试用的仓库里没有声明 `allowedTools` 的技能,装配出来本就是
`None` —— 于是「删掉那行赋值」在现有套件里是**静默无感**的(赋值是空操作)。
这里放一个**声明了 allowedTools 的项目技能**,让白名单非 `None`,并断言它真的到达 loop。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

# ⚠️ `api/__init__.py` 有 `from api.app import app` → 父包同名属性被设成 FastAPI 实例,
# 遮蔽子模块:`import api.app as api_app` 绑定到**实例**而非模块(M9 已记录的陷阱)。
# 必须用 importlib 拿真模块,否则 monkeypatch.setattr 会报 "FastAPI has no attribute"。
api_app = importlib.import_module("api.app")
from api.tasks import TaskManager  # noqa: E402  (须在 api_app 之后,避免包属性遮蔽)
from policy.models import Plan  # noqa: E402,F401  (类型引用,保持导入图显式)
from verify.report import RiskReport  # noqa: E402


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch) -> TaskManager:
    """隔离的 TaskManager(不污染模块级单例)。"""
    m = TaskManager()
    monkeypatch.setattr(api_app, "manager", m)
    return m


def _config() -> Any:
    from agent.config import LLMConfig

    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=3)


def _report(demo_repo: Path) -> RiskReport:
    return RiskReport(
        repo=demo_repo,
        base_commit="abc",
        branch="sess-fake",
        changed_files=[],
        test_command="python -m pytest tests/ -q",
        rounds=[],
        rounds_used=0,
        remaining_risk="低",
        diff="",
    )


def _write_project_skill(repo: Path, allowed: list[str]) -> None:
    p = repo / ".whycode" / "skills" / "editor" / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "name: editor", "description: 改代码", "allowedTools:"]
    lines += [f"  - {a}" for a in allowed]
    lines.append("---")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_api_threads_allowlist_to_verified_loop(
    demo_repo: Path, manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式 skills=[...] 选中声明型技能 → 白名单必须到达 VerifiedLoop。"""
    _write_project_skill(demo_repo, ["read_file"])
    seen: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            seen.update(kw)
            self.allowed_local_tools: set[str] | None = None

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            # run 时才读(生产也是构造后赋值、run 内才建 runner)
            seen["allowed_at_run"] = self.allowed_local_tools
            return _report(demo_repo)

    monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)

    manager.create("t-m15")
    req = api_app._TaskRequest(
        task=demo_repo.name, repo=str(demo_repo), auto_approve=True, skills=["editor"]
    )
    await api_app._run_task("t-m15", req, _config())

    assert seen["allowed_at_run"] == {"read_file"}


async def test_api_default_skills_does_not_narrow(
    demo_repo: Path, manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**默认路径不收窄**(审计 B1):仓库里有声明型技能,但请求没传 skills → None。"""
    _write_project_skill(demo_repo, ["read_file"])
    seen: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            self.allowed_local_tools: set[str] | None = None

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            seen["allowed_at_run"] = self.allowed_local_tools
            return _report(demo_repo)

    monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)

    manager.create("t-m15d")
    req = api_app._TaskRequest(task=demo_repo.name, repo=str(demo_repo), auto_approve=True)
    await api_app._run_task("t-m15d", req, _config())

    assert seen["allowed_at_run"] is None  # 不收窄 → 闭环不会瘫痪


async def test_api_surfaces_assembly_warnings(
    demo_repo: Path, manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装配告警必须外显(审计 B4):技能名拼错时前端要能看到 `assembly_warning`。

    早先 API 只读 `allowed_local_tools`、丢弃 `asm.errors` → 拼错技能名会**静默**
    退化成「不限制」,调用方以为自己收窄了。
    """

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            self.allowed_local_tools: set[str] | None = None

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            return _report(demo_repo)

    monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)

    manager.create("t-m15e")
    req = api_app._TaskRequest(
        task=demo_repo.name, repo=str(demo_repo), auto_approve=True, skills=["edtior"]
    )
    await api_app._run_task("t-m15e", req, _config())

    h = manager.get("t-m15e")
    assert h is not None
    events = [e for e in h.history if e.get("event") == "assembly_warning"]
    assert events, "装配告警未外显"
    assert any("edtior" in str(e.get("data", {}).get("message", "")) for e in events)


async def test_api_without_declaring_skill_leaves_unrestricted(
    demo_repo: Path, manager: TaskManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仓库里只有无声明的内置技能 → 白名单为 None(不限制,兼容纪律)。"""
    seen: dict[str, Any] = {}

    class _FakeLoop:
        def __init__(self, config: Any, **kw: Any) -> None:
            self.allowed_local_tools: set[str] | None = None

        async def run(self, repo: Path, task: str, events: Any = None) -> RiskReport:
            seen["allowed_at_run"] = self.allowed_local_tools
            return _report(demo_repo)

    monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)

    manager.create("t-m15b")
    req = api_app._TaskRequest(task=demo_repo.name, repo=str(demo_repo), auto_approve=True)
    await api_app._run_task("t-m15b", req, _config())

    assert seen["allowed_at_run"] is None


# 传递依赖链会拉入重模块(mcp SDK)的模块名 —— 它们在**函数体内**导入会阻塞事件循环。
# 实测:`assembly.build` 首导入 ≈3.4s(assembly.mcp → mcp_client.client → mcp)。
_HEAVY_IMPORTS = ("assembly", "mcp_client", "mcp")


def test_api_module_has_no_heavy_function_body_imports() -> None:
    """回归守卫(审计 M15 缺陷 A):`api/app.py` 不得在**函数体内**导入重模块。

    症状:`api/app.py` 曾把 `from assembly.build import build_assembly` 放进 `_run_task()`
    **函数体内**。该模块的传递依赖链是 `assembly.mcp → mcp_client.client → mcp` SDK,
    首次导入约 **3.4s**;而函数体内导入发生在 `asyncio.create_task` 之后 →
    **同步阻塞事件循环**,把每个 API 任务的首次工具调用推迟数秒
    (既有测试 `_wait_status(timeout=3.0)` 因此必然超时)。

    **为什么用 AST 而不是计时**:计时测试受调度与预热影响(单文件跑时依赖早被别的
    import 预热,3.4s 被摊掉)。这里要守的是**结构性事实** —— 重导入必须在模块级。
    AST 检查是确定性的:谁把 import 挪回函数体,立刻变红。

    **只查重模块**(`_HEAVY_IMPORTS`),不搞「函数体内一律不许 import」的一刀切:
    `fastapi.HTTPException` 之类廉价导入放函数体里是既有风格(毫秒级,无阻塞风险),
    把它们一起禁掉会把这条守卫变成需要不断加白名单的负担。
    """
    import ast
    import inspect

    # ① 模块级 import 必须已绑定(证明它在模块作用域,而不是函数体里)
    assert hasattr(api_app, "build_assembly"), "build_assembly 未在 api/app.py 模块级导入"
    assert hasattr(api_app, "prepare_repo"), "prepare_repo 未在 api/app.py 模块级导入"

    # ② 函数体内的 import 不得命中重模块
    tree = ast.parse(inspect.getsource(api_app))
    offenders: list[str] = []
    for fn in [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]:
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            else:
                continue
            for target in targets:
                if target.split(".")[0] in _HEAVY_IMPORTS:
                    offenders.append(f"{fn.name}() line {node.lineno}: {target}")
    assert not offenders, (
        "api/app.py 函数体内导入了重模块(会阻塞事件循环,见审计 M15 缺陷 A;"
        f"应移到模块级): {offenders}"
    )
