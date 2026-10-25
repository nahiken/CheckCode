"""M6 单元测试:MailboxStore 原子快照持久化(纯存储层,零队列逻辑)。

原子快照:save 写临时文件 + os.replace,崩溃安全(要么旧要么新)。
load 一次 JSON 读出完整状态(一致性视图)——与 M4 的 JSONL 事件日志语义不同。
"""

from __future__ import annotations

import json
from mailbox.store import MailboxStore
from pathlib import Path

import pytest


def _one_record() -> dict[str, object]:
    return {
        "task_id": "t-1",
        "status": "claimed",
        "payload": {"cmd": "write_file", "args": {"path": "a.py"}},
        "claimed_by": "w-1",
        "attempts": 1,
    }


class TestStoreSaveLoad:
    def test_save_then_load_roundtrips(self, tmp_path: Path) -> None:
        """保存后读回:字段完整、状态一致。"""
        store = MailboxStore(tmp_path / "mailbox.json")
        store.save({"t-1": _one_record()})

        data = store.load()
        assert data["t-1"]["task_id"] == "t-1"
        assert data["t-1"]["status"] == "claimed"
        assert data["t-1"]["claimed_by"] == "w-1"

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """文件不存在 → 空 dict(新建队列的起点)。"""
        store = MailboxStore(tmp_path / "nope.json")
        assert store.load() == {}

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        """父目录不存在 → 自动创建。"""
        store = MailboxStore(tmp_path / "a" / "b" / "mailbox.json")
        store.save({"t-1": _one_record()})
        assert store.load()["t-1"]["task_id"] == "t-1"

    def test_overwrite_replaces_state(self, tmp_path: Path) -> None:
        """覆盖保存两次 → 最新状态(不残留旧记录)。"""
        path = tmp_path / "mailbox.json"
        store = MailboxStore(path)
        store.save({"t-1": _one_record()})
        store.save({})  # 清空
        assert store.load() == {}

    def test_corrupt_json_raises_value_error(self, tmp_path: Path) -> None:
        """损坏 JSON → 抛 ValueError(清晰 message,不静默)。"""
        path = tmp_path / "mailbox.json"
        path.write_text("{broken", encoding="utf-8")
        store = MailboxStore(path)
        with pytest.raises(ValueError):
            store.load()

    def test_atomic_write_leaves_no_tmp(self, tmp_path: Path) -> None:
        """原子写:保存后无 .tmp 残留文件。"""
        path = tmp_path / "mailbox.json"
        store = MailboxStore(path)
        store.save({"t-1": _one_record()})
        leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        """落盘内容本身是合法 JSON(可直接被其他工具读取)。"""
        path = tmp_path / "mailbox.json"
        store = MailboxStore(path)
        store.save({"t-1": _one_record()})
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["t-1"]["status"] == "claimed"
