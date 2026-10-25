"""skills.loader —— Skill 三级加载(内置/项目级/用户级)+ 依赖预检。

安全语义(要点):
- **内置级(builtin)**:随 WhyCode 分发,可信 → 允许无 allowedTools。
- **项目级(project)/用户级(user)**:Fork Skill 必须显式声明 allowedTools;
  缺失 → **拒绝加载**(报错含 skill 名,绝不静默放行)。
- **依赖预检**:allowedTools 每项必须是以下三类之一 ——
  `mcp:<server>`(已注册的 MCP server)/ `skill:<name>`(已加载的技能)/
  **裸本地工具名**(M15:对照传入的 `registered_tools` 校验)。
  加载后由 resolve_dependencies 对照已注册工具/技能集合校验,缺失项上报。
- **M15 运行时收窄**:裸本地工具名同时是「本 skill 可用哪些本地工具」的**白名单**
  来源,经 `SkillSelection.allowed_local_tools` 透传进 ToolRunner 的 `tools_allow`
  层(见 policy/evaluator.py)。这是**工具级白名单**,不是 OS 沙箱。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from skills.frontmatter import parse_frontmatter

# 每级 SKILL.md 的目录名
SKILLS_DIRNAME = "skills"
# 合法的依赖前缀(其余视为「裸本地工具名」,需对照注册工具集校验)
VALID_PREFIXES = ("mcp:", "skill:")
# 报错消息里最多列出的工具名个数(工具集可能很大,异常消息不能无上界)
_SUMMARY_LIMIT = 10


def _is_local_tool(dep: str) -> bool:
    """裸本地工具名(无前缀)判定 —— M15:allowedTools 可列 read_file 等本地工具。"""
    return not dep.startswith(VALID_PREFIXES)


def _summarize_tools(registered_tools: set[str] | None) -> str:
    """列出可用工具名(供报错),**有上界** —— 工具集可能很大,不能把整张表塞进异常。

    前 _SUMMARY_LIMIT 个 + 总数;超出部分折叠(报错只需要「看一眼有哪些」)。
    """
    if not registered_tools:
        return "(无)"
    names = sorted(registered_tools)
    if len(names) <= _SUMMARY_LIMIT:
        return ", ".join(names)
    head = ", ".join(names[:_SUMMARY_LIMIT])
    return f"{head}, …(共 {len(names)} 个)"


def _reject_unknown_dep(
    dep: str,
    *,
    skill_name: str,
    registered_tools: set[str] | None,
) -> None:
    """非法依赖项 → ValueError。

    - 带合法前缀(mcp:/skill:):加载期只放行,存在性由 resolve_dependencies 预检。
    - 裸本地工具名:必须对照 `registered_tools` 校验;未传集合(None)则一律视为非法
      —— 保住 M5 错误消息与既有断言的**向后兼容纪律**。
    """
    if not _is_local_tool(dep):
        return
    if registered_tools is not None and dep in registered_tools:
        return
    raise ValueError(
        f"Skill {skill_name} 依赖 {dep!r} 非法:必须以 mcp:/skill: 前缀声明,"
        f"或为已注册的本地工具名(当前可用: {_summarize_tools(registered_tools)})"
    )


class SkillKind(enum.StrEnum):
    BUILTIN = "builtin"
    PROJECT = "project"
    USER = "user"


@dataclass
class Skill:
    name: str
    description: str
    allowed_tools: list[str]
    kind: SkillKind
    source: Path
    body: str = ""

    @property
    def allowed_local_tools(self) -> set[str]:
        """本 skill 允许的**本地工具名**集合(M15:白名单来源)。

        只挑裸名项;`mcp:`/`skill:` 是依赖声明,不是本地工具。
        """
        return {dep for dep in self.allowed_tools if _is_local_tool(dep)}


def load_skill(
    path: Path,
    *,
    kind: SkillKind,
    registered_tools: set[str] | None = None,
) -> Skill:
    """加载单个 SKILL.md。project/user 级缺 allowedTools → ValueError。

    registered_tools:已注册的本地工具名集合(M15);给了才允许裸本地工具名,
    不给则裸名一律非法(与 M5 行为逐字一致)。
    """
    meta = parse_frontmatter(path.read_text(encoding="utf-8"))
    if kind in (SkillKind.PROJECT, SkillKind.USER) and not meta.allowed_tools:
        raise ValueError(
            f"Fork Skill 必须声明 allowedTools({meta.name}, {path})——"
            f"这是安全边界:未声明依赖的技能禁止加载"
        )
    for dep in meta.allowed_tools:
        _reject_unknown_dep(
            dep, skill_name=meta.name, registered_tools=registered_tools
        )
    return Skill(
        name=meta.name,
        description=meta.description,
        allowed_tools=meta.allowed_tools,
        kind=kind,
        source=path,
        body=meta.body,
    )


def _scan_dir(skills_root: Path, kind: SkillKind) -> list[Path]:
    """扫描一个 skill 根目录下的技能:每个技能一个子目录 <name>/SKILL.md。

    - builtin 级根直接是 skills/builtin(技能在 builtin/<name>/SKILL.md)。
    - project/user 级根是 <repo>/.whycode 或 <user>/.whycode,技能在
      root/skills/<name>/SKILL.md(与内置结构对齐)。
    目录不存在 → 空。子目录隔离多技能(避免同目录覆盖)。
    """
    base = skills_root if kind == SkillKind.BUILTIN else skills_root / SKILLS_DIRNAME
    if not base.exists():
        return []
    return sorted(base.glob("*/SKILL.md"))


class SkillRegistry:
    """聚合内置/项目/用户三级的可见技能。靠后的级别覆盖同名。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._errors: list[str] = []

    def load_all(
        self,
        *,
        repo: Path,
        builtin_dir: Path | None = None,
        user_dir: Path | None = None,
        registered_mcp: set[str] | None = None,
        registered_tools: set[str] | None = None,
    ) -> list[str]:
        """按 builtin → project → user 顺序加载,返回加载错误列表。

        project 级 = repo/.whycode/skills/;user 级 = ~/.whycode/skills/。
        registered_mcp:已注册的 MCP server 名集合;缺失依赖(mcp:/skill:)的
        project/user 级技能**拒绝加载**(验收点「技能缺失依赖被拒」)。
        registered_tools:已注册的本地工具名集合(M15):只有集合内的裸工具名
        才是合法依赖;None → 裸名非法(与 M5 一致)。
        """
        self._errors = []
        mcp_servers = registered_mcp or set()
        order: list[tuple[Path | None, SkillKind]] = [
            (builtin_dir, SkillKind.BUILTIN),
            (repo / ".whycode", SkillKind.PROJECT),
            (user_dir, SkillKind.USER),
        ]
        for root, kind in order:
            if root is None:
                continue
            for path in _scan_dir(root, kind):
                try:
                    skill = load_skill(
                        path, kind=kind, registered_tools=registered_tools
                    )
                    # 依赖预检:依赖缺失 → 拒绝加载
                    missing = self.resolve_dependencies(
                        skill.allowed_tools,
                        registered_mcp=mcp_servers,
                        registered_skills=set(self._skills),
                        registered_tools=registered_tools,
                    )
                    if missing:
                        raise ValueError(
                            f"Skill {skill.name} 依赖不满足,拒绝加载: {missing}"
                        )
                    # 靠后级别覆盖同名(先加载先入,后加载覆盖)
                    self._skills[skill.name] = skill
                except (ValueError, OSError) as exc:
                    self._errors.append(f"{kind.value}:{path}: {exc}")
        return list(self._errors)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    @property
    def skills(self) -> dict[str, Skill]:
        return dict(self._skills)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def resolve_dependencies(
        self,
        allowed_tools: list[str],
        *,
        registered_mcp: set[str],
        registered_skills: set[str],
        registered_tools: set[str] | None = None,
    ) -> list[str]:
        """预检依赖:返回缺失项列表(空 = 全部满足)。

        mcp:<server> → 需已注册的 MCP server 名;skill:<name> → 需已加载的 Skill 名;
        裸本地工具名 → 需在 registered_tools 内(不传 → 视为缺失,与 M5 一致)。
        """
        missing: list[str] = []
        for dep in allowed_tools:
            if dep.startswith("mcp:"):
                name = dep[len("mcp:") :]
                if name not in registered_mcp:
                    missing.append(dep)
            elif dep.startswith("skill:"):
                name = dep[len("skill:") :]
                if name not in registered_skills:
                    missing.append(dep)
            elif registered_tools is None or dep not in registered_tools:
                missing.append(dep)  # 裸本地工具名:未注册(或未提供集合)
        return missing
