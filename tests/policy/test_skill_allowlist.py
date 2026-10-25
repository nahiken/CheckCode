"""M15 单元测试:Skill 工具白名单层(policy/evaluator.py)。

规格要点(见 FEATURE_COMPLETION_PLAN §M15):
- `tools_allow is None` → **完全不限制**(兼容纪律:默认路径不变)。
- `tools_allow` 为集合 → 未列出的工具一律 deny,layer == "skill"。
- 层序:**沙箱/黑名单硬边界在前**(白名单不可使其失效),skill 层在规则/计划/模式之前。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from policy.evaluator import PolicyEvaluator
from policy.models import Plan, Rule

# 与 tests/policy/test_policy.py 同一张工具分类表(与 tools/ 声明一致)
TOOLS_SPEC: dict[str, dict[str, object]] = {
    "read_file": {"read_only": True, "path_key": "path"},
    "list_dir": {"read_only": True, "path_key": "path"},
    "write_file": {"read_only": False, "path_key": "path"},
    "run_command": {"read_only": False, "path_key": None},
}


def _ev(workdir: Path, **kwargs: Any) -> PolicyEvaluator:
    return PolicyEvaluator(workdir, tools_spec=TOOLS_SPEC, **kwargs)


# === 兼容纪律:None = 不限制 ===


def test_none_means_unrestricted(tmp_path: Path) -> None:
    """tools_allow 缺省(None)→ 与不带该层时逐字段一致(兼容纪律)。"""
    ro = _ev(tmp_path).evaluate("read_file", {"path": "a.py"})
    assert ro == _ev(tmp_path, tools_allow=None).evaluate("read_file", {"path": "a.py"})
    wr = _ev(tmp_path).evaluate("write_file", {"path": "a.py", "content": "x"})
    assert wr == _ev(tmp_path, tools_allow=None).evaluate(
        "write_file", {"path": "a.py", "content": "x"}
    )
    cmd = _ev(tmp_path).evaluate("run_command", {"command": "git status"})
    assert cmd == _ev(tmp_path, tools_allow=None).evaluate(
        "run_command", {"command": "git status"}
    )


# === 白名单命中/未命中 ===


def test_listed_tool_passes_through(tmp_path: Path) -> None:
    """白名单内的工具:继续走后面的层(只读 → 模式层放行)。"""
    d = _ev(tmp_path, tools_allow={"read_file"}).evaluate("read_file", {"path": "a.py"})
    assert d.decision == "permit"
    assert d.layer == "mode"  # 不是 skill 层


def test_unlisted_tool_denied_with_skill_layer(tmp_path: Path) -> None:
    d = _ev(tmp_path, tools_allow={"read_file"}).evaluate(
        "write_file", {"path": "a.py", "content": "x"}
    )
    assert d.decision == "deny"
    assert d.layer == "skill"
    assert "write_file" in d.reason


def test_empty_allowlist_denies_everything(tmp_path: Path) -> None:
    """空集合 ≠ None:显式声明「不许用任何工具」是合法收窄,必须全拒。"""
    ev = _ev(tmp_path, tools_allow=set())
    assert ev.evaluate("read_file", {"path": "a.py"}).layer == "skill"
    assert ev.evaluate("list_dir", {"path": "."}).layer == "skill"


def test_allowlist_denies_even_permitted_by_plan(tmp_path: Path) -> None:
    """白名单层在计划层之前:即使写操作命中已审批计划,未列入白名单仍被拒。"""
    ev = _ev(tmp_path, tools_allow={"read_file"}, plan=Plan(files=["a.py"]))
    d = ev.evaluate("write_file", {"path": "a.py", "content": "x"})
    assert d.decision == "deny"
    assert d.layer == "skill"


def test_allowlist_denies_even_with_permit_rule(tmp_path: Path) -> None:
    """白名单层在用户规则之前:显式 permit 规则也不能把未列入的工具放进来。"""
    ev = _ev(tmp_path, tools_allow={"read_file"}, rules=[Rule(tool="write_file", effect="permit")])
    assert ev.evaluate("write_file", {"path": "a.py", "content": "x"}).layer == "skill"


# === 层序:硬边界不受白名单影响 ===


def test_allowlisted_tool_still_subject_to_sandbox(tmp_path: Path) -> None:
    """白名单**内**的工具仍逃不脱沙箱 —— 白名单不构成沙箱豁免。"""
    ev = _ev(tmp_path, tools_allow={"read_file"})
    d = ev.evaluate("read_file", {"path": "../escape.py"})
    assert d.decision == "deny"
    assert d.layer == "sandbox"


def test_sandbox_wins_when_both_skill_and_sandbox_would_deny(tmp_path: Path) -> None:
    """同一次调用**同时**违反白名单与沙箱 → 必须报 sandbox(证明 skill 层在沙箱**之后**)。

    ⚠️ 这是承重断言:被调工具必须**不在白名单内**,否则 skill 层永不触发 ——
    那样无论层序如何都只看得到沙箱(审计用变异测试证明:把 skill 层挪到沙箱**之前**,
    旧写法 `tools_allow={"read_file"}` + 读 `read_file` 仍然通过,即断言是单侧的)。
    """
    ev = _ev(tmp_path, tools_allow={"write_file"})  # read_file 不在白名单
    d = ev.evaluate("read_file", {"path": "../escape.py"})  # 既越界、又不在白名单
    assert d.decision == "deny"
    assert d.layer == "sandbox"  # 不是 skill


def test_allowlisted_run_command_still_subject_to_blacklist(tmp_path: Path) -> None:
    """白名单列了 run_command,高危命令仍被黑名单拦下。"""
    ev = _ev(tmp_path, tools_allow={"run_command"})
    d = ev.evaluate("run_command", {"command": "rm -rf /"})
    assert d.decision == "deny"
    assert d.layer == "blacklist"


def test_blacklist_wins_when_both_skill_and_blacklist_would_deny(tmp_path: Path) -> None:
    """同一次调用同时违反白名单与黑名单 → 必须报 blacklist(层序承重断言,同沙箱那条)。"""
    ev = _ev(tmp_path, tools_allow={"read_file"})  # run_command 不在白名单
    d = ev.evaluate("run_command", {"command": "rm -rf /"})  # 既高危、又不在白名单
    assert d.decision == "deny"
    assert d.layer == "blacklist"  # 不是 skill


def test_uncapped_tool_falls_to_blacklist_when_allowlist_unrestricted(tmp_path: Path) -> None:
    """None 时 run_command 照旧被黑名单拦(对照基线,防「加层顺手改了顺序」)。"""
    d = _ev(tmp_path).evaluate("run_command", {"command": "rm -rf /"})
    assert d.layer == "blacklist"


def test_unknown_tool_denied_when_restricted(tmp_path: Path) -> None:
    """分类表里没有的工具:受限时 deny(保守);不限制时按写工具走模式层。"""
    assert _ev(tmp_path, tools_allow={"read_file"}).evaluate("nope", {}).layer == "skill"
    assert _ev(tmp_path).evaluate("nope", {}).layer == "mode"
