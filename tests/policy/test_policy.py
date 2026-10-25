"""M2 单元测试:路径沙箱 + 高危命令黑名单 + 分层权限评估器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from policy.evaluator import PolicyEvaluator
from policy.models import Plan, Rule
from policy.rules import check_high_risk
from policy.sandbox import resolve_in_sandbox

# === 沙箱 ===

def test_sandbox_normal_path(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    resolved = resolve_in_sandbox(tmp_path, "a.py")
    assert resolved == (tmp_path / "a.py").resolve()


def test_sandbox_nested_path(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    resolved = resolve_in_sandbox(tmp_path, "sub/b.py")
    assert resolved == (tmp_path / "sub" / "b.py").resolve()


def test_sandbox_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="越界"):
        resolve_in_sandbox(tmp_path, "../escape.py")


def test_sandbox_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="越界"):
        resolve_in_sandbox(tmp_path, str(tmp_path.parent / "outside.txt"))


def test_sandbox_rejects_non_string(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非空字符串"):
        resolve_in_sandbox(tmp_path, 123)
    with pytest.raises(ValueError, match="非空字符串"):
        resolve_in_sandbox(tmp_path, "")


def test_sandbox_blocks_symlink_escape(tmp_path: Path) -> None:
    """沙箱外建真实文件,沙箱内放指向它的符号链接 → 仍被拒。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="越界"):
        resolve_in_sandbox(tmp_path, "link.py")


# === 高危命令黑名单 ===

def test_blacklist_rm_recursive(tmp_path: Path) -> None:
    assert check_high_risk("rm -rf /tmp/x") is not None
    assert check_high_risk("rm -rf /") is not None


def test_blacklist_mkfs_and_fork_bomb() -> None:
    assert check_high_risk("mkfs.ext4 /dev/sda1") is not None
    assert check_high_risk(":(){ :|:& };:") is not None


def test_blacklist_allow_normal_commands() -> None:
    # 带安全前缀的删除视为项目内操作,放行(黑名单本质不可靠,主防线是审批)
    assert check_high_risk("rm -rf ./build") is None
    assert check_high_risk("rm -rf -- build") is None
    assert check_high_risk("rm -rf ./dist") is None
    assert check_high_risk("python -m pytest") is None
    assert check_high_risk("git status") is None
    assert check_high_risk("pip install requests") is None


# === 分层评估器 ===

# 工具分类表(与 tools/ 中 Tool 声明一致;独立测试时显式传入)
TOOLS_SPEC: dict[str, dict[str, object]] = {
    "read_file": {"read_only": True, "path_key": "path"},
    "list_dir": {"read_only": True, "path_key": "path"},
    "write_file": {"read_only": False, "path_key": "path"},
    "run_command": {"read_only": False, "path_key": None},
}


def _ev(workdir: Path, **kwargs: Any) -> PolicyEvaluator:
    return PolicyEvaluator(workdir, tools_spec=TOOLS_SPEC, **kwargs)


def test_eval_read_permit(tmp_path: Path) -> None:
    ev = _ev(tmp_path)
    d = ev.evaluate("read_file", {"path": "a.py"})
    assert d.decision == "permit"


def test_eval_write_ask_in_normal_mode(tmp_path: Path) -> None:
    ev = _ev(tmp_path)
    d = ev.evaluate("write_file", {"path": "a.py", "content": "x"})
    assert d.decision == "ask"
    assert d.layer == "mode"


def test_eval_write_permit_when_in_plan(tmp_path: Path) -> None:
    ev = _ev(tmp_path, plan=Plan(files=["src"]))
    d = ev.evaluate("write_file", {"path": "src/a.py", "content": "x"})
    assert d.decision == "permit"
    assert d.layer == "plan"
    # 目录前缀匹配
    assert ev.evaluate("write_file", {"path": "src/sub/b.py", "content": "x"}).decision == "permit"
    # 精确文件匹配
    assert ev.evaluate("write_file", {"path": "src/a.py", "content": "x"}).layer == "plan"


def test_eval_run_command_permit_when_in_plan_commands(tmp_path: Path) -> None:
    """run_command 命中计划命令白名单 → permit(修复 review 发现的严格模式恒拒 bug)。"""
    ev = _ev(tmp_path, plan=Plan(commands=["python -m pytest"]))
    d = ev.evaluate("run_command", {"command": "python -m pytest -q"})
    assert d.decision == "permit"
    assert d.layer == "plan"


def test_eval_run_command_ask_when_not_in_plan_commands(tmp_path: Path) -> None:
    ev = _ev(tmp_path, plan=Plan(commands=["python -m pytest"]))
    d = ev.evaluate("run_command", {"command": "git status"})
    assert d.decision == "ask"
    assert d.layer == "mode"


def test_eval_strict_write_deny_outside_plan(tmp_path: Path) -> None:
    ev = _ev(tmp_path, mode="strict", plan=Plan(files=["src/a.py"]))
    d = ev.evaluate("write_file", {"path": "other.py", "content": "x"})
    assert d.decision == "deny"
    assert d.layer == "plan"


def test_eval_strict_write_ask_even_in_plan(tmp_path: Path) -> None:
    ev = _ev(tmp_path, mode="strict", plan=Plan(files=["src/a.py"]))
    d = ev.evaluate("write_file", {"path": "src/a.py", "content": "x"})
    assert d.decision == "ask"


def test_eval_sandbox_deny_beats_plan(tmp_path: Path) -> None:
    """越界路径是硬边界:即使命中计划也不放行。"""
    ev = _ev(tmp_path, plan=Plan(files=["a.py"]))
    d = ev.evaluate("write_file", {"path": "../a.py", "content": "x"})
    assert d.decision == "deny"
    assert d.layer == "sandbox"


def test_eval_blacklist_deny(tmp_path: Path) -> None:
    ev = _ev(tmp_path)
    d = ev.evaluate("run_command", {"command": "rm -rf /"})
    assert d.decision == "deny"
    assert d.layer == "blacklist"


def test_eval_rule_deny_takes_priority(tmp_path: Path) -> None:
    """用户规则拒绝优先于计划允许。"""
    ev = _ev(tmp_path, plan=Plan(files=["a.py"]), rules=[Rule(tool="write_file", effect="deny")])
    d = ev.evaluate("write_file", {"path": "a.py", "content": "x"})
    assert d.decision == "deny"
    assert d.layer == "rules"


def test_eval_rule_pattern_permit_then_deny(tmp_path: Path) -> None:
    ev = _ev(
        tmp_path,
        rules=[
            Rule(tool="run_command", pattern="pytest", effect="permit"),
            Rule(tool="run_command", pattern="pytest", effect="deny"),
        ],
    )
    assert ev.evaluate("run_command", {"command": "python -m pytest"}).decision == "permit"
    assert ev.evaluate("run_command", {"command": "python -m pytest x"}).decision == "permit"


def test_eval_rule_pattern_deny_after_permit(tmp_path: Path) -> None:
    """先允许后拒绝的覆盖语义:run_command 默认 ask,显式 deny 覆盖。"""
    ev = _ev(
        tmp_path,
        rules=[
            Rule(tool="run_command", pattern="pytest", effect="deny"),
            Rule(tool="run_command", pattern="git status", effect="permit"),
        ],
    )
    d = ev.evaluate("run_command", {"command": "python -m pytest"})
    assert d.decision == "deny"
    assert ev.evaluate("run_command", {"command": "git status"}).decision == "permit"


def test_eval_run_command_treated_as_write(tmp_path: Path) -> None:
    """run_command 在 normal 无计划下走 ask(不能静态判定读写性)。"""
    ev = _ev(tmp_path)
    assert ev.evaluate("run_command", {"command": "python -m pytest"}).decision == "ask"
