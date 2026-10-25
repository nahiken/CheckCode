"""assembly/skills.py —— Skill 装配:三级加载 + allowlist 选择 + 系统提示注入。

Skills 本体由 skills/loader.py 加载(内置/项目/用户三级 + 依赖预检)。装配层:
1. 按 allowlist(whycode.toml [skills].allow)选技能;allowlist 为空 → 全部可见技能。
2. 把所选技能拼成一段「可用技能说明」追加到 Agent 系统提示(让模型知道能请求什么)。
3. 计算已加载 MCP server 名集合 → 供 SkillRegistry 依赖预检(mcp:<server> 匹配)。

本模块纯同步/离线(只读 SKILL.md),可被 CLI/Web 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from skills.loader import Skill, SkillRegistry

# 内置 skill 根:包内 skills/builtin(随 WhyCode 分发)
_BUILTIN_DIR = Path(__file__).resolve().parents[1] / "skills" / "builtin"


@dataclass
class SkillSelection:
    """装配结果:选中的技能 + 系统提示追加段 + 本地工具白名单 + 加载错误。

    allowed_local_tools(M15,由 `skill_tool_allowlist` 计算):
    - **None** = **本次不施加工具收窄** —— 未选中任何技能,或所选技能都没声明
      `allowedTools`(默认路径 + 内置 `explain`),或声明了但没列出任何本地工具;
    - **set**  = 收窄到这些工具(所选声明型技能声明本地工具的并集)。

    注意「空集 ≠ None」的历史语义在 M15 修订后**已收紧**:声明了 `allowedTools`
    却一个本地工具都没列(只依赖 MCP)属于**可疑声明**,不再静默把工具面砍空,
    而是**不施加收窄**并在 errors 里报警(见 `skill_tool_allowlist`)。
    """

    skills: list[Skill] = field(default_factory=list)
    system_prompt_extra: str = ""
    # 装配告警/错误(**可追加**):装配层会把 MCP 健康探测等非 skills 来源的告警
    # 并入这里,复用同一条外显通道(CLI 打印 / API 推 assembly_warning 事件)。
    # 故此字段不是「skills 专属」—— 它是「本次装配的告警清单」。
    errors: list[str] = field(default_factory=list)
    allowed_local_tools: set[str] | None = None


def skill_tool_allowlist(selected: list[Skill]) -> tuple[set[str] | None, list[str]]:
    """从所选技能计算本地工具白名单。返回 `(白名单, 警告列表)`。

    返回 `None` = **不施加收窄**。以下三种情况都返回 None:

    1. 没选中任何技能;
    2. 选中的技能**都没声明** `allowedTools`(内置技能随 WhyCode 分发、可信);
    3. 声明了 `allowedTools` 但**一个本地工具都没列出**(纯 mcp:/skill: 依赖)。

    第 3 条是 M15 修订的关键:早先把它当成「合法的空集收窄」,结果是**默认路径
    静默瘫痪** —— 仓库里只要有一个只声明 `mcp:fs` 或 `skill:x` 的项目技能,默认
    运行(allow 为空 → 选中全部可见技能)就会把 `read_file`/`write_file`/
    `run_command` 全部拒光,闭环读不了写不了跑不了测试,**且没有任何报错**。

    调用方(`assemble_skills`)只在**显式选择技能**时才施加收窄 —— 收窄是显式
    选择的**结果**,不是默认路径的副产品。**并集语义**:多个声明型技能同时选中时
    取并集(规格要求),因此 A 声明的工具对 B 也可用(已知取舍,见 docs/15-m15.md)。
    """
    declared = [s for s in selected if s.allowed_tools]
    if not declared:
        return None, []
    local: set[str] = set()
    for s in declared:
        local |= s.allowed_local_tools
    if not local:
        names = ", ".join(s.name for s in declared)
        return None, [
            f"技能 {names} 声明了 allowedTools 但未列出任何本地工具"
            f"(仅 mcp:/skill: 依赖)—— 不施加工具收窄, 以避免把闭环的本地工具面"
            f"静默砍空。若确实要限制本地工具, 请在 allowedTools 里显式列出工具名"
            f"(如 read_file)"
        ]
    return local, []



def assemble_skills(
    repo: Path,
    *,
    allow: list[str] | None = None,
    user_dir: Path | None = None,
    registered_mcp: set[str] | None = None,
    registered_tools: set[str] | None = None,
    warn_unselected_declared: bool = False,
) -> SkillSelection:
    """加载三级技能 → 按 allow 白名单选择 → 生成系统提示追加段 + 本地工具白名单。

    allow 为空 → 用全部可见技能(builtin 无 allowedTools 限制,天然可全量);
    显式 allow 但请求项不存在 → 记入 errors(不静默忽略)。

    **工具收窄只在显式 allow 时生效**(M15):allow 为空时 `allowed_local_tools`
    恒为 None(不限制),即便选中的技能声明了 `allowedTools` —— 否则仓库里一个
    纯 mcp:/skill: 依赖的项目技能就会把默认运行的本地工具面静默砍空。

    registered_tools:已注册的本地工具名集合(M15),用于校验 allowedTools 里的
    裸工具名;None → 只接受 mcp:/skill: 形式(与 M5 一致)。

    warn_unselected_declared:**生产路径**(接入了用户级技能之后)置 True —— 当可见
    集合里存在声明型技能、而本次又没做显式选择时,提示「你的 allowedTools 没生效」。
    这类静默落差在用户级技能默认可见后才可能出现(用户装了个技能,以为它在限制工具,
    实际没有)。库层默认 False:默认路径的「零告警」是既有契约(见
    `test_default_path_does_not_narrow`),单元测试不该被生产告警污染。
    """
    registry = SkillRegistry()
    errors = list(
        registry.load_all(
            repo=repo,
            builtin_dir=_BUILTIN_DIR,
            user_dir=user_dir,
            registered_mcp=registered_mcp or set(),
            registered_tools=registered_tools,
        )
    )
    visible = registry.skills
    allow_set = set(allow or [])

    selected: list[Skill] = []
    if allow_set:
        # 白名单模式:只取 allow 命中的技能;缺项报错(allow 非 None 由 allow_set 保证)
        allow_list = allow or []
        for name in allow_list:
            skill = visible.get(name)
            if skill is None:
                errors.append(f"skill:{name} 未找到(allowlist 指定但不存在/依赖不满足)")
            else:
                selected.append(skill)
    else:
        selected = list(visible.values())

    # 追加段:列出可用技能,让模型知道可请求注入
    extra = ""
    if selected:
        lines = ["", "可用的技能(如需使用请说明,装配层会注入对应提示/工具):"]
        for s in selected:
            lines.append(f"- {s.name}: {s.description}")
        extra = "\n".join(lines)

    # M15:本地工具白名单。**只有显式选择技能**(allow 非空)才施加收窄 ——
    # 收窄是显式选择的结果,不是默认路径的副产品(否则仓库里一个只声明 mcp: 依赖的
    # 技能就会把默认运行的本地工具面静默砍空,见 `skill_tool_allowlist`)。
    #
    # 三级加载接入后(用户级技能默认可见),默认路径多了一类来源:用户主目录里的
    # 技能也进了 `selected`。生产路径置 warn_unselected_declared → **记录**「可见
    # 集合里有声明型技能、但本次不施加收窄」这一事实 —— 用户以为自己的技能生效了,
    # 实际工具面没动,静默的落差比报错更难查。库层默认关(默认路径零告警是既有契约)。
    allowlist: set[str] | None = None
    warnings: list[str] = []
    if allow_set:
        allowlist, warnings = skill_tool_allowlist(selected)
    elif warn_unselected_declared and any(s.allowed_tools for s in selected):
        declared = ", ".join(s.name for s in selected if s.allowed_tools)
        warnings.append(
            f"技能 {declared} 声明了 allowedTools,但本次未显式选择技能"
            f"(--skills / [skills].allow),故**不施加**工具收窄。"
            f"若要其 allowedTools 生效,请显式选择该技能。"
        )
    return SkillSelection(
        skills=selected,
        system_prompt_extra=extra,
        errors=errors + warnings,
        allowed_local_tools=allowlist,
    )
