"""M15 单元测试:allowedTools 接受裸本地工具名 + 对照注册工具集校验。

兼容纪律(M5 语义不回归):
- 不传 registered_tools → 裸名一律非法(与 M5 完全一致,`test_loader.py` 不动)。
- 传了集合 → 集合内的裸名合法,集合外仍非法。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.loader import SkillKind, SkillRegistry, load_skill

REGISTERED = {"read_file", "write_file", "list_dir", "run_command"}


def _write_skill(
    root: Path,
    name: str,
    *,
    allowed: list[str] | None = None,
    kind_dir: str = ".whycode/skills",
) -> Path:
    lines = ["---", f"name: {name}", "description: 说明"]
    if allowed is not None:
        lines.append("allowedTools:")
        for a in allowed:
            lines.append(f"  - {a}")
    lines.append("---")
    p = root / kind_dir / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


class TestBareLocalToolNames:
    def test_bare_registered_tool_accepted(self, tmp_path: Path) -> None:
        p = _write_skill(tmp_path, "editor", allowed=["read_file", "write_file"])
        skill = load_skill(p, kind=SkillKind.PROJECT, registered_tools=REGISTERED)
        assert skill.allowed_tools == ["read_file", "write_file"]

    def test_bare_unregistered_tool_rejected(self, tmp_path: Path) -> None:
        p = _write_skill(tmp_path, "bad", allowed=["read_file", "ghost_tool"])
        with pytest.raises(ValueError, match="ghost_tool"):
            load_skill(p, kind=SkillKind.PROJECT, registered_tools=REGISTERED)

    def test_prefixed_forms_still_accepted(self, tmp_path: Path) -> None:
        """mcp:/skill: 前缀形式不受影响(即使不在本地工具集里)。"""
        p = _write_skill(tmp_path, "mcpuser", allowed=["mcp:fs", "skill:other"])
        skill = load_skill(p, kind=SkillKind.PROJECT, registered_tools=REGISTERED)
        assert skill.allowed_tools == ["mcp:fs", "skill:other"]

    def test_without_registered_tools_bare_name_rejected(self, tmp_path: Path) -> None:
        """不传集合 → 裸名非法,M5 错误消息不变(兼容纪律)。"""
        p = _write_skill(tmp_path, "bad", allowed=["read_file"])
        with pytest.raises(ValueError, match="mcp:"):
            load_skill(p, kind=SkillKind.PROJECT)


class TestRegistryValidation:
    def test_load_all_validates_bare_names(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _write_skill(tmp_path, "editor", allowed=["read_file"])
        reg = SkillRegistry()
        errors = reg.load_all(
            repo=tmp_path, builtin_dir=builtin, user_dir=None, registered_tools=REGISTERED
        )
        assert errors == []
        assert reg.get("editor") is not None

    def test_load_all_rejects_unregistered_bare_name(self, tmp_path: Path) -> None:
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _write_skill(tmp_path, "bad", allowed=["ghost_tool"])
        reg = SkillRegistry()
        errors = reg.load_all(
            repo=tmp_path, builtin_dir=builtin, user_dir=None, registered_tools=REGISTERED
        )
        assert len(errors) == 1
        assert "ghost_tool" in errors[0]
        assert reg.get("bad") is None

    def test_resolve_dependencies_local_tool(self) -> None:
        reg = SkillRegistry()
        assert (
            reg.resolve_dependencies(
                ["read_file"], registered_mcp=set(), registered_skills=set(),
                registered_tools=REGISTERED,
            )
            == []
        )
        assert reg.resolve_dependencies(
            ["ghost_tool"], registered_mcp=set(), registered_skills=set(),
            registered_tools=REGISTERED,
        ) == ["ghost_tool"]

    def test_resolve_dependencies_default_keeps_m5_semantics(self) -> None:
        """不传 registered_tools → 裸名算缺失(与 M5 一致)。"""
        reg = SkillRegistry()
        assert reg.resolve_dependencies(
            ["read_file"], registered_mcp=set(), registered_skills=set()
        ) == ["read_file"]
