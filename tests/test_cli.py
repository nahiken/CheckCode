"""M0 CLI 层测试:子命令路由、参数校验、退出码。

覆盖:
- --version / --help / 无子命令(打印 help)
- run:缺 --task 时报错(退出码 2)、带 --task 正常
- resume / eval 子命令可用
- 未知子命令退出码(argparse invalid choice -> 2)

说明:CLI 的错误提示为中文,且在 Windows 上子进程输出可能是 GBK 编码,
为保证跨平台稳定,断言只依赖 ASCII 子串与退出码,不做中文匹配。
"""

from __future__ import annotations

import subprocess
import sys


def run_cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    """以子进程方式运行 CLI,返回原始字节(避免编码依赖)。"""
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
        capture_output=True,
        text=False,
        check=False,
    )


def _text(b: bytes) -> str:
    """宽容解码,仅用于 ASCII 子串断言。"""
    return b.decode("utf-8", errors="replace")


def test_cli_module_importable() -> None:
    """cli.main 可导入且暴露 main()。"""
    from cli.main import main as _m

    assert callable(_m)


def test_cli_version() -> None:
    r = run_cli("--version")
    assert r.returncode == 0
    assert "whycode 0.6.0" in _text(r.stdout)


def test_cli_help() -> None:
    r = run_cli("--help")
    assert r.returncode == 0
    out = _text(r.stdout)
    assert "mewcode" in out
    assert "run" in out and "resume" in out and "eval" in out


def test_cli_no_command_prints_help() -> None:
    r = run_cli()
    assert r.returncode == 0
    assert "usage" in _text(r.stdout)


def test_cli_run_without_task_errors() -> None:
    r = run_cli("run")
    assert r.returncode == 2  # argparse parser.error -> SystemExit(2)
    err = _text(r.stderr)
    assert "error" in err
    assert "--task" in err  # 提示 run 需要 --task 参数(中文部分不做跨平台匹配)


def test_cli_run_with_task_ok() -> None:
    """M3:run 已接线真实闭环——无 API key 时打印错误并退出 1(不再回显任务)。

    把 API key 环境变量置空串(存在但为空 → load_dotenv(override=False) 不覆盖、
    get() 为 falsy → 无 key),保证确定性:该路径不触网。
    """
    env = dict(__import__("os").environ)
    for var in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        env[var] = ""
    r = subprocess.run(
        [sys.executable, "-m", "cli.main", "run", "--task", "hello world"],
        capture_output=True,
        text=False,
        check=False,
        env=env,
    )
    assert r.returncode == 1  # 未配置 API key
    text = _text(r.stdout) + _text(r.stderr)
    assert "API key" in text


def test_cli_eval_unknown_task_errors() -> None:
    """M8:eval --task 未知 id → 报错退出 1。"""
    r = subprocess.run(
        [sys.executable, "-m", "cli.main", "eval", "--task", "nope"],
        capture_output=True,
        text=False,
        check=False,
    )
    assert r.returncode == 1
    assert "nope" in _text(r.stdout) + _text(r.stderr)


def test_cli_eval_help_lists_flags() -> None:
    """M8:eval 子命令帮助含 --baseline/--save-baseline/--task。"""
    r = run_cli("eval", "--help")
    assert r.returncode == 0
    out = _text(r.stdout)
    assert "--baseline" in out and "--save-baseline" in out and "--task" in out


def test_cli_unknown_command_exit_code() -> None:
    r = run_cli("bogus")
    assert r.returncode == 2  # 未知子命令由 argparse 拒绝
    assert "invalid choice" in _text(r.stderr)


def test_cli_resume_available() -> None:
    """M4:resume 需要 --session(缺省报 argparse 错误,退出码 2)。"""
    r = run_cli("resume")
    assert r.returncode == 2
    assert "--session" in _text(r.stderr)


def test_cli_resume_missing_session_store() -> None:
    """resume --session 指向不存在的会话 → 报错退出码 1(不触网)。"""
    env = dict(__import__("os").environ)
    for var in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        env[var] = ""
    r = subprocess.run(
        [sys.executable, "-m", "cli.main", "resume", "--session", "sess-nope"],
        capture_output=True,
        text=False,
        check=False,
        env=env,
    )
    # 无 API key 时先报 key 错误;有 key(CI)时因 repo 不存在报错。两种都退出 1。
    assert r.returncode == 1
    text = _text(r.stdout) + _text(r.stderr)
    assert text  # 有输出(错误信息)即通过,不做中文匹配


def test_cli_eval_available() -> None:
    """M8:eval 子命令已接线。无 API key 时走确定性路径(退出 1,不触网)。

    注意:若有真实 API key,`mewcode eval` 会跑完整 11 条真实评测——那是
    真实的端到端评测(不是单元测试),应手动触发而非在测试套件里跑。
    """
    env = dict(__import__("os").environ)
    for var in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        env[var] = ""
    r = subprocess.run(
        [sys.executable, "-m", "cli.main", "eval"],
        capture_output=True,
        text=False,
        check=False,
        env=env,
    )
    assert r.returncode == 1  # 无 API key → 报错退出
    text = _text(r.stdout) + _text(r.stderr)
    assert "API key" in text


def test_cli_task_rejected_by_resume() -> None:
    """--task 是 run 子命令专属参数,resume 不应接受。

    自 argparse 改为 subparsers 后,--task 仅定义于 run。resume 传入时
    argparse 报错(缺 --session 或 unrecognized),退出码 2。
    """
    r = run_cli("resume", "--task", "x")
    assert r.returncode == 2
    assert "error" in _text(r.stderr)
