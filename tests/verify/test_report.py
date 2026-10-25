"""M3 单元测试:RiskReport 风险推导 + 渲染。"""

from __future__ import annotations

from pathlib import Path

from policy.models import Plan
from verify.report import RiskReport, derive_remaining_risk
from verify.runner import TestResult


def _ok(command: str = "pytest") -> TestResult:
    return TestResult(command=command, passed=True, exit_code=0, output="ok", duration_ms=10.0)


def _fail(command: str = "pytest") -> TestResult:
    return TestResult(command=command, passed=False, exit_code=1, output="boom", duration_ms=5.0)


def _timeout(command: str = "pytest") -> TestResult:
    return TestResult(
        command=command, passed=False, exit_code=None, output="(验证命令超时)",
        duration_ms=1000.0, timed_out=True,
    )


# === derive_remaining_risk ===

def test_risk_low_when_passed() -> None:
    risk = derive_remaining_risk(
        rounds=[_ok()], changed_files=[], plan=Plan(), max_fix_rounds=2
    )
    assert "低:所有验证命令通过" in risk


def test_risk_high_when_failed_to_limit() -> None:
    risk = derive_remaining_risk(
        rounds=[_fail(), _fail()], changed_files=[], plan=Plan(), max_fix_rounds=2
    )
    assert "高:第 2 轮后测试仍失败" in risk
    assert "boom" in risk


def test_risk_high_on_timeout() -> None:
    risk = derive_remaining_risk(
        rounds=[_timeout()], changed_files=[], plan=Plan(), max_fix_rounds=2
    )
    assert "高:验证命令超时" in risk


def test_risk_out_of_plan_extra_files() -> None:
    """修改了计划外文件 → 附加中风险(越权修改)。"""
    risk = derive_remaining_risk(
        rounds=[_ok()],
        changed_files=["math_utils.py", "other.py"],
        plan=Plan(files=["math_utils.py"]),
        max_fix_rounds=2,
    )
    assert "低:所有验证命令通过" in risk
    assert "中:修改了计划外文件 ['other.py']" in risk


def test_risk_plan_covered_no_extra() -> None:
    """全部修改都在计划内 → 无越权风险附加。"""
    risk = derive_remaining_risk(
        rounds=[_ok()],
        changed_files=["src/a.py", "src/sub/b.py"],
        plan=Plan(files=["src"]),
        max_fix_rounds=2,
    )
    assert "低:所有验证命令通过" in risk
    assert "计划外" not in risk


def test_risk_empty_rounds_fail() -> None:
    """无验证轮次(理论上不可能,但保守视为失败)。"""
    risk = derive_remaining_risk(
        rounds=[], changed_files=[], plan=Plan(), max_fix_rounds=2
    )
    assert "高:" in risk


# === RiskReport ===

def test_report_render_contains_key_fields(tmp_path: Path) -> None:
    report = RiskReport(
        repo=tmp_path,
        base_commit="abc123",
        branch="feat/abc",
        changed_files=["math_utils.py"],
        test_command="python -m pytest tests/ -q",
        rounds=[_ok()],
        rounds_used=1,
        remaining_risk="低:所有验证命令通过",
        diff="--- a/math_utils.py\n+++ b/math_utils.py",
    )
    text = report.render()
    assert "abc123" in text
    assert "feat/abc" in text
    assert "math_utils.py" in text
    assert "python -m pytest tests/ -q" in text
    assert "低:所有验证命令通过" in text
    assert "## Diff" in text
    assert "+" in text


def test_report_passed_projected_from_rounds(tmp_path: Path) -> None:
    """passed 是属性,由末轮推导(单一来源)。"""
    ok_report = RiskReport(
        repo=tmp_path, base_commit="a", branch="b",
        changed_files=[], test_command="pytest", rounds=[_ok()],
        rounds_used=1, remaining_risk="低", diff="",
    )
    fail_report = RiskReport(
        repo=tmp_path, base_commit="a", branch="b",
        changed_files=[], test_command="pytest", rounds=[_ok(), _fail()],
        rounds_used=2, remaining_risk="高", diff="",
    )
    assert ok_report.passed is True
    assert fail_report.passed is False


def test_report_last_round_property(tmp_path: Path) -> None:
    report = RiskReport(
        repo=tmp_path, base_commit="a", branch="b",
        changed_files=[], test_command="pytest", rounds=[_ok(), _fail()],
        rounds_used=2, remaining_risk="高", diff="",
    )
    assert report.last_round is not None
    assert report.last_round.passed is False


def test_report_empty_rounds_render(tmp_path: Path) -> None:
    report = RiskReport(
        repo=tmp_path, base_commit="a", branch="b",
        changed_files=[], test_command="pytest", rounds=[],
        rounds_used=0, remaining_risk="", diff="",
    )
    assert report.last_round is None
    assert report.passed is False
    assert "验证记录" in report.render()
