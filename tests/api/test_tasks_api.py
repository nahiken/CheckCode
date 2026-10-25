"""M7 集成测试:FastAPI 任务创建 + 状态查询 + SSE 事件流。

用 fastapi TestClient 驱动真实路由;后台任务通过注入 fake 配置/事件验证,
不启动真实 LLM(检查 API 挂接与 SSE 协议层)。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api.app import app, manager
from api.tasks import SSE_QUEUE_MAX, TaskHandle, parse_sse_frame, sse_frame


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


class TestTasksApi:
    def test_create_task_returns_id_and_sse_url(self, client: TestClient) -> None:
        """创建任务 → 返回 202 + task_id + sse_url(202=已接受,后台异步执行)。"""
        resp = client.post("/api/tasks", json={"task": "修复 math_utils"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["task_id"]
        assert body["sse_url"] == f"/api/tasks/{body['task_id']}/events"

    def test_create_task_empty_rejected(self, client: TestClient) -> None:
        """空 task → 422(Pydantic 校验失败)。"""
        resp = client.post("/api/tasks", json={"task": "   "})
        assert resp.status_code == 422

    def test_status_unknown_task_404(self, client: TestClient) -> None:
        """不存在的 task_id → 404。"""
        resp = client.get("/api/tasks/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "task not found"

    def test_rollback_unknown_task_404(self, client: TestClient) -> None:
        """回滚不存在的任务 → 404。"""
        resp = client.post("/api/tasks/nonexistent/rollback")
        assert resp.status_code == 404

    def test_status_created_task(self, client: TestClient) -> None:
        """创建后可查询状态。

        默认 auto_approve=False(安全默认),无 API key 时任务在 set_error 前
        不会真的跑起来 —— 此处只断言状态是**已登记的合法态**之一。
        awaiting_approval 是 M9 引入的审批等待态:默认关闭自动放行后它会成为
        写任务的常见落点,故一并列出。
        """
        body = client.post("/api/tasks", json={"task": "x"}).json()
        tid = body["task_id"]
        resp = client.get(f"/api/tasks/{tid}")
        assert resp.json()["status"] in (
            "created",
            "running",
            "done",
            "error",
            "awaiting_approval",
        )

    def test_events_unknown_task_404(self, client: TestClient) -> None:
        """不存在的 task 订阅 SSE → 404(明确错误)。"""
        resp = client.get("/api/tasks/nonexistent/events")
        assert resp.status_code == 404

    def test_task_list_endpoint(self, client: TestClient) -> None:
        """任务列表端点 → 返回数组。"""
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_task_list_includes_request_snapshot(self, client: TestClient) -> None:
        """M9 W1:列表项带创建请求快照(task/strict/auto_approve)与 pending 概要。

        前端据此在 awaiting 行渲染允许/拒绝、在 done/error 行渲染「恢复」按钮。
        """
        created = client.post(
            "/api/tasks", json={"task": "修复", "strict": True, "auto_approve": False}
        ).json()
        tid = created["task_id"]
        item = next(t for t in client.get("/api/tasks").json() if t["task_id"] == tid)
        # 请求快照随任务带出(create 时写入 handle.request)
        assert item["task"] == "修复"
        assert item["pending_approvals"] == []  # 无进行中审批

    def test_rollback_before_done_rejected(self, client: TestClient) -> None:
        """任务未完成回滚 → 明确错误(无基线)。"""
        body = client.post("/api/tasks", json={"task": "x"}).json()
        tid = body["task_id"]
        resp = client.post(f"/api/tasks/{tid}/rollback")
        assert resp.status_code == 200
        assert resp.json()["error"]


class TestSseFrames:
    def test_sse_frame_roundtrip(self) -> None:
        """事件 → SSE 帧 → 解析回原事件。"""
        payload = {"event": "thinking", "data": {"content": "分析中"}}
        frame = sse_frame(payload)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        assert parse_sse_frame(frame) == payload

    def test_parse_ignores_non_data_lines(self) -> None:
        """非 data 行(如注释/空行)→ None(忽略)。"""
        assert parse_sse_frame(": keepalive\n\n") is None
        assert parse_sse_frame("") is None


class TestTaskManager:
    def test_push_bounded_queue_drops_oldest(self) -> None:
        """单订阅者队列满 → 丢弃最旧,保最新。"""
        handle = TaskHandle(task_id="t")
        q = handle.subscribe()
        for i in range(SSE_QUEUE_MAX + 10):
            handle.push({"event": "ev", "data": {"n": i}})
        # 只保最新 SSE_QUEUE_MAX 条
        assert q.qsize() == SSE_QUEUE_MAX

    def test_subscribe_replays_history_and_broadcasts(self) -> None:
        """迟到订阅经 history 回放;每个订阅者队列只收订阅后的广播。"""
        handle = TaskHandle(task_id="t")
        # 订阅前的事件:进 history(端点回放),不产生队列内容
        handle.push({"event": "thinking", "data": {"content": "a"}})
        handle.push({"event": "assistant", "data": {"content": "b"}})
        # 两个订阅者(并发连接):订阅前的历史事件不在各自队列(端点从 history 回放)
        q1 = handle.subscribe()
        q2 = handle.subscribe()
        assert q1.empty() and q2.empty()
        # 订阅后的事件:广播给所有订阅者
        handle.push({"event": "report", "data": {"passed": True}})
        for q in (q1, q2):
            assert q.get_nowait()["event"] == "report"
        # 历史含全部事件(含终止 report,迟到订阅可回放)
        assert [e["event"] for e in handle.history] == ["thinking", "assistant", "report"]

    def test_set_done_pushes_report(self) -> None:
        """任务完成 → push report 事件 + 缓存报告。"""
        manager.create("t1")
        manager.set_done("t1", {"passed": True, "diff": "..."})
        handle = manager.get("t1")
        assert handle is not None
        assert handle.status == "done"
        assert handle.report == {"passed": True, "diff": "..."}
        # 历史末尾是 report 终止事件(迟到订阅回放可得)
        assert handle.history[-1]["event"] == "report"

    def test_set_error_pushes_error(self) -> None:
        manager.create("t2")
        manager.set_error("t2", "boom")
        handle = manager.get("t2")
        assert handle is not None
        assert handle.status == "error"
        assert handle.history[-1]["event"] == "error"
