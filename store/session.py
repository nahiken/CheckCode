"""store/session.py —— 会话 JSONL 存储 + 恢复推导。

SessionStore:追加写一条记录 = 一行 JSON(崩溃安全:最多丢最后一行,历史不坏)。
read_with_skips 容忍损坏行;continue_session 复用既有文件(进程重启后继续追加)。

目录**延迟创建**:构造/读取不落盘,只在首次真正追加时才 mkdir(M10 修复)——
计划审批门要在「任何写入之前」提前返回,拒绝路径必须零 session 文件(连空目录都不建)。

恢复推导(recover_events)——M4 核心语义「不重放已完成副作用操作」:
把持久化事件流重建成「该从哪条继续」。判定器是 store/effect.py 的 is_read_only(唯一来源)。
- 已完成(写)的完整事务 → 干净重放(作为模型历史,不重新执行)。
- pending 读(读已执行但结果未回灌)→ 用落盘结果喂回模型(只读,重放安全)。
- pending 写(写已落盘但结果未回灌)→ **跳过**,回灌合成「已跳过副作用」结果。
- overridden=True 的写 → 对应 assistant tool_calls 从模型上下文排除(防重复回灌)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent.types import AgentMessage
from store.effect import is_read_only
from store.models import (
    MessageRecord,
    StoreRecord,
    ToolCallRecord,
    parse_record,
)
from tools.base import MAX_OUTPUT_CHARS, truncate_output

# 超长工具输出落盘截断上限(默认与 tools.base 一致,可注入)
DEFAULT_MAX_OUTPUT_CHARS = MAX_OUTPUT_CHARS


def truncate_for_store(content: str, limit: int) -> str:
    """落盘层严格截断:复用 truncate_output 的算法与后缀,但留出后缀长度,
    保证结果严格 ≤ limit(执行层是近似截断,落盘层必须严格有界)。"""
    from tools.base import _TRUNC_SUFFIX

    if len(content) <= limit:
        return content
    keep = max(0, limit - len(_TRUNC_SUFFIX))
    truncated, _ = truncate_output(content, limit=keep)
    return truncated


class SessionStore:
    """JSONL 会话存储:追加写 + 读回 + 损坏容忍 + 跨进程继续。"""

    def __init__(
        self,
        path: Path,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        run_id: str = "",
    ) -> None:
        self.path = path
        self.max_output_chars = max_output_chars
        # M12:统一给本次写入的记录补 run_id(与 ts 同一写入点纪律)。
        self.run_id = run_id

    # ---- 写 ----

    def _append(self, record: StoreRecord) -> None:
        # M9 W2:单一写入点补落盘时间戳(Trace 据此算时长/间隙)。
        # M12:同一写入点补 run_id(缺省时)。
        # 记录可能被复用(测试构造带显式 ts/run_id)→ 只在缺省时打点,保持可预测。
        if record.ts is None:
            record.ts = time.time()
        if not record.run_id:
            record.run_id = self.run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

    def record_header(self, header: object) -> None:
        self._append(header)  # type: ignore[arg-type]

    def record_message(self, message: MessageRecord) -> None:
        self._append(message)

    def record_tool_call(self, record: ToolCallRecord) -> None:
        """落盘工具调用记录。result 超长时裁剪并标记(落盘裁剪,M4 验收点)。"""
        if len(record.result) > self.max_output_chars:
            record.result = truncate_for_store(record.result, self.max_output_chars)
            record.truncated = True
        self._append(record)

    def record_phase(self, record: object) -> None:
        self._append(record)  # type: ignore[arg-type]

    # ---- 读 ----

    def records(self) -> list[StoreRecord]:
        return self.read_with_skips()[0]

    def read_with_skips(self) -> tuple[list[StoreRecord], int]:
        """读回全部记录 + 被跳过的损坏行数。文件不存在 → ([], 0)。

        **崩溃安全**:append-only JSONL 的卖点是「崩溃最多丢最后一行」。但崩溃可能
        把最后一个多字节字符截成半个 —— 此时**解码**就会抛 `UnicodeDecodeError`,
        而且它发生在迭代期间、在 `try` 之外,会直接穿透出去(审计 M10–M15 B8:
        实测 `GET /api/tasks/{id}/trace` 因此返回 500)。

        这里改用 `errors="replace"`:被替换的坏字节使该行 JSON 解析失败,自然落入
        `skipped` 计数,语义与「跳过损坏行」一致 —— 保住了「最多丢最后一行」的承诺。
        """
        if not self.path.exists():
            return [], 0
        out: list[StoreRecord] = []
        skipped = 0
        # errors="replace" 而非默认 strict:尾行被截断的多字节字符 → U+FFFD,
        # 该行 json 解析失败 → skipped += 1(不抛异常)。
        with self.path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(parse_record(line))
                except (ValueError, TypeError, KeyError):
                    skipped += 1
        return out, skipped

    # ---- 跨进程 ----

    def continue_session(self) -> None:
        """进程重启后复用既有文件。

        M10:目录创建延后到首次 `_append`(`path.parent.mkdir` 是唯一的建目录点)。
        这样「读了旧会话但最终没写」的路径(计划被拒绝)不产生任何文件系统副作用。
        """
        return


@dataclass
class RecoveredEvent:
    """恢复事件:一种待执行动作(喂回模型 / 跳过并回灌提示)。"""

    kind: Literal["message", "skip"] = "message"
    message: AgentMessage | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind == "message" and self.message is None:
            raise ValueError("message 事件必须携带 AgentMessage")


def recover_events(records: list[StoreRecord]) -> list[RecoveredEvent]:
    """从持久化事件流推导恢复动作(不重放已完成副作用操作)。

    规则:
    1. 逐条扫描 message 与 tool_call 记录。
    2. 匹配的 assistant tool_calls(该 id 有 tool_call 记录)→ 该 assistant 消息从模型历史剔除,
       恢复动作改由 tool_call 记录驱动。
    3. tool_call 记录:
       - read_only 且无对应 tool 消息 → 回灌落盘结果(重放安全)。
       - 非 read_only 且无对应 tool 消息 → 跳过,回灌「已跳过副作用」提示。
       - overridden=True → 跳过(不喂模型,防重复回灌)。
    4. 完整(assistant tool_calls + tool_call + tool 消息齐全)→ 保持原样,作为已完成历史。
    """
    out: list[RecoveredEvent] = []
    overridden: set[str] = set()

    # 第一遍:收集 overridden 标记 + 每个 tool_call 是否有对应 tool 消息回灌(已完成)
    tool_ids_with_result: set[str] = set()
    pending_tool_ids: set[str] = set()
    for rec in records:
        if isinstance(rec, ToolCallRecord):
            if rec.overridden:
                overridden.add(rec.tool_call_id)
            pending_tool_ids.add(rec.tool_call_id)
        elif isinstance(rec, MessageRecord) and rec.role == "tool" and rec.tool_call_id:
            if rec.tool_call_id in pending_tool_ids:
                tool_ids_with_result.add(rec.tool_call_id)

    # 第二遍:对每个 tool_call 记录推导恢复动作
    #  - 有对应 tool 消息(已完成回灌)→ 无需动作
    #  - overridden → 已覆写,跳过(防重复回灌)
    #  - 只读 → 重放落盘结果(安全)
    #  - 写/命令 → 副作用已发生,跳过并回灌提示
    for rec in records:
        if not isinstance(rec, ToolCallRecord):
            continue
        if rec.tool_call_id in overridden:
            continue
        if rec.tool_call_id in tool_ids_with_result:
            continue
        if is_read_only(rec.tool_name):
            out.append(
                RecoveredEvent(
                    kind="message",
                    message=AgentMessage(
                        role="tool",
                        content=rec.result or "",
                        tool_call_id=rec.tool_call_id,
                    ),
                    reason="只读操作,重放已落盘结果",
                )
            )
        else:
            out.append(
                RecoveredEvent(
                    kind="skip",
                    message=AgentMessage(
                        role="tool",
                        content=(
                            f"[恢复] 工具 {rec.tool_name} 的副作用已在上次运行完成,"
                            f"本次已跳过,不重放(tool_call_id={rec.tool_call_id})。"
                            f"如需重新执行,请发起新的工具调用。"
                        ),
                        tool_call_id=rec.tool_call_id,
                    ),
                    reason=f"副作用操作 {rec.tool_name} 已在上次完成,跳过重放",
                )
            )
    return out


def build_resume_seed(records: list[StoreRecord]) -> list[AgentMessage]:
    """从持久化记录构建「恢复模型上下文」(含已完成副作用,不重新执行)。

    规则(要点:恢复 vs 重放的边界):
    - 已完成的历史(assistant + tool 结果)原样保留 → 模型看到已发生的事实,不重放。
    - overridden 的 assistant 消息(其结果已不可用)整条剔除,避免悬空 tool_calls。
    - pending 读 → 追加落盘结果;pending 写 → 追加「已跳过副作用」提示(recover_events)。
    """
    overridden = {
        r.tool_call_id
        for r in records
        if isinstance(r, ToolCallRecord) and r.overridden
    }
    msgs: list[AgentMessage] = []
    for rec in records:
        if not isinstance(rec, MessageRecord):
            continue
        if rec.role == "assistant" and rec.tool_calls:
            if any(tc.id in overridden for tc in rec.tool_calls):
                continue
            msgs.append(
                AgentMessage(
                    role="assistant",
                    content=rec.content,
                    reasoning_content=rec.reasoning_content,
                    tool_calls=rec.tool_calls,
                )
            )
        elif rec.role == "tool":
            if rec.tool_call_id in overridden:
                continue
            msgs.append(
                AgentMessage(role="tool", content=rec.content, tool_call_id=rec.tool_call_id)
            )
        else:
            msgs.append(
                AgentMessage(
                    role=rec.role,  # type: ignore[arg-type]
                    content=rec.content,
                    reasoning_content=rec.reasoning_content,
                )
            )
    # 追加恢复动作(只读重放结果 / 副作用跳过提示)
    for ev in recover_events(records):
        if ev.message is not None:
            msgs.append(ev.message)
    return msgs
