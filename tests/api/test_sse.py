"""M7 单元测试:SSE 事件流端点(真实 generator 驱动)。

不启动后台任务:直接构造 TaskHandle 并向队列注入事件,驱动 /events 端点
验证:顺序、report 终止、错误处理、心跳。这是「SSE 端点真正工作」的证据。
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import app, manager
from api.tasks import parse_sse_frame, sse_frame


def _parsed_events(resp: Any) -> list[dict[str, Any]]:
    """解析 SSE 响应为事件列表(跳过 None 与空帧)。"""
    out: list[dict[str, Any]] = []
    for f in resp.text.split("\n\n"):
        if not f.strip():
            continue
        parsed = parse_sse_frame(f)
        if parsed is not None:
            out.append(parsed)
    return out


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


class TestSseEndpoint:
    def test_stream_events_in_order_until_report(self, client: TestClient) -> None:
        """事件按顺序推送,report 终止事件后流结束。"""
        handle = manager.create("sse-1")
        handle.push({"event": "thinking", "data": {"content": "分析"}})
        handle.push({"event": "assistant", "data": {"content": "我来修"}})
        manager.set_done("sse-1", {"passed": True, "diff": "+x"})

        resp = client.get("/api/tasks/sse-1/events")
        events = _parsed_events(resp)

        kinds = [e["event"] for e in events]
        assert kinds == ["thinking", "assistant", "report"]
        # report 负载含 diff
        assert events[-1]["data"]["diff"] == "+x"
        assert events[-1]["data"]["passed"] is True

    def test_stream_report_with_path_serializes(self, client: TestClient) -> None:
        """report 负载含 WindowsPath(通过 _report_to_dict 转 str)不崩。

        直接测 _report_to_dict:repo 是 Path → model_dump 留 WindowsPath,
        jsonable_encoder 转 str → SSE 帧可序列化。
        """
        from pathlib import Path

        from api.app import _report_to_dict
        from verify.report import RiskReport
        from verify.runner import TestResult

        report = RiskReport(
            repo=Path("."),
            base_commit="abc",
            branch="sess-x",
            changed_files=["math_utils.py"],
            test_command="pytest",
            rounds=[
                TestResult(
                    command="pytest",
                    passed=True,
                    exit_code=0,
                    output="ok",
                    duration_ms=10,
                )
            ],
            rounds_used=1,
            remaining_risk="低",
            diff="+x",
        )
        d = _report_to_dict(report)
        assert isinstance(d["repo"], str)  # Path → str
        assert d["passed"] is True  # property 显式并入
        # 可序列化为 SSE 帧(不抛 TypeError)
        sse_frame({"event": "report", "data": d})

    def test_error_event_terminates_stream(self, client: TestClient) -> None:
        """error 事件到达 → 流终止(不悬挂)。"""
        manager.create("sse-2")
        manager.set_error("sse-2", "boom")
        resp = client.get("/api/tasks/sse-2/events")
        events = _parsed_events(resp)
        assert events[-1]["event"] == "error"

    def test_rollback_event_terminates_stream(self, client: TestClient) -> None:
        """rollback 事件到达 → 流终止(活跃订阅者实时收到)。"""
        handle = manager.create("sse-3")
        handle.subscribe()  # 活跃订阅者(created,历史为空 → 走实时路径)
        manager.set_rollback("sse-3", "已回滚到 abc")
        resp = client.get("/api/tasks/sse-3/events")
        events = _parsed_events(resp)
        assert events[-1]["event"] == "rollback"
        assert events[-1]["data"]["message"] == "已回滚到 abc"

    def test_approval_event_not_terminal_stream_continues(self, client: TestClient) -> None:
        """M9 W1:approval 事件非终止——SSE 流在审批间保持打开,report 才结束。

        语义:浏览器在审批面板点允许/拒绝的往返不关闭流(awaiting 只是挂起,不终止)。
        """
        handle = manager.create("sse-ap")
        handle.push({"event": "approval", "data": {"approval_id": "ap-1"}})
        manager.set_done("sse-ap", {"passed": True})

        events = _parsed_events(client.get("/api/tasks/sse-ap/events"))
        assert [e["event"] for e in events] == ["approval", "report"]
        # approval 负载原样带出(approval_id 供审批端点 resolve)
        assert events[0]["data"]["approval_id"] == "ap-1"

    def test_resume_event_then_report(self, client: TestClient) -> None:
        """M9 W1:resume 任务在 SSE 里推 resume 标记事件,随后正常 report 终止。"""
        handle = manager.create("sse-rs")
        handle.push({"event": "resume", "data": {"branch": "sess-rs"}})
        manager.set_done("sse-rs", {"passed": True})

        events = _parsed_events(client.get("/api/tasks/sse-rs/events"))
        assert [e["event"] for e in events] == ["resume", "report"]

    def test_report_frame_json_roundtrip(self) -> None:
        """sse_frame + parse_sse_frame 往返(含中文)。"""
        payload = {"event": "verify", "data": {"round": 1, "passed": True}}
        assert parse_sse_frame(sse_frame(payload)) == payload
