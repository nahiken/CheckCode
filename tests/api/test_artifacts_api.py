"""M12 集成测试:artifact 端点(/report、/diff、/trace)。

用 TestClient 驱动真实路由;直接构造 TaskHandle 注入 report/session_file,
不跑真实闭环(专注端点契约:200/404/409 矩阵、content-type、格式)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import app, manager
from store.models import HeaderRecord, MessageRecord, PhaseRecord
from store.session import SessionStore


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _seed_report(task_id: str, *, diff: str = "--- a\n+++ b\n") -> None:
    h = manager.create(task_id)
    h.status = "done"
    h.report = {"diff": diff, "changed_files": ["m.py"], "passed": True}


def _seed_session(task_id: str, path: Path, run_id: str = "run-x") -> None:
    store = SessionStore(path, run_id=run_id)
    store.record_header(
        HeaderRecord(session_id="sess-t", repo="/r", task="t", branch="b")
    )
    store.record_message(MessageRecord(role="user", content="hi"))
    store.record_phase(PhaseRecord(phase="done", data={"passed": True}))
    manager.get(task_id).session_file = str(path)  # type: ignore[union-attr]


class TestReportEndpoint:
    def test_report_ok(self, client: TestClient) -> None:
        _seed_report("t-report")
        resp = client.get("/api/tasks/t-report/report")
        assert resp.status_code == 200
        assert resp.json()["changed_files"] == ["m.py"]

    def test_report_unknown_404(self, client: TestClient) -> None:
        assert client.get("/api/tasks/nope/report").status_code == 404

    def test_report_not_done_409(self, client: TestClient) -> None:
        manager.create("t-run")
        resp = client.get("/api/tasks/t-run/report")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        # 结构化 detail:客户端不必解析中文文案即可区分「进行中」与「已失败」
        assert detail["status"] == "created"
        assert detail["error"] is None

    def test_report_error_task_409_distinguishable(self, client: TestClient) -> None:
        """终态 error 的任务:409 但 detail 带 status=error + 错误信息,可区分于运行中。"""
        h = manager.create("t-err")
        h.status = "error"
        h.error = "boom"
        detail = client.get("/api/tasks/t-err/report").json()["detail"]
        assert detail["status"] == "error"
        assert detail["error"] == "boom"


class TestDiffEndpoint:
    def test_diff_ok(self, client: TestClient) -> None:
        _seed_report("t-diff", diff="diff --git a/x b/x\n")
        body = client.get("/api/tasks/t-diff/diff").json()
        assert body["diff"] == "diff --git a/x b/x\n"
        assert body["changed_files"] == ["m.py"]

    def test_diff_unknown_404(self, client: TestClient) -> None:
        assert client.get("/api/tasks/nope/diff").status_code == 404

    def test_diff_not_done_409(self, client: TestClient) -> None:
        manager.create("t-diff-run")
        assert client.get("/api/tasks/t-diff-run/diff").status_code == 409


class TestTraceEndpoint:
    def test_trace_json_ok(self, client: TestClient, tmp_path: Path) -> None:
        _seed_report("t-trace")
        _seed_session("t-trace", tmp_path / "s.jsonl")
        body = client.get("/api/tasks/t-trace/trace").json()
        assert body["count"] == len(body["entries"])
        assert body["summary"]["session_id"] == "sess-t"
        assert body["summary"]["run_id"] == "run-x"

    def test_trace_jsonl_content_type(self, client: TestClient, tmp_path: Path) -> None:
        _seed_report("t-trace2")
        _seed_session("t-trace2", tmp_path / "s2.jsonl")
        resp = client.get("/api/tasks/t-trace2/trace?format=jsonl")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        for line in resp.text.splitlines():
            if line:
                assert "kind" in json.loads(line)

    def test_trace_unknown_404(self, client: TestClient) -> None:
        assert client.get("/api/tasks/nope/trace").status_code == 404

    def test_trace_missing_session_file_empty(self, client: TestClient) -> None:
        """任务存在但会话还没落盘 → 空结果而非 404(运行中读 Trace 是正常场景)。"""
        manager.create("t-empty")
        body = client.get("/api/tasks/t-empty/trace").json()
        assert body["entries"] == []
        assert body["summary"]["total_tool_calls"] == 0

    def test_trace_invalid_format_422(self, client: TestClient) -> None:
        """非法 format 被 Query(pattern) 拒绝 → 422,而非静默按 JSON 返回。"""
        manager.create("t-fmt")
        assert client.get("/api/tasks/t-fmt/trace?format=xml").status_code == 422

    def test_trace_run_id_filter(self, client: TestClient, tmp_path: Path) -> None:
        _seed_report("t-rid")
        _seed_session("t-rid", tmp_path / "s3.jsonl", run_id="run-a")
        body = client.get("/api/tasks/t-rid/trace?run_id=run-a").json()
        assert body["summary"]["run_id"] == "run-a"


class TestSessionFileContract:
    """契约:handle.session_file 必须指向**传给 loop 的那个 session**。

    两处都用 _new_session_id(task_id),但没有任何测试钉死;今后若 loop 侧换前缀,
    artifact 端点会静默指向错文件(表现为空 200,不报错)。
    """

    def test_session_file_matches_loop_session_id(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_repo: Path
    ) -> None:
        import asyncio
        import importlib

        from agent.config import LLMConfig
        from api.app import _TaskRequest
        from verify.report import RiskReport

        # api/__init__.py 把父包同名属性设成 FastAPI 实例 → `import api.app` 会绑到实例,
        # 必须用 importlib 拿真模块(仓库既有的命名陷阱)。
        api_app = importlib.import_module("api.app")
        seen: dict[str, str] = {}

        class _FakeLoop:
            def __init__(self, config: object, **kw: object) -> None:
                seen["session_id"] = str(kw["session_id"])

            async def run(self, repo: Path, task: str, events: object = None) -> RiskReport:
                return RiskReport(
                    repo=repo,
                    base_commit="abc",
                    branch=seen["session_id"],
                    changed_files=[],
                    test_command="echo",
                    remaining_risk="低",
                )

        monkeypatch.setattr(api_app, "VerifiedLoop", _FakeLoop)
        tid = "t-contract"
        manager.create(tid)

        async def _drive() -> None:
            await api_app._run_task(
                tid,
                _TaskRequest(task="x", repo=str(demo_repo)),
                LLMConfig(base_url="http://t", api_key="k", model="m"),
            )

        asyncio.run(_drive())
        handle = manager.get(tid)
        assert handle is not None and handle.session_file is not None
        assert handle.session_file.endswith(f"{seen['session_id']}.jsonl")
