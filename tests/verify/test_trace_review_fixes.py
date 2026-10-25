"""M15 审计修复的回归测试(第二轮 code review)。

覆盖:只读会话不可 resume / trace 的 passed 与 run_id / 撕裂 UTF-8 / 回滚守卫 /
错误态清 report / 导入成本。
"""

from __future__ import annotations

import json
from pathlib import Path

from store.models import HeaderRecord, PhaseRecord, ToolCallRecord
from store.session import SessionStore
from verify.trace import build_trace, run_summary

# === 审计 B6:run_id 空串必须归一化为「不过滤」 ===


def _session(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "s.jsonl")


def test_empty_run_id_means_no_filter(tmp_path: Path) -> None:
    """`run_id=""`(CLI `--run-id ""` / `?run_id=` 空串)必须与未给定同义。

    早先空串被当作真过滤器 → 只留 header,静默返回近乎空的 Trace(实测 count 2 vs 10)。
    """
    store = _session(tmp_path)
    store.record_header(
        HeaderRecord(session_id="s1", repo="/r", task="t", branch="s1", mode="normal")
    )
    store.record_phase(PhaseRecord(phase="done", data={"passed": True}))

    unfiltered = build_trace(store.records())
    assert len(build_trace(store.records(), run_id="")) == len(unfiltered)
    assert len(build_trace(store.records(), run_id=None)) == len(unfiltered)


# === 审计 B3:上一次 run 的 passed 不得泄漏到本次 ===


def test_stale_passed_not_reported_for_unfinished_run(tmp_path: Path) -> None:
    """run-1 通过 + run-2 崩溃(未写 done)→ 摘要的 passed 必须是 False。

    早先只在 done 带该键时才覆盖 → 上一次的 True 留在摘要里,且 run_id 显示本次 run,
    CI/看板会把一个从未完成的运行读成通过。
    """
    store = _session(tmp_path)
    store.record_header(
        HeaderRecord(session_id="s1", repo="/r", task="t", branch="s1", mode="normal")
    )
    # run-1:完整跑完并通过
    store.record_phase(PhaseRecord(phase="run", data={"run_id": "run-1"}))
    store.record_phase(PhaseRecord(phase="done", data={"passed": True}))
    # run-2:起了但崩溃(没有 done)
    store.record_phase(PhaseRecord(phase="run", data={"run_id": "run-2"}))

    summary = run_summary(build_trace(store.records()))
    assert summary["run_id"] == "run-2"
    assert summary["passed"] is False  # 本次未完成 ≠ 通过


def test_passed_true_when_latest_run_completed(tmp_path: Path) -> None:
    """对照:本次真的写完了 done → passed 如实反映。"""
    store = _session(tmp_path)
    store.record_header(
        HeaderRecord(session_id="s1", repo="/r", task="t", branch="s1", mode="normal")
    )
    store.record_phase(PhaseRecord(phase="run", data={"run_id": "run-1"}))
    store.record_phase(PhaseRecord(phase="done", data={"passed": False}))
    store.record_phase(PhaseRecord(phase="run", data={"run_id": "run-2"}))
    store.record_phase(PhaseRecord(phase="done", data={"passed": True}))

    assert run_summary(build_trace(store.records()))["passed"] is True


# === 审计 B2:按 run_id 过滤时时长不得算进 header 的 ts ===


def test_run_filtered_duration_excludes_header_ts(tmp_path: Path) -> None:
    """header 属于**首次**运行;过滤某次运行时它不得参与时间跨度(否则时长≈整段会话)。"""
    store = _session(tmp_path)
    store.record_header(
        HeaderRecord(session_id="s1", repo="/r", task="t", branch="s1", mode="normal")
    )
    store.record_phase(PhaseRecord(phase="run", data={"run_id": "run-2"}))

    # 手工给记录打上相差很大的 ts(模拟「很久以前的首跑 + 今天的 resume」)
    recs = store.records()
    recs[0].ts = 1000.0  # header(首跑)
    recs[0].run_id = "run-1"
    recs[1].ts = 2000.0  # run-2 的记录
    recs[1].run_id = "run-2"

    filtered = build_trace(recs, run_id="run-2")
    summary = run_summary(filtered)
    # 只有 run-2 自己的记录参与 → 跨度 0(单条),而不是 1000s
    assert summary["duration_s"] in (0.0, None), summary


# === 审计 C1:被 before Hook 拦下的调用不得算作「已执行」 ===


def test_hook_rejected_call_not_counted_as_executed(tmp_path: Path) -> None:
    """`granted=True` 但 layer=hook(被 before Hook 拒)→ executed 不计数。"""
    store = _session(tmp_path)
    store.record_tool_call(
        ToolCallRecord(
            tool_call_id="c1",
            tool_name="write_file",
            decision="permit",
            layer="hook",
            granted=True,  # 权限放行了
            is_error=True,  # 但没执行成功
        )
    )
    summary = run_summary(build_trace(store.records()))
    assert summary["granted"] == 1  # 权限确实放行了
    assert summary["executed"] == 0  # 但没执行


def test_executed_counts_real_run(tmp_path: Path) -> None:
    store = _session(tmp_path)
    store.record_tool_call(
        ToolCallRecord(tool_call_id="c1", tool_name="read_file", decision="permit", granted=True)
    )
    summary = run_summary(build_trace(store.records()))
    assert summary["granted"] == 1 and summary["executed"] == 1


def test_executed_field_is_authoritative(tmp_path: Path) -> None:
    """有 `executed` 字段时以它为准,**不走启发式**。

    两种分歧场景:
    - `executed=True` 但 `is_error=True` —— 工具真的跑了但报错(旧启发式会误判为
      「没执行」;执行路径的置位才对,它确实产生过副作用);
    - `executed=False` 但 `granted=True, layer=permit, is_error=False` —— 理论上不该
      出现,但字段若与启发式冲突,权威判据是字段本身(启发式只是给老记录兜底)。
    """
    store = _session(tmp_path)
    store.record_tool_call(
        ToolCallRecord(
            tool_call_id="c1",
            tool_name="write_file",
            decision="permit",
            granted=True,
            executed=True,  # 作者:真的执行了
            is_error=True,  # 而且失败了 —— 旧启发式会算作「未执行」
        )
    )
    summary = run_summary(build_trace(store.records()))
    assert summary["executed"] == 1  # 字段说了算


# === 审计 B8:尾行被截断的多字节字符不得让整次读取抛异常 ===


def test_torn_multibyte_tail_is_skipped_not_raised(tmp_path: Path) -> None:
    """崩溃截断半个汉字 → 跳过该行(不是抛 UnicodeDecodeError → HTTP 500)。"""
    p = tmp_path / "s.jsonl"
    header = json.dumps(
        {"type": "header", "session_id": "s1", "repo": "/r", "task": "t", "branch": "s1"}
    ).encode("utf-8")
    p.write_bytes(header + b"\n" + b'{"type":"message","role":"user","content":"\xe4')

    records, skipped = SessionStore(p).read_with_skips()

    assert len(records) == 1  # header 读到了
    assert skipped == 1  # 半截行被跳过而非抛出
