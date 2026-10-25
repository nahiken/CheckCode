"""assembly/build.py —— 高层装配:`build_assembly(repo, config)` 一个上下文搞定全部。

把 config(whycode.toml)+ MCP 生命周期 + Skill 选择组合成一次装配:
    async with build_assembly(repo, config_path) as ctx:
        ctx.extra_tools      # {name: Tool}(MCP 工具,已绑 handler;可能空)
        ctx.skills_extra     # SkillSelection(系统提示追加段 + 本地工具白名单)
        ctx.errors           # 加载错误(不中断:工具/技能尽力装配)

错误语义:skill 缺失 / 依赖不满足 → errors 列表(不抛,任务仍可跑本地工具);
MCP 连接失败 → 抛 ConnectionError(装配中止,整体关闭已连的)。

M15:skill 的 `allowedTools` 里的裸本地工具名同时是**运行时白名单**来源,经
`ctx.skills.allowed_local_tools` 透传给 ToolRunner(`tools_allow`);
未选技能 → None → 不限制(默认路径零变化)。
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from assembly.config import AssemblyConfig, load_config
from assembly.skills import SkillSelection, assemble_skills
from tools.base import Tool
from tools.registry import build_tools

# ⚠️ `assembly.mcp` **不**在此处导入:它的传递依赖链是
# `assembly.mcp → mcp_client.client → mcp SDK`,首次导入约 1s(mcp 模块本身 ~1.0s)。
# 而 `MCPAssembly` 只在 `config.mcp_servers` 非空时才用得到 —— API 路径
# (`build_assembly(repo, None, ...)`)与未配 MCP 的 CLI 路径永远用不到。
# 放在函数体内导入会让「每个导入 assembly.build 的进程」都白付这笔启动开销
# (审计 M10–M15 C4:`import api.app` 曾因此 +1.1s)。因此延迟到 `__aenter__` 的
# 守卫分支里导入 —— 那里要么真配了 MCP(该付),要么根本不执行。
# 注:本模块有 `from __future__ import annotations`,故注解 `MCPAssembly | None`
# 不求值,声明期不需要该名字。类型检查走 TYPE_CHECKING 分支。
if TYPE_CHECKING:
    from assembly.mcp import MCPAssembly

# 用户级技能根:环境变量覆盖优先,否则 ~/.whycode(与 skills.loader 的
# <user_dir>/skills/<name>/SKILL.md 约定配套)。
USER_DIR_ENV = "WHYCODE_USER_DIR"


def default_user_dir() -> Path:
    """生产路径的用户级技能根。

    `WHYCODE_USER_DIR` 存在且非空 → 用它(测试/多用户隔离的显式旋钮);
    否则 → `~/.whycode`。

    ⚠️ 默认值**只在生产调用点**求值,不下沉到 `assemble_skills` 内部:loader 的
    `user_dir=None` 语义是「不加载用户级」,若在 loader 里补默认,开发机上真实存在
    的 `~/.whycode/skills` 会悄悄改变单元测试的装配结果(测试不再封闭)。故由
    CLI/Web 显式传入本函数的结果,测试继续传 `None`。
    """
    override = os.environ.get(USER_DIR_ENV, "").strip()
    return Path(override) if override else Path.home() / ".whycode"


class Assembly:
    """一次装配的结果。extra_tools 供 ToolRunner;skills_extra 供系统提示。"""

    def __init__(
        self,
        *,
        extra_tools: dict[str, Tool],
        skills: SkillSelection,
        mcp: MCPAssembly | None,
    ) -> None:
        self.extra_tools = extra_tools
        self.skills = skills
        self.mcp = mcp

    @property
    def errors(self) -> list[str]:
        return list(self.skills.errors)


class AssemblyContextManager:
    """async with 目标:进入连 MCP,退出全关。"""

    def __init__(
        self,
        repo: Path,
        config: AssemblyConfig,
        *,
        user_dir: Path | None = None,
        allow: list[str] | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self.user_dir = user_dir
        # M15:CLI --skills 覆盖 whycode.toml 的 [skills].allow。语义严格区分:
        # - None → 用 config 的 [skills].allow(通常为 []、「不筛选」)
        # - []   → 「不筛选」,与 None 在**未配 [skills].allow** 时同义
        # - 非空 → 只启用列表内技能(**并因此施加其 allowedTools 收窄**)
        self.allow = allow if allow is not None else config.skills_allow
        self._stack = AsyncExitStack()
        self._assembly: Assembly | None = None

    async def __aenter__(self) -> Assembly:
        # 1) Skill(离线,先做——不需要连接)。
        #    registered_tools:本地工具表(M15:allowedTools 允许写裸本地工具名,
        #    加载时对照此集合校验「这个名字存不存在」)。
        skills = assemble_skills(
            self.repo,
            allow=self.allow,
            user_dir=self.user_dir,
            registered_mcp={s.label for s in self.config.mcp_servers},
            registered_tools=set(build_tools(self.repo)),
            # 生产路径才提示「声明型技能未被显式选择」(见 assemble_skills 说明)
            warn_unselected_declared=True,
        )
        # 2) MCP:连 servers → 聚合工具。连接失败抛错(装配中止)。
        #    守卫内导入 `MCPAssembly` —— 见文件头说明(避免为不连 MCP 的路径白付 SDK 导入)。
        mcp: MCPAssembly | None = None
        extra_tools: dict[str, Tool] = {}
        health_warnings: list[str] = []
        if self.config.mcp_servers:
            from assembly.mcp import MCPAssembly

            mcp = await self._stack.enter_async_context(MCPAssembly(self.config.mcp_servers))
            # 健康探测:连上 ≠ 能用(对端可能握手后卡死)。不健康的 server **外显成
            # 告警**而不是静默 —— 工具仍在表里(调用时会自行抛错转错误 Observation),
            # 但用户先一步知道「这个 server 这一轮别指望」(C 包接线:health_check
            # 此前只在测试里被调用)。
            for label, ok in (await mcp.health_report()).items():
                if not ok:
                    health_warnings.append(
                        f"MCP server {label!r} 健康检查未通过(握手后 ping 失败/超时)"
                        f" —— 其工具本轮仍可见,但调用可能失败"
                    )
            extra_tools = dict(mcp.build_extra_tools())
        # MCP 工具并入白名单:技能声明了 `mcp:<server>` 依赖,即声明该 server 的工具
        # 在其作用域内(白名单是**工具名级**的,本地与 MCP 共用同一命名空间)。
        # None 保持 None —— 未声明 allowedTools 的技能不施加限制(兼容纪律)。
        # 用 `|` 而非 `|=`:不原地改 dataclass 字段(若将来复用/缓存 SkillSelection,
        # 原地并集会累积 —— 审计 M15 缺陷 F)。
        if skills.allowed_local_tools is not None:
            skills.allowed_local_tools = skills.allowed_local_tools | set(extra_tools)
        # MCP 健康告警并入 errors(单一外显通道:CLI 打印 / API 推 assembly_warning 事件)
        if health_warnings:
            skills.errors = list(skills.errors) + health_warnings
        self._assembly = Assembly(extra_tools=extra_tools, skills=skills, mcp=mcp)
        return self._assembly

    async def __aexit__(self, *exc: object) -> None:
        # AsyncExitStack 逆序关闭(MCP 先于 assembly 释放)
        await self._stack.aclose()
        self._assembly = None


def build_assembly(
    repo: Path,
    config_path: Path | None,
    *,
    user_dir: Path | None = None,
    allow: list[str] | None = None,
) -> AssemblyContextManager:
    """加载 whycode.toml 并返回装配上下文(async with)。

    config 缺失/为空 → 空装配(无 MCP、无技能,任务退化为纯本地工具——与现状等价)。
    allow:技能选择覆盖,三态:None → 用 config 的 [skills].allow;[] → **全部可见技能**;
    非空列表 → 只启用列表内的技能(注意 `[]` 与 `None` 在此**同义**,均为「不筛选」)。

    user_dir:用户级技能根。**生产调用点应传 `default_user_dir()`**;测试传 `None`
    以保持封闭(见 `default_user_dir` 的说明)。
    """
    config = load_config(config_path)
    return AssemblyContextManager(repo, config, user_dir=user_dir, allow=allow)
