"""skills.frontmatter —— SKILL.md frontmatter 解析(轻量 YAML 子集)。

只解析 Skill 需要的最小字段子集,不引入 pyyaml:
- name: 技能名(必填)
- description: 描述(可选)
- allowedTools: 依赖列表(每行 `- mcp:xxx` / `- skill:yyy`;或用引号包裹的逗号分隔字符串)

frontmatter 以 `---` 行开始/结束。缺失 frontmatter → 解析失败(必须显式声明)。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillMeta:
    name: str
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    body: str = ""  # frontmatter 之后的正文


def _parse_list_item(raw: str) -> list[str]:
    """解析一个 allowedTools 值:可含多个逗号分隔项或单个 '-' 列表项。"""
    item = raw.strip()
    if not item:
        return []
    if item.startswith("-"):
        item = item[1:].strip()
    # 去掉引号包裹
    if len(item) >= 2 and item[0] in "\"'" and item[-1] == item[0]:
        item = item[1:-1]
    return [part.strip() for part in item.split(",") if part.strip()]


def _strip_quotes(value: str) -> str:
    """去掉包裹的引号(单/双)。"""
    v = value.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> SkillMeta:
    """解析 SKILL.md:frontmatter + 正文。无 frontmatter / 缺 name → ValueError。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 必须以 '---' frontmatter 开头")
    # 找结束的 '---'
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("SKILL.md frontmatter 缺少结束 '---'")

    fields: dict[str, str] = {}
    allowed: list[str] = []
    current_key: str | None = None
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            allowed.extend(_parse_list_item(stripped))
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current_key = key.strip().lower()
            value = value.strip()
            if current_key == "allowedtools":
                allowed.extend(_parse_list_item(value))
            elif value:
                fields[current_key] = _strip_quotes(value)
        elif current_key == "allowedtools":
            # 续行(可能是未缩进的逗号分隔)
            allowed.extend(_parse_list_item(stripped))

    name = _strip_quotes(fields.get("name", ""))
    if not name:
        raise ValueError("SKILL.md frontmatter 必须声明 name")
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return SkillMeta(
        name=name,
        description=_strip_quotes(fields.get("description", "")),
        allowed_tools=allowed,
        body=body,
    )
