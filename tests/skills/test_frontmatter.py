"""M5 单元测试:SKILL.md frontmatter 解析(轻量 YAML 子集,不引入 pyyaml)。"""

from __future__ import annotations

import pytest

from skills.frontmatter import parse_frontmatter


class TestParseFrontmatter:
    def test_full_metadata(self) -> None:
        text = """---
name: my-skill
description: 做一件事
allowedTools:
  - mcp:filesystem
  - skill:other
---

正文说明
"""
        meta = parse_frontmatter(text)
        assert meta.name == "my-skill"
        assert meta.description == "做一件事"
        assert meta.allowed_tools == ["mcp:filesystem", "skill:other"]

    def test_minimal_metadata(self) -> None:
        text = """---
name: minimal
---

正文
"""
        meta = parse_frontmatter(text)
        assert meta.name == "minimal"
        assert meta.allowed_tools == []

    def test_no_frontmatter(self) -> None:
        # 无 frontmatter → 解析失败(必须显式声明)
        with pytest.raises(ValueError):
            parse_frontmatter("纯正文,无 frontmatter")

    def test_no_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name"):
            parse_frontmatter("---\ndescription: 无名字\n---\n正文")

    def test_preserves_body(self) -> None:
        text = "---\nname: x\n---\n# 标题\n正文内容\n"
        meta = parse_frontmatter(text)
        assert "标题" in meta.body

    def test_multiline_list_variants(self) -> None:
        """列表可用 '-' 或逗号分隔;键可带引号。"""
        text = """---
name: k
description: "带引号的描述"
allowedTools: "mcp:a, skill:b"
---

正文
"""
        meta = parse_frontmatter(text)
        assert meta.description == "带引号的描述"
        assert meta.allowed_tools == ["mcp:a", "skill:b"]
