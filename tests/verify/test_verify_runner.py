"""M3 单元测试:确定性测试执行(TestRunner)。"""

from __future__ import annotations

from pathlib import Path

from verify.runner import MAX_VERIFY_OUTPUT, TestRunner


def _runner(tmp_path: Path) -> TestRunner:
    return TestRunner(tmp_path)


def test_pass(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = r.run("echo ok && exit 0")
    assert result.passed is True
    assert result.exit_code == 0
    assert "ok" in result.output
    assert result.timed_out is False
    assert result.duration_ms is not None


def test_fail_exit_code(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = r.run("echo boom && exit 3")
    assert result.passed is False
    assert result.exit_code == 3
    assert "boom" in result.output


def test_utf8_output(tmp_path: Path) -> None:
    """UTF-8 字节输出被正确解码(模拟 pytest 的 UTF-8 输出契约)。

    子进程经管道输出:Windows 控制台默认 GBK,故测试用 sys.stdout.buffer.write
    写原始 UTF-8 字节,验证解码路径(真实场景 pytest 强制 UTF-8)。
    """
    r = _runner(tmp_path)
    result = r.run(
        "python -c \"import sys; "
        "sys.stdout.buffer.write('中文验证输出'.encode('utf-8'))\""
    )
    assert "中文验证输出" in result.output


def test_timeout(tmp_path: Path) -> None:
    r = TestRunner(tmp_path, timeout=0.1)
    result = r.run("python -c \"import time; time.sleep(2)\"")
    assert result.passed is False
    assert result.timed_out is True
    assert result.exit_code is None


def test_stdout_fallback_to_stderr(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    # stdout 为空、stderr 有内容 → 回退 stderr(原始 UTF-8 字节)
    result = r.run(
        "python -c \"import sys; "
        "sys.stderr.buffer.write('仅 stderr'.encode('utf-8')); sys.exit(1)\""
    )
    assert result.passed is False
    assert "仅 stderr" in result.output


def test_truncates_long_output(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = r.run(f"python -c \"print('x' * {MAX_VERIFY_OUTPUT * 3})\"")
    assert result.passed is True
    assert len(result.output) < MAX_VERIFY_OUTPUT * 2  # 已截断
    assert "已截断" in result.output


def test_empty_output_placeholder(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    result = r.run("exit 0")  # 无任何输出
    assert result.passed is True
    assert "(无输出)" in result.output
