"""M12 单元测试:run_id 落到每条会话记录。

覆盖:SessionStore 统一补 run_id(与 ts 同一写入点纪律)、显式 run_id 优先(不被覆盖)、
旧文件(无 run_id)仍可解析为 ""、resume 追加的新记录用新 run_id。
"""

from __future__ import annotations

from pathlib import Path

from store.models import HeaderRecord, MessageRecord, PhaseRecord
from store.session import SessionStore


def _header(ts: float | None = None) -> HeaderRecord:
    return HeaderRecord(
        session_id="sess-1", repo="/tmp/r", task="t", branch="sess-1", ts=ts
    )


def test_records_get_run_id_from_store(tmp_path: Path) -> None:
    """未显式给 run_id 的记录 → 落盘时统一补 store.run_id。"""
    store = SessionStore(tmp_path / "s.jsonl", run_id="run-abc")
    store.record_header(_header())
    store.record_message(MessageRecord(role="user", content="hi"))
    store.record_phase(PhaseRecord(phase="done"))

    records = store.records()
    assert [r.run_id for r in records] == ["run-abc"] * 3


def test_explicit_run_id_preserved(tmp_path: Path) -> None:
    """记录自带 run_id(如恢复时复用)→ 不被 store.run_id 覆盖(与 ts 同规则)。"""
    store = SessionStore(tmp_path / "s.jsonl", run_id="run-new")
    store.record_header(_header())
    store._append(MessageRecord(role="user", content="old", run_id="run-old"))

    ids = [r.run_id for r in store.records()]
    assert ids == ["run-new", "run-old"]


def test_default_run_id_is_empty(tmp_path: Path) -> None:
    """不传 run_id(旧调用方)→ 记录 run_id 为空串,行为不回归。"""
    store = SessionStore(tmp_path / "s.jsonl")
    store.record_header(_header())
    assert store.records()[0].run_id == ""


def test_legacy_file_without_run_id_parses(tmp_path: Path) -> None:
    """旧文件(记录无 run_id 字段)仍可解析,run_id 取默认 ""。"""
    path = tmp_path / "legacy.jsonl"
    path.write_text(
        '{"type":"header","session_id":"s","repo":"/r","task":"t","branch":"b","ts":1.0}\n',
        encoding="utf-8",
    )
    rec = SessionStore(path).records()[0]
    assert rec.run_id == ""


def test_resume_appends_with_new_run_id(tmp_path: Path) -> None:
    """resume:同一会话文件,第二次运行用新的 run_id 追加(区分两次跑)。"""
    path = tmp_path / "s.jsonl"
    SessionStore(path, run_id="run-1").record_phase(PhaseRecord(phase="run"))
    SessionStore(path, run_id="run-2").record_phase(PhaseRecord(phase="run"))

    run_ids = [r.run_id for r in SessionStore(path).records()]
    assert run_ids == ["run-1", "run-2"]


def test_run_id_does_not_break_discriminated_union(tmp_path: Path) -> None:
    """run_id 只加在共享基类 → 判别式 union 按 type 分发不被打破。"""
    store = SessionStore(tmp_path / "s.jsonl", run_id="run-x")
    store.record_header(_header())
    store.record_message(MessageRecord(role="assistant", content="ok"))
    store.record_phase(PhaseRecord(phase="done", data={"passed": True}))

    records, skipped = store.read_with_skips()
    assert skipped == 0
    assert [type(r).__name__ for r in records] == ["HeaderRecord", "MessageRecord", "PhaseRecord"]
