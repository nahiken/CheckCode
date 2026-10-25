"""M8 单元测试:评测任务集 + 单任务评估 + 指标汇总 + 基线回归。

用 httpx.MockTransport 注入 mock agent(不真实调用 LLM),真实 pytest 在
demo_repo 副本中运行。核心断言:任务集规范、单任务结果口径、指标公式。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent.config import LLMConfig
from eval.metrics import EvalResult, EvalSummary
from eval.tasks import EVAL_TASKS, get_task

# ============ 任务集 ============

class TestEvalTasks:
    def test_11_tasks_with_unique_ids(self) -> None:
        assert len(EVAL_TASKS) == 11
        ids = [t.id for t in EVAL_TASKS]
        assert len(set(ids)) == len(ids)

    def test_tasks_have_verify_and_plan_files(self) -> None:
        for t in EVAL_TASKS:
            assert t.task
            assert t.verify
            # 只读类无计划文件;其余必须有(越权判定依据)
            if t.kind in ("fix", "test", "recovery", "scope"):
                assert t.plan_files, f"{t.id} 缺 plan_files"

    def test_get_task_by_id(self) -> None:
        t = get_task("fix-divide")
        assert t.id == "fix-divide"
        assert "divide" in t.task
        with pytest.raises(KeyError):
            get_task("nope")

    def test_all_four_kinds_present(self) -> None:
        kinds = {t.kind for t in EVAL_TASKS}
        assert {"fix", "test", "readonly", "recovery", "scope"} <= kinds


# ============ 单任务评估 ============

def _config() -> LLMConfig:
    return LLMConfig(base_url="http://test", api_key="k", model="m", max_steps=20)


def _mock_transport(requests: list[str]) -> httpx.MockTransport:
    """按序返回 SSE 响应(修复任务:read → write → readback → 完成)。"""
    req_iter = iter(requests)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(req_iter))

    return httpx.MockTransport(handler)


def _sse(data: dict[str, object]) -> str:
    return f"data: {json.dumps(data)}\n\n"


# 正确修复后的 math_utils.py(demo_repo 全部测试通过)
_FIXED_MATH_UTILS = (
    '"""math_utils"""\n'
    "\n\n"
    "def add(a, b):\n    return a + b\n\n\n"
    "def subtract(a, b):\n    return a - b\n\n\n"
    "def divide(a, b):\n"
    '    if b == 0:\n        raise ValueError("division by zero")\n'
    "    return a / b\n\n\n"
    "def fib(n):\n"
    "    if n < 0:\n        return -1\n"
    "    if n <= 1:\n        return n\n"
    "    return fib(n - 1) + fib(n - 2)\n"
)


def _fix_turn(content: str | None = None) -> str:
    """回合:write_file 写入修复内容(默认正确修复 divide/fib),带 usage。"""
    fixed = content or _FIXED_MATH_UTILS
    return (
        _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": json.dumps({"path": "math_utils.py", "content": fixed})}}]}, "finish_reason": None}]})
        + _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        + _sse({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": []})
    )


def _finish_turn(text: str = "完成") -> str:
    return (
        _sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]})
        + _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        + _sse({"usage": {"prompt_tokens": 6, "completion_tokens": 2}, "choices": []})
    )


@pytest.fixture()
def demo_repo_work() -> Path:
    """demo_repo 可写副本(复用 conftest demo_repo fixture 语义)。"""
    from verify.prepare import prepare_repo

    return prepare_repo(None)


class TestRunner:
    @pytest.mark.asyncio
    async def test_fix_task_passes(self, demo_repo_work: Path) -> None:
        from eval.runner import run_eval_task

        # mock agent:修复(写正确 math_utils)→ 真实 pytest 通过
        transport = _mock_transport([_fix_turn(), _finish_turn("修复完成")])
        result = await run_eval_task(
            get_task("fix-divide"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=2,
        )
        assert result.ok is True
        assert result.passed is True
        assert result.elapsed > 0
        assert result.tokens > 0  # usage 采集

    @pytest.mark.asyncio
    async def test_fix_task_fails_when_agent_does_not_fix(self, demo_repo_work: Path) -> None:
        from eval.runner import run_eval_task

        # mock agent:只读不写 → 测试恒失败
        transport = _mock_transport([_finish_turn("我没改"), _finish_turn("还是不改")])
        result = await run_eval_task(
            get_task("fix-divide"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=2,
        )
        assert result.ok is False
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_acceptance_runs_on_final_branch_not_baseline(self, demo_repo_work: Path) -> None:
        """⚠️ 承重:独立验收必须跑在**任务分支的最终态**上,不是基线仓库。

        闭环把改动 commit 到任务分支后拆掉 worktree,**主工作树停在基线**。若验收
        直接在 `repo` 里跑(passed 之前被写成 `ok` 的那种实现就容易这样),测的是
        **未修改的仓库**,结果恒为失败 —— 且会伪装成「闭环乐观、验收严格」的
        假信号。本测试用「修复成功」的任务锁死:验收必须看到改动。
        """
        from eval.runner import run_eval_task

        transport = _mock_transport([_fix_turn(), _finish_turn("修复完成")])
        result = await run_eval_task(
            get_task("fix-divide"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=2,
        )
        assert result.ok is True  # 验收在分支最终态上跑 → 看到修复
        assert result.verify_rounds_passed is True

    @pytest.mark.asyncio
    async def test_scope_task_detects_out_of_scope(self, demo_repo_work: Path) -> None:
        from eval.runner import run_eval_task

        # mock agent:修复 divide(计划内)+ 同时写计划外 README.md
        fixed = json.dumps({"path": "math_utils.py", "content": _FIXED_MATH_UTILS})
        scope_args = json.dumps({"path": "README.md", "content": "# hijack"})
        scope_turn = (
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": fixed}},
                {"index": 1, "id": "c2", "function": {"name": "write_file", "arguments": scope_args}},
            ]}, "finish_reason": None}]})
            + _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
            + _sse({"usage": {"prompt_tokens": 4, "completion_tokens": 2}, "choices": []})
        )
        transport = _mock_transport([scope_turn, _finish_turn("done")])
        result = await run_eval_task(
            get_task("scope-guard"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=2,
        )
        # README.md 不在 plan.files → 越权修改判定;math_utils 修复 → 验收通过
        assert result.out_of_scope is True
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_readonly_task_reports_no_changes(self, demo_repo_work: Path) -> None:
        from eval.runner import run_eval_task

        # 只读类:agent 不改文件 → changed_files 空
        transport = _mock_transport([_finish_turn("分析如下...")])
        result = await run_eval_task(
            get_task("readonly-describe"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=1,
        )
        assert result.is_readonly is True
        assert result.changed_files == []

    @pytest.mark.asyncio
    async def test_recovery_task_resumes_second_phase(self, demo_repo_work: Path) -> None:
        """恢复类:两阶段(phase1 未修完 → phase2 同 session resume 修完)→ recovered=True。

        transport 序列:phase1 写坏(验证失败)+ phase2 写对(修复完成)。
        两阶段共用 repo + session,第二次 run 自动走 VerifiedLoop resume 路径。
        """
        from eval.runner import run_eval_task

        broken = _FIXED_MATH_UTILS.replace('raise ValueError("division by zero")', 'raise ZeroDivisionError("boom")')
        broken_turn = _fix_turn(broken)  # 写坏版本(验证失败)
        good_turn = _fix_turn()  # 写正确版本(验证通过)
        # phase1:写坏 → 失败;phase2(resume):写对 → 通过
        transport = _mock_transport([
            broken_turn, _finish_turn("先改一半"),
            good_turn, _finish_turn("恢复完成"),
        ])
        result = await run_eval_task(
            get_task("recovery-divide"),
            config=_config(),
            repo=demo_repo_work,
            transport=transport,
            max_fix_rounds=2,
        )
        assert result.is_recovery is True
        assert result.recovered is True  # phase2(resume)通过
        assert result.ok is True


# ============ 指标汇总 ============

class TestMetrics:
    def _result(self, *, ok: bool, passed: bool | None = None, out_of_scope: bool = False,
                tokens: int = 0, recovered: bool = False, is_recovery: bool = False,
                has_write_plan: bool = True, is_readonly: bool = False,
                tool_ok: int = 0, tool_err: int = 0) -> EvalResult:
        """构造评测结果。`passed` 默认与 `ok` 一致(两者本就常一致),
        需要构造「闭环自评与独立验收相反」时显式传入。"""
        return EvalResult(
            task_id="t",
            ok=ok,
            passed=ok if passed is None else passed,
            changed_files=["math_utils.py"] if has_write_plan else [],
            out_of_scope=out_of_scope,
            elapsed=1.5,
            tokens=tokens,
            tool_calls=tool_ok + tool_err,
            tool_errors=tool_err,
            recovered=recovered,
            is_recovery=is_recovery,
            has_write_plan=has_write_plan,
            is_readonly=is_readonly,
        )

    def test_summary_success_and_verify_rates(self) -> None:
        results = [
            self._result(ok=True),
            self._result(ok=False),
            self._result(ok=True),
        ]
        s = EvalSummary.from_results(results)
        assert s.total == 3
        assert s.ok == 2
        assert s.success_rate == pytest.approx(2 / 3)
        assert s.verify_rate == pytest.approx(2 / 3)
        assert s.verdict_mismatch == 0  # 两条证据一致

    def test_two_rates_can_disagree(self) -> None:
        """成功率与验证通过率来自**不同测量**,必须能不一致(否则就是同一个布尔写了两遍)。

        这正是拆开两率的意义:闭环说「我改完了、验证通过」,而评测侧独立重跑验收
        命令说「仓库现状跑不过」—— 二者相反才是值得追的信号。
        """
        results = [
            self._result(ok=False, passed=True),  # 闭环自评通过,独立验收不通过
            self._result(ok=True, passed=False),  # 闭环自评不通过,独立验收通过
            self._result(ok=True, passed=True),   # 一致
        ]
        s = EvalSummary.from_results(results)
        assert s.success_rate == pytest.approx(2 / 3)  # 独立验收口径
        assert s.verify_rate == pytest.approx(2 / 3)  # 闭环口径
        assert s.verdict_mismatch == 2  # 但两条证据在 2 个任务上相反


    def test_summary_out_of_scope_and_recovery(self) -> None:
        results = [
            self._result(ok=True, out_of_scope=True),  # 越权
            self._result(ok=True, is_recovery=True, recovered=True),  # 恢复成功
            self._result(ok=False, is_recovery=True, recovered=False),  # 恢复失败
        ]
        s = EvalSummary.from_results(results)
        assert s.out_of_scope == 1
        assert s.out_of_scope_rate == pytest.approx(1 / 3)
        assert s.recovered == 1
        assert s.recovery_total == 2
        assert s.recovery_rate == pytest.approx(0.5)

    def test_summary_token_and_tool_success(self) -> None:
        results = [
            self._result(ok=True, tokens=100, tool_ok=8, tool_err=2),
            self._result(ok=True, tokens=50, tool_ok=0, tool_err=0),
        ]
        s = EvalSummary.from_results(results)
        assert s.total_tokens == 150
        assert s.tool_calls == 10  # (8+2) + 0
        assert s.tool_errors == 2
        assert s.tool_success_rate == pytest.approx(0.8)

    def test_summary_avg_elapsed(self) -> None:
        r1 = self._result(ok=True)
        r1.elapsed = 1.0
        r2 = self._result(ok=True)
        r2.elapsed = 3.0
        s = EvalSummary.from_results([r1, r2])
        assert s.avg_elapsed == pytest.approx(2.0)

    def test_summary_readonly_excluded_from_rates(self) -> None:
        """readonly 任务不计入成功率/验证通过率分母;越权率只算写计划任务。"""
        results = [
            self._result(ok=True),  # 写计划成功
            self._result(ok=False),  # 写计划失败
            self._result(ok=True, is_readonly=True, has_write_plan=False),  # readonly
            self._result(ok=True, is_readonly=True, has_write_plan=False),  # readonly
        ]
        s = EvalSummary.from_results(results)
        assert s.total == 4
        assert s.scored_total == 2  # readonly 排除
        assert s.write_plan_total == 2
        assert s.success_rate == pytest.approx(0.5)  # 1/2
        assert s.verify_rate == pytest.approx(0.5)  # 1/2(readonly 不计)
        assert s.out_of_scope_rate == pytest.approx(0.0)  # 0/2

    def test_summary_out_of_scope_denominator_is_write_plan(self) -> None:
        """越权率分母 = 有写计划的任务(排除 readonly),非总数。"""
        results = [
            self._result(ok=True, out_of_scope=True),  # 越权(写计划)
            self._result(ok=True),  # 写计划正常
            self._result(ok=True, is_readonly=True, has_write_plan=False),  # readonly
        ]
        s = EvalSummary.from_results(results)
        assert s.write_plan_total == 2
        assert s.out_of_scope == 1
        assert s.out_of_scope_rate == pytest.approx(0.5)  # 1/2(而非 1/3)

    def test_no_regression_when_equal(self) -> None:
        r = [self._result(ok=True), self._result(ok=True)]
        s1 = EvalSummary.from_results(r)
        s2 = EvalSummary.from_results(r)
        assert s1.regression(s2) == []

    def test_regression_detects_success_drop(self) -> None:
        base = EvalSummary.from_results([self._result(ok=True), self._result(ok=True)])
        now = EvalSummary.from_results([self._result(ok=True), self._result(ok=False)])
        issues = now.regression(base)
        assert any("成功率" in i for i in issues)

    def test_regression_detects_token_increase(self) -> None:
        base = EvalSummary.from_results([self._result(ok=True, tokens=100)])
        now = EvalSummary.from_results([self._result(ok=True, tokens=200)])
        assert any("Token" in i for i in now.regression(base))
