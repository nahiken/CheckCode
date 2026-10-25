"""M5 单元测试:Skills 三级加载 + Fork Skill 强制 allowedTools + 依赖预检。"""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.loader import SkillKind, SkillRegistry, load_skill


def _write_skill(
    root: Path,
    rel: str,
    name: str,
    *,
    description: str = "说明",
    allowed: list[str] | None = None,
) -> Path:
    """写一个 SKILL.md。allowed=None → 不写 allowedTools 行(模拟 Fork Skill 缺失)。"""
    lines = ["---", f"name: {name}", f"description: {description}"]
    if allowed is not None:
        lines.append("allowedTools:")
        for a in allowed:
            lines.append(f"  - {a}")
    lines.append("---")
    lines.append("正文")
    p = root / rel
    # 每个技能一个子目录 skills/<name>/SKILL.md(规范:子目录隔离多技能)
    p = p.parent / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestLoadSkill:
    def test_loads_builtin_skill(self, tmp_path: Path) -> None:
        """内置级:允许无 allowedTools(内置可信)。"""
        p = _write_skill(tmp_path, "skills/search.md", "search", allowed=["mcp:filesystem"])
        skill = load_skill(p, kind=SkillKind.BUILTIN)
        assert skill.name == "search"
        assert skill.kind == "builtin"
        assert skill.allowed_tools == ["mcp:filesystem"]

    def test_project_fork_missing_allowed_rejected(self, tmp_path: Path) -> None:
        """项目级 Fork Skill 缺 allowedTools → 拒绝。"""
        p = _write_skill(tmp_path, "skills/forked.md", "forked")
        with pytest.raises(ValueError, match="allowedTools"):
            load_skill(p, kind=SkillKind.PROJECT)

    def test_user_fork_missing_allowed_rejected(self, tmp_path: Path) -> None:
        """用户级 Fork Skill 缺 allowedTools → 拒绝。"""
        p = _write_skill(tmp_path, "skills/forked.md", "forked")
        with pytest.raises(ValueError, match="allowedTools"):
            load_skill(p, kind=SkillKind.USER)

    def test_invalid_dependency_rejected(self, tmp_path: Path) -> None:
        """allowedTools 含非法前缀(非 mcp:/skill:)→ 拒绝。"""
        p = _write_skill(
            tmp_path, "skills/bad.md", "bad",
            allowed=["python:foo", "tool:bar"],
        )
        with pytest.raises(ValueError, match="mcp:"):
            load_skill(p, kind=SkillKind.USER)


class TestSkillRegistry:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """builtin / project / user 三个根目录。

        builtin/user 直接是 skills 根;project 是 repo(技能在 repo/.whycode/skills/)。
        """
        builtin = tmp_path / "builtin"
        project = tmp_path / "repo"
        user = tmp_path / "user"
        for d in (builtin, user):
            d.mkdir()
        project.mkdir()
        return builtin, project, user

    def test_three_levels_merged_user_overrides(self, tmp_path: Path) -> None:
        """三级合并;user 覆盖同名 project。"""
        builtin, project, user = self._setup(tmp_path)
        # builtin 级:builtin/<name>/SKILL.md(无 skills/ 子层)
        _write_skill(builtin, "greet.md", "greet", allowed=["mcp:a"])
        _write_skill(project, ".whycode/skills/tweak.md", "tweak", allowed=["mcp:b"])
        # user 同名覆盖 project
        _write_skill(project, ".whycode/skills/theme.md", "theme", allowed=["mcp:p"])
        _write_skill(user, "skills/theme.md", "theme", allowed=["mcp:u"])

        registry = SkillRegistry()
        errors = registry.load_all(
            repo=project,
            builtin_dir=builtin,
            user_dir=user,
            registered_mcp={"a", "b", "p", "u"},
        )
        assert errors == []  # 全部加载成功
        greet = registry.get("greet")
        assert greet is not None and greet.kind == SkillKind.BUILTIN
        tweak = registry.get("tweak")
        assert tweak is not None and tweak.kind == SkillKind.PROJECT
        theme = registry.get("theme")
        assert theme is not None and theme.kind == SkillKind.USER
        assert theme.allowed_tools == ["mcp:u"]  # user 覆盖

    def test_fork_without_allowed_rejected_and_recorded(self, tmp_path: Path) -> None:
        """project/user 的 Fork Skill 缺 allowedTools → 拒绝并记录 error。"""
        builtin, project, user = self._setup(tmp_path)
        _write_skill(project, ".whycode/skills/nobad.md", "nobad")  # 缺 allowedTools
        _write_skill(user, "skills/nobad2.md", "nobad2")  # 缺 allowedTools
        registry = SkillRegistry()
        errors = registry.load_all(repo=project, builtin_dir=builtin, user_dir=user)
        assert len(errors) == 2
        assert any("nobad" in e for e in errors)
        assert registry.get("nobad") is None  # 未加载

    def test_missing_dependency_gates_loading(self, tmp_path: Path) -> None:
        """依赖预检:声明的 mcp server 未注册 → 技能拒绝加载(验收点)。"""
        builtin, project, user = self._setup(tmp_path)
        _write_skill(project, ".whycode/skills/needsfs.md", "needsfs", allowed=["mcp:filesystem"])
        registry = SkillRegistry()
        errors = registry.load_all(
            repo=project, builtin_dir=builtin, user_dir=user,
            registered_mcp={"math"},  # filesystem 未注册
        )
        assert len(errors) == 1
        assert "mcp:filesystem" in errors[0]
        assert registry.get("needsfs") is None  # 未加载

    def test_missing_dependency_satisfied_gates_none(self, tmp_path: Path) -> None:
        """注册满足依赖 → 技能加载成功。"""
        builtin, project, user = self._setup(tmp_path)
        _write_skill(project, ".whycode/skills/needsfs.md", "needsfs", allowed=["mcp:filesystem"])
        registry = SkillRegistry()
        errors = registry.load_all(
            repo=project, builtin_dir=builtin, user_dir=user,
            registered_mcp={"filesystem"},
        )
        assert errors == []
        assert registry.get("needsfs") is not None

    def test_builtin_level_available(self, tmp_path: Path) -> None:
        """内置级 Skills 可加载(builtin 目录存在且有技能)。"""
        builtin = Path(__file__).resolve().parents[2] / "skills" / "builtin"
        project = tmp_path / "repo"
        project.mkdir()
        user = tmp_path / "user"
        user.mkdir()
        registry = SkillRegistry()
        errors = registry.load_all(repo=project, builtin_dir=builtin, user_dir=user)
        assert errors == []
        explain = registry.get("explain")
        assert explain is not None
        assert explain.kind == SkillKind.BUILTIN

    def test_dependency_check_satisfied(self, tmp_path: Path) -> None:
        """mcp: 依赖可在已注册工具集合中解析。"""
        registry = SkillRegistry()
        assert registry.resolve_dependencies(
            ["mcp:filesystem", "skill:other"],
            registered_mcp={"filesystem"},
            registered_skills={"other"},
        ) == []

    def test_dependency_check_missing_rejected(self, tmp_path: Path) -> None:
        """缺失 mcp / skill 依赖 → 报告缺失项。"""
        registry = SkillRegistry()
        missing = registry.resolve_dependencies(
            ["mcp:filesystem", "skill:ghost"],
            registered_mcp={"math"},
            registered_skills={"other"},
        )
        assert missing == ["mcp:filesystem", "skill:ghost"]
