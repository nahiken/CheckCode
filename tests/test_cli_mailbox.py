"""M14 CLI 测试:`mewcode send` / `mewcode inbox`(共享邮箱的投递与查看)。

这两个子命令是「跨进程」的操作面:一个终端 `send`,另一个终端的 `worker`
消费,第三个终端 `inbox` 查看。因此测试重点在**进程边界**上:
- send 落盘的快照,能被另一个进程(这里用子进程跑 CLI)读到;
- inbox 只读、不改状态(查完消息仍可被 worker 领取);
- 幂等键在跨进程下依然生效。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "cli.main", *args],
        capture_output=True,
        text=False,
        check=False,
    )


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


class TestSendCommand:
    def test_send_directed_message(self, tmp_path: Path) -> None:
        """send 一条定向消息 → 快照文件里带 kind/sender/recipient/corr_id。"""
        snap = tmp_path / "mailbox.json"
        r = run_cli(
            "send", "--queue", str(snap), "--id", "m-1",
            "--from", "alice", "--to", "bob", "--text", "帮我看看",
        )
        assert r.returncode == 0, _text(r.stderr)
        assert "m-1" in _text(r.stdout)

        # 另一个进程(本测试)直接读快照 → 跨进程可见性
        data = json.loads(snap.read_text(encoding="utf-8"))
        assert data["m-1"]["kind"] == "message"
        assert data["m-1"]["sender"] == "alice"
        assert data["m-1"]["recipient"] == "bob"
        assert data["m-1"]["corr_id"] == "m-1"
        assert data["m-1"]["status"] == "pending"

    def test_send_broadcast_when_no_recipient(self, tmp_path: Path) -> None:
        """省略 --to → 广播(recipient 为 null,任一 agent 可领)。"""
        snap = tmp_path / "mailbox.json"
        r = run_cli(
            "send", "--queue", str(snap), "--id", "m-b",
            "--from", "alice", "--text", "通知",
        )
        assert r.returncode == 0, _text(r.stderr)
        data = json.loads(snap.read_text(encoding="utf-8"))
        assert data["m-b"]["recipient"] is None

    def test_send_as_task(self, tmp_path: Path) -> None:
        """--task → 进任务车道(kind=task),给 worker 消费而非 inbox。"""
        snap = tmp_path / "mailbox.json"
        r = run_cli(
            "send", "--queue", str(snap), "--id", "t-1",
            "--from", "alice", "--text", "修复 bug", "--task",
        )
        assert r.returncode == 0, _text(r.stderr)
        data = json.loads(snap.read_text(encoding="utf-8"))
        assert data["t-1"]["kind"] == "task"
        assert data["t-1"]["payload"] == {"task": "修复 bug"}

    def test_send_is_idempotent_across_processes(self, tmp_path: Path) -> None:
        """同 --id 投两次(两次独立进程)→ 只有一条记录,内容保持首次的。"""
        snap = tmp_path / "mailbox.json"
        args = ("send", "--queue", str(snap), "--id", "m-1", "--from", "alice",
                "--to", "bob", "--text", "原始")
        assert run_cli(*args).returncode == 0
        assert run_cli(*args[:-1], "改过").returncode == 0

        data = json.loads(snap.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data["m-1"]["payload"]["text"] == "原始"

    def test_send_requires_id_and_text(self, tmp_path: Path) -> None:
        r = run_cli("send", "--queue", str(tmp_path / "m.json"), "--from", "a")
        assert r.returncode == 2
        assert "--id" in _text(r.stderr)

    def test_empty_to_is_normalized_to_broadcast(self, tmp_path: Path) -> None:
        """`--to ""` 归一为广播(审计轻-2)。

        空串不是 agent id。若原样存下,这条消息既非广播、也不匹配任何 agent,
        而且它是 PENDING(`recover_deliveries` 只处理 CLAIMED)→ **永远投不出去**
        的黑洞:任何人的 `claim_inbox` 都看不见它。
        """
        snap = tmp_path / "mailbox.json"
        r = run_cli("send", "--queue", str(snap), "--id", "m-e",
                    "--from", "alice", "--to", "", "--text", "orphan?")
        assert r.returncode == 0, _text(r.stderr)
        data = json.loads(snap.read_text(encoding="utf-8"))
        assert data["m-e"]["recipient"] is None  # 归一为广播,而非 ""
        assert "广播" in _text(r.stdout)

    def test_task_with_to_is_rejected(self, tmp_path: Path) -> None:
        """`--task` + `--to` 同用 → 报错(审计轻-3)。

        原实现静默忽略 `--to` 却回显「广播」——任务根本不是广播,且 `--from`
        也被丢弃,回显纯属误导。改为显式拒绝,让用户立刻知道参数用错了。
        """
        snap = tmp_path / "mailbox.json"
        r = run_cli("send", "--queue", str(snap), "--id", "t-1", "--from", "a",
                    "--to", "bob", "--text", "x", "--task")
        assert r.returncode == 2
        assert "--task" in _text(r.stdout) + _text(r.stderr)
        assert not snap.exists()  # 什么都没入队

    def test_task_echo_says_task_not_broadcast(self, tmp_path: Path) -> None:
        """`--task` 的回显必须是「任务」而非「广播」。"""
        snap = tmp_path / "mailbox.json"
        r = run_cli("send", "--queue", str(snap), "--id", "t-1", "--from", "a",
                    "--text", "干活", "--task")
        assert r.returncode == 0
        out = _text(r.stdout)
        assert "任务" in out
        assert "广播" not in out

    def test_send_across_lanes_same_id_reports_error(self, tmp_path: Path) -> None:
        """同 id 跨车道投递 → 明确报错(而不是静默 no-op 却打印成功)。"""
        snap = tmp_path / "mailbox.json"
        assert run_cli("send", "--queue", str(snap), "--id", "X", "--from", "a",
                       "--text", "msg").returncode == 0
        r = run_cli("send", "--queue", str(snap), "--id", "X", "--from", "a",
                    "--text", "干活", "--task")
        assert r.returncode == 1
        assert "另一条车道" in _text(r.stdout) + _text(r.stderr)


class TestInboxCommand:
    def _prepare(self, tmp_path: Path) -> Path:
        snap = tmp_path / "mailbox.json"
        # alice 给 bob 定向一条;再广播一条;再给 carol 定向一条
        assert run_cli("send", "--queue", str(snap), "--id", "m-to-bob", "--from", "alice",
                       "--to", "bob", "--text", "给 bob").returncode == 0
        assert run_cli("send", "--queue", str(snap), "--id", "m-cast", "--from", "alice",
                       "--text", "广播").returncode == 0
        assert run_cli("send", "--queue", str(snap), "--id", "m-to-carol", "--from", "alice",
                       "--to", "carol", "--text", "给 carol").returncode == 0
        return snap

    def test_inbox_filters_by_recipient(self, tmp_path: Path) -> None:
        """bob 的收件箱 = 定向给 bob 的 + 广播;**不含**给 carol 的。"""
        snap = self._prepare(tmp_path)
        r = run_cli("inbox", "--queue", str(snap), "--agent", "bob", "--json")
        assert r.returncode == 0, _text(r.stderr)
        ids = {rec["task_id"] for rec in json.loads(_text(r.stdout))}
        assert ids == {"m-to-bob", "m-cast"}

    def test_inbox_is_read_only(self, tmp_path: Path) -> None:
        """inbox 只读:查完消息仍是 pending,worker 照样能领到。"""
        snap = self._prepare(tmp_path)
        assert run_cli("inbox", "--queue", str(snap), "--agent", "bob").returncode == 0

        data = json.loads(snap.read_text(encoding="utf-8"))
        assert data["m-to-bob"]["status"] == "pending"
        assert data["m-to-bob"]["claimed_by"] is None

    def test_inbox_for_unknown_agent_sees_only_broadcasts(self, tmp_path: Path) -> None:
        """陌生 agent 只看到广播;别人的定向消息对它不可见(不泄露、也不误领)。"""
        snap = self._prepare(tmp_path)
        r = run_cli("inbox", "--queue", str(snap), "--agent", "nobody", "--json")
        assert r.returncode == 0
        ids = {rec["task_id"] for rec in json.loads(_text(r.stdout))}
        assert ids == {"m-cast"}

    def test_inbox_empty_when_no_broadcast_and_nothing_directed(
        self, tmp_path: Path
    ) -> None:
        """只有定向消息时,非收件人的收件箱为空。"""
        snap = tmp_path / "mailbox.json"
        assert run_cli("send", "--queue", str(snap), "--id", "m-b", "--from", "alice",
                       "--to", "bob", "--text", "只给 bob").returncode == 0
        r = run_cli("inbox", "--queue", str(snap), "--agent", "carol", "--json")
        assert r.returncode == 0
        assert json.loads(_text(r.stdout)) == []

    def test_inbox_ignores_tasks(self, tmp_path: Path) -> None:
        """收件箱不显示任务(任务属于 worker 车道)。"""
        snap = tmp_path / "mailbox.json"
        assert run_cli("send", "--queue", str(snap), "--id", "t-1", "--from", "a",
                       "--text", "干活", "--task").returncode == 0
        r = run_cli("inbox", "--queue", str(snap), "--agent", "bob", "--json")
        assert json.loads(_text(r.stdout)) == []

    def test_inbox_requires_agent(self, tmp_path: Path) -> None:
        r = run_cli("inbox", "--queue", str(tmp_path / "m.json"))
        assert r.returncode == 2
        assert "--agent" in _text(r.stderr)

    def test_inbox_for_empty_agent_sees_broadcasts(self, tmp_path: Path) -> None:
        """`--agent ""` 是边界输入:不崩、只看到广播(空串不是任何定向消息的收件人)。"""
        snap = tmp_path / "mailbox.json"
        assert run_cli("send", "--queue", str(snap), "--id", "m-cast", "--from", "a",
                       "--text", "广播").returncode == 0
        assert run_cli("send", "--queue", str(snap), "--id", "m-b", "--from", "a",
                       "--to", "bob", "--text", "只给 bob").returncode == 0
        r = run_cli("inbox", "--queue", str(snap), "--agent", "", "--json")
        assert r.returncode == 0
        ids = {rec["task_id"] for rec in json.loads(_text(r.stdout))}
        assert ids == {"m-cast"}
