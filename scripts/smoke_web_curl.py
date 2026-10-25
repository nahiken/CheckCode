"""Web 端 curl 冒烟:docker compose 起栈后验证 创建/SSE/审批/resume(M9 W8)。

⚠ 仅当 WHYCODE_SMOKE_REAL=1 且 Docker 栈已起(docker compose up --build)时执行。

覆盖(经真实 HTTP,无浏览器):
1. GET /api/tasks → 200 列表。
2. POST /api/tasks(空/非法)→ 422;合法 → 202 + task_id + sse_url。
3. GET {id}/events → SSE 首帧可达(report/error 终止帧最终到达)。
4. 审批与恢复端点可达(200/404 语义)。
5. 未知任务 → 404。

用法:
    docker compose up --build -d
    WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_web_curl.py [--base http://127.0.0.1:8000]

退出码:0 = 通过;1 = 任一检查失败。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

# 守卫:未显式开启真实运行 → 直接跳过(CI/误触不执行)
if os.environ.get("WHYCODE_SMOKE_REAL") != "1":
    print("跳过:Web 冒烟需显式 WHYCODE_SMOKE_REAL=1(且 Docker 栈已起)。")
    sys.exit(0)


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API 基址")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    client = httpx.Client(base_url=base, timeout=10.0)

    # 1. 任务列表
    r = client.get("/api/tasks")
    _check("GET /api/tasks → 200", r.status_code == 200)

    # 2. 非法任务 → 422
    r = client.post("/api/tasks", json={"task": "   "})
    _check("POST 空任务 → 422", r.status_code == 422)

    # 3. 合法创建 → 202 + task_id
    r = client.post(
        "/api/tasks",
        json={"task": "读一下仓库里的文件", "strict": True, "auto_approve": True},
    )
    _check("POST 合法任务 → 202", r.status_code == 202)
    body = r.json()
    tid = body.get("task_id")
    _check("响应含 task_id + sse_url", bool(tid) and bool(body.get("sse_url")), f"tid={tid}")

    # 4. SSE 首帧可达(不依赖真实 LLM:无 key 时后台很快 error;有 key 走真实)
    #    轮询 events,取前几字节(首帧应到达,含 error 或正常事件)
    sse_ok = False
    headers = {"Accept": "text/event-stream"}
    with client.stream("GET", f"/api/tasks/{tid}/events", headers=headers) as resp:
        if resp.status_code == 200:
            # 流式读第一帧(最多 8s)
            got = b""
            deadline = time.monotonic() + 8
            for chunk in resp.iter_bytes():
                got += chunk
                if b"\n\n" in got:
                    break
                if time.monotonic() > deadline:
                    break
            sse_ok = got.startswith(b"data: ")
    detail = got[:80].decode(errors="replace") if not sse_ok else ""
    _check("SSE events 首帧 data: 帧可达", sse_ok, detail)

    # 5. 审批端点语义:不存在任务的审批 → 404
    r = client.post(f"/api/tasks/{tid}/approvals/ap-nope", json={"approve": True})
    _check("未知审批 → 404", r.status_code == 404)

    # 6. resume:任务若已到 error(无 key)应可恢复;running/done 拒绝不崩溃
    st = client.get(f"/api/tasks/{tid}").json()
    if st.get("status") in ("error", "rolled_back"):
        r = client.post(f"/api/tasks/{tid}/resume")
        _check("error 任务 resume → 200", r.status_code == 200, str(r.json()))
    else:
        r = client.post(f"/api/tasks/{tid}/resume")
        _check("运行中 resume → 200(拒绝信息)", r.status_code == 200)
    valid = ("created", "running", "done", "error")
    _check("任务状态端点可达", st.get("status") in valid, str(st.get("status")))

    print("\n✓ Web curl 冒烟通过(创建/422/SSE/审批/resume 语义)。")


if __name__ == "__main__":
    main()
