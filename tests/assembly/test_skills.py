"""assembly/skills 单元测试:技能选择 + 系统提示注入(M9 W3)+ 工具白名单(M15)。"""

from __future__ import annotations

from pathlib import Path

from assembly.skills import assemble_skills, skill_tool_allowlist
from skills.loader import Skill, SkillKind

LOCAL_TOOLS = {"read_file", "write_file", "list_dir", "run_command"}


def _skill(name: str, allowed: list[str], *, kind: SkillKind = SkillKind.PROJECT) -> Skill:
    return Skill(
        name=name,
        description="d",
        allowed_tools=allowed,
        kind=kind,
        source=Path("x/SKILL.md"),
    )


def _write_project_skill(root: Path, name: str, allowed: list[str]) -> None:
    p = root / ".whycode" / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: d", "allowedTools:"]
    lines += [f"  - {a}" for a in allowed]
    lines.append("---")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_builtin_all_selected_when_no_allow(tmp_path: Path) -> None:
    """allow 空 → 全部内置可见技能(explain)。"""
    sel = assemble_skills(tmp_path, user_dir=None)
    assert sel.errors == []
    names = [s.name for s in sel.skills]
    assert "explain" in names
    # 系统提示追加段列出了技能
    assert "explain" in sel.system_prompt_extra


def test_allowlist_filters_to_named(tmp_path: Path) -> None:
    """allow 指定 → 只取该技能。"""
    sel = assemble_skills(tmp_path, allow=["explain"])
    assert [s.name for s in sel.skills] == ["explain"]


def test_allowlist_unknown_skill_reports_error(tmp_path: Path) -> None:
    """allow 指定不存在的技能 → 记入 errors(不静默忽略)。"""
    sel = assemble_skills(tmp_path, allow=["nonexistent"])
    assert sel.skills == []
    assert any("nonexistent" in e for e in sel.errors)


def test_project_skill_with_dependency_loaded(tmp_path: Path) -> None:
    """项目级技能(.whycode/skills/)被加载;allowedTools 前缀合法。"""
    # 构造一个项目级 skill,依赖满足(注册 mcp:fs)
    proj_skill = tmp_path / ".whycode" / "skills" / "audit" / "SKILL.md"
    proj_skill.parent.mkdir(parents=True)
    proj_skill.write_text(
        "---\n"
        "name: audit\n"
        "description: 审计依赖\n"
        "allowedTools:\n"
        "  - mcp:fs\n"
        "---\n"
        "用 filesystem 检查依赖。\n",
        encoding="utf-8",
    )
    sel = assemble_skills(tmp_path, registered_mcp={"fs"})
    names = {s.name for s in sel.skills}
    assert "audit" in names
    audit = next(s for s in sel.skills if s.name == "audit")
    assert audit.kind == SkillKind.PROJECT
    # 依赖不满足(未注册 fs)→ 加载报错,不出现
    sel2 = assemble_skills(tmp_path, registered_mcp=set())
    assert "audit" not in {s.name for s in sel2.skills}
    assert any("audit" in e for e in sel2.errors)


# === M15:本地工具白名单 ===


def test_allowlist_is_none_when_no_skill_declares(tmp_path: Path) -> None:
    """默认路径:只剩未声明 allowedTools 的内置技能 → 不限制(None),零变化。"""
    sel = assemble_skills(tmp_path, user_dir=None)
    assert [s.name for s in sel.skills] == ["explain"]
    assert sel.allowed_local_tools is None


def test_allowlist_is_union_of_declaring_skills(tmp_path: Path) -> None:
    """显式选中两个声明型技能 → 并集。"""
    _write_project_skill(tmp_path, "reader", ["read_file"])
    _write_project_skill(tmp_path, "writer", ["write_file", "read_file"])
    sel = assemble_skills(
        tmp_path,
        allow=["reader", "writer"],
        registered_mcp=set(),
        registered_tools=LOCAL_TOOLS,
    )
    assert sel.allowed_local_tools == {"read_file", "write_file"}


def test_default_path_does_not_narrow(tmp_path: Path) -> None:
    """⚠️ 承重:**默认路径(allow 为空)绝不施加收窄** —— 即便仓库里有声明型技能。

    审计 M10–M15 B1:早先「allow 为空 → 选中全部可见技能 → 取它们的并集」,
    结果是仓库里一个只声明 `mcp:fs` 的项目技能就会把默认运行的本地工具面**静默砍空**
    (read_file/write_file/run_command 全拒,无任何报错)。修法:收窄只跟随**显式选择**。
    """
    _write_project_skill(tmp_path, "reader", ["read_file"])  # 声明型技能存在于仓库
    sel = assemble_skills(tmp_path, registered_mcp=set(), registered_tools=LOCAL_TOOLS)
    assert "reader" in {s.name for s in sel.skills}  # 仍然可见(系统提示里会列出)
    assert sel.allowed_local_tools is None  # 但**不收窄**


def test_prefixed_only_skill_does_not_paralyze_default_path(tmp_path: Path) -> None:
    """审计 B1 的原始复现:只声明 `mcp:fs`(零本地工具)的技能不得砍空默认运行。"""
    _write_project_skill(tmp_path, "mfsonly", ["mcp:fs"])
    sel = assemble_skills(
        tmp_path, registered_mcp={"fs"}, registered_tools=LOCAL_TOOLS
    )
    assert sel.allowed_local_tools is None
    assert sel.errors == []  # 默认路径下连告警都不该有(它不是错误)


def test_explicit_prefixed_only_skill_warns_and_does_not_narrow(tmp_path: Path) -> None:
    """显式选中一个「声明了 allowedTools 但零本地工具」的技能 → 不砍空 + 报警。

    空集在旧实现里被当作「合法收窄」,结果是把闭环全部工具拒光。现在改为
    「不施加收窄 + 明确告警」——用户看得见,而不是静默瘫痪。
    """
    _write_project_skill(tmp_path, "mfsonly", ["mcp:fs"])
    sel = assemble_skills(
        tmp_path, registered_mcp={"fs"}, allow=["mfsonly"], registered_tools=LOCAL_TOOLS
    )
    assert sel.allowed_local_tools is None  # 不砍空
    assert any("mfsonly" in e and "本地工具" in e for e in sel.errors)


def test_undeclared_builtin_does_not_narrow(tmp_path: Path) -> None:
    """内置 explain(无声明)与声明型项目技能共存 → 白名单只由**显式选中**的声明者决定。"""
    _write_project_skill(tmp_path, "reader", ["read_file"])
    sel = assemble_skills(
        tmp_path, allow=["reader"], registered_mcp=set(), registered_tools=LOCAL_TOOLS
    )
    assert {s.name for s in sel.skills} == {"reader"}
    assert sel.allowed_local_tools == {"read_file"}


def _write_user_skill(user_dir: Path, name: str, allowed: list[str]) -> None:
    """用户级技能:<user_dir>/skills/<name>/SKILL.md(与 loader 的 project 结构对齐)。"""
    p = user_dir / "skills" / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", "description: d", "allowedTools:"]
    lines += [f"  - {a}" for a in allowed]
    lines.append("---")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_user_dir_loads_third_tier(tmp_path: Path) -> None:
    """用户级(第三级)技能在**传入 user_dir 后**真实加载。

    C 包接线前,user_dir 在生产调用点恒为 None —— 三级加载只有测试覆盖。
    这里锁住「传了就该加载」,使生产接线有回归防线。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    user = tmp_path / "home" / ".whycode"
    _write_user_skill(user, "helper", ["read_file"])

    sel = assemble_skills(
        repo,
        user_dir=user,
        registered_mcp=set(),
        registered_tools=LOCAL_TOOLS,
    )
    assert "helper" in {s.name for s in sel.skills}


def test_user_dir_none_does_not_load_users_home(tmp_path: Path) -> None:
    """user_dir=None → **不加载**用户级(库层契约)。

    这是测试封闭性的承重墙:若把默认值下沉进 loader,开发机上真实存在的
    ~/.whycode/skills 会悄悄改变单元测试结果。故默认只在生产调用点求值。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    user = tmp_path / "home" / ".whycode"
    _write_user_skill(user, "helper", ["read_file"])

    sel = assemble_skills(
        repo, user_dir=None, registered_mcp=set(), registered_tools=LOCAL_TOOLS
    )
    assert "helper" not in {s.name for s in sel.skills}


def test_unselected_declared_skill_warns_when_asked(tmp_path: Path) -> None:
    """生产开关打开时:可见集合里有声明型技能但未显式选择 → 告警(不静默)。

    用户级技能接入后这类落差才可能出现(用户装了个技能,以为它在限制工具)。
    显式选择路径已有收窄,故只测「未显式选择」这一支。
    """
    _write_project_skill(tmp_path, "reader", ["read_file"])
    sel = assemble_skills(
        tmp_path,
        registered_mcp=set(),
        registered_tools=LOCAL_TOOLS,
        warn_unselected_declared=True,
    )
    assert sel.allowed_local_tools is None  # 行为不变:仍然不收窄
    assert any("reader" in e and "不施加" in e for e in sel.errors)  # 但有话直说


def test_unselected_declared_skill_silent_by_default(tmp_path: Path) -> None:
    """库层默认(不传开关)→ **零告警**:默认路径的静默是既有契约。

    与 `test_default_path_does_not_narrow` 同源:生产告警不该污染库层单测。
    """
    _write_project_skill(tmp_path, "reader", ["read_file"])
    sel = assemble_skills(tmp_path, registered_mcp=set(), registered_tools=LOCAL_TOOLS)
    assert sel.errors == []


def test_skill_tool_allowlist_pure_function() -> None:
    """`skill_tool_allowlist` 返回 (白名单, 警告):三态语义的单一来源。"""
    assert skill_tool_allowlist([]) == (None, [])
    assert skill_tool_allowlist([_skill("a", [])]) == (None, [])
    # 只声明前缀依赖 → None + 告警(不再返回空集收窄)
    allow, warns = skill_tool_allowlist([_skill("a", ["mcp:x"])])
    assert allow is None and len(warns) == 1
    # 有本地工具 → 正常并集,无告警
    assert skill_tool_allowlist([_skill("a", ["read_file"]), _skill("b", ["mcp:x"])]) == (
        {"read_file"},
        [],
    )
