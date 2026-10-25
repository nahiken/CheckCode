"""verify/trace.py —— 从会话记录重建可读 Trace(M9 W2;M12 扩展 JSONL/摘要/run_id)。

纯离线逻辑(不依赖 LLM/git):读 SessionStore 的记录,重建一条带时间轴的
执行轨迹——消息、工具调用(参数/权限结论/审批边界)、运行阶段、各步耗时。

时间轴依赖记录里的 ts(M9 W2:SessionStore._append 单一写入点落盘)。
旧文件缺 ts → ts=None,时长/间隙算不出,Trace 退化为「按 append 顺序」:
仍完整呈现消息/工具/阶段,只是不显示耗时。

M12 增加三件事:
- `run_id`(每条记录带,由 _append 统一填充):一次 run/resume 一个值,记进
  `PhaseRecord(phase="run")`。`build_trace(run_id=...)` 可按次过滤,把 resume 多次的
  会话拆成「这一次跑了什么」。**权威来源是 run phase 的 detail.run_id**——header 的
  run_id 是首次运行写死的,多次 resume 的会话里它描述的是第一跑。
- `to_jsonl(entries)`:每行一个 TraceEntry,可流式消费/追加。
- `run_summary(entries)`:运行摘要(计数/时长/phases/是否通过),纯从 TraceEntry 派生;
  `passed` 取最后一条 done phase 的 detail.passed(loop 落盘的权威结论)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from store.models import (
    HeaderRecord,
    MessageRecord,
    PhaseRecord,
    StoreRecord,
    ToolCallRecord,
)
from store.session import SessionStore

# Trace 行的种类(前端/CLI 按此渲染)
StepKind = Literal["header", "message", "tool_call", "phase", "gap", "summary"]


@dataclass
class TraceEntry:
    """Trace 中的一行。kind 决定渲染;ts/duration 可选(缺 ts 文件为 None)。"""

    kind: StepKind
    ts: float | None = None
    duration_ms: float | None = None
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.ts is not None:
            out["ts"] = self.ts
        if self.duration_ms is not None:
            out["duration_ms"] = self.duration_ms
        if self.data:
            out["data"] = self.data
        return out


def _fmt_ts(ts: float | None) -> str:
    """时间戳 → 可读本地时间(HH:MM:SS.mmm);None → '-'。"""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def build_trace(
    records: list[StoreRecord], *, session_id: str = "", run_id: str | None = None
) -> list[TraceEntry]:
    """从持久化记录重建 Trace(按 append 顺序,缺 ts 也能出)。

    run_id(M12):给定则只保留**该次运行**的记录(header 除外——它是会话级上下文,
    恢复会话时不会重写,故无论哪个 run 都保留),便于在 resume 多次的会话里
    单独看「这一次跑了什么」。

    呈现规则:
    - header → 任务摘要行(含 run_id)。
    - message → 角色 + 内容(超长裁剪到 200 字符,附省略标记)。
    - tool_call → 工具名/参数/权限结论/审批边界 + 结果(超长裁剪)。
    - phase → 运行阶段标记(plan / verify / resume / done ...)。
    - 相邻有 ts 的记录 → 间隙行(前一条到当前条的耗时;无 ts 跳过)。
    末尾附统计 summary 行(记录数/有 ts 的跨度秒数)。
    """
    entries: list[TraceEntry] = []
    # 归一化:显式 `run_id=""`(CLI `--run-id ""` / `?run_id=` 空串)必须与「未给定」
    # 同义。早先空串被当成真过滤器(只留 header),静默返回近乎空的 Trace 而不报错
    # (审计 M10–M15 B6)。
    if run_id is not None and run_id == "":
        run_id = None
    filtered = run_id is not None
    if filtered:
        records = [r for r in records if isinstance(r, HeaderRecord) or r.run_id == run_id]
    # 有效的 run_id:显式请求优先;否则取会话里最近一次运行(header 的 run_id 由**首次**
    # 运行的 store 写死,多次 resume 的会话里它会说谎 —— 故以 run phase 为权威)。
    effective_run_id = (
        run_id if run_id is not None else (_last_run_id(records) or _first_run_id(records))
    )
    session_id = session_id or _session_from(records)

    for i, rec in enumerate(records):
        # 间隙:当前条与上一条有 ts → 算耗时(标记耗时长的步骤)
        if i > 0:
            prev_ts = records[i - 1].ts
            cur_ts = rec.ts
            if prev_ts is not None and cur_ts is not None:
                gap_ms = (cur_ts - prev_ts) * 1000.0
                if gap_ms >= 0:
                    entries.append(
                        TraceEntry(
                            kind="gap",
                            ts=rec.ts,
                            duration_ms=round(gap_ms, 1),
                            text=f"(+{gap_ms / 1000.0:.2f}s)",
                        )
                    )

        if isinstance(rec, HeaderRecord):
            mode = "strict" if rec.mode == "strict" else "normal"
            entries.append(
                TraceEntry(
                    kind="header",
                    # header 是**会话级**上下文(恢复时不重写),它的 ts 属于**首次**运行。
                    # 按 run_id 过滤时若把它纳入时间跨度,`duration_s` 会变成整段会话时长
                    # (审计 M10–M15 B2)。故过滤态下不带 ts;它也不是「同一次运行」的时刻。
                    ts=None if filtered else rec.ts,
                    text=f"[任务] {rec.task}",
                    data={
                        "session_id": rec.session_id,
                        "repo": rec.repo,
                        "branch": rec.branch,
                        "mode": mode,
                        # 本次(或请求的)运行的 run_id —— 显式过滤优先,否则最近一次运行。
                        "run_id": effective_run_id,
                    },
                )
            )
        elif isinstance(rec, MessageRecord):
            role = rec.role
            if role == "assistant" and rec.tool_calls:
                names = ", ".join(tc.name for tc in rec.tool_calls)
                entries.append(
                    TraceEntry(
                        kind="message",
                        ts=rec.ts,
                        text=f"[assistant→工具] 发起 {names}",
                        data={"tool_calls": [tc.name for tc in rec.tool_calls]},
                    )
                )
                if rec.reasoning_content:
                    entries.append(
                        TraceEntry(
                            kind="message",
                            ts=rec.ts,
                            text=f"[思考] {_clip(rec.reasoning_content)}",
                        )
                    )
            elif role == "assistant":
                if rec.content:
                    entries.append(
                        TraceEntry(
                            kind="message", ts=rec.ts, text=f"[assistant] {_clip(rec.content)}"
                        )
                    )
            elif role == "tool":
                entries.append(
                    TraceEntry(
                        kind="message",
                        ts=rec.ts,
                        text=f"[工具结果] {_clip(rec.content)}",
                        data={"tool_call_id": rec.tool_call_id},
                    )
                )
            else:  # system / user
                label = "system" if role == "system" else "user"
                entries.append(
                    TraceEntry(
                        kind="message", ts=rec.ts, text=f"[{label}] {_clip(rec.content)}"
                    )
                )
        elif isinstance(rec, ToolCallRecord):
            # 展示用「是否已执行」而非「权限是否放行」——before Hook 拒绝时 granted 仍为
            # True(见 hooks/registry.py),渲染成「已执行」会误导复盘(审计 C1)。
            ran = rec.granted and rec.layer != "hook" and not rec.is_error
            granted = "已执行" if ran else ("被拒" if rec.decision == "deny" else "未执行")
            approval = (
                f"审批:{'批' if rec.approved else '未批'}"
                if rec.asked
                else "判定:" + rec.decision
            )
            args_summary = _clip(json.dumps(rec.args, ensure_ascii=False, default=str))
            entries.append(
                TraceEntry(
                    kind="tool_call",
                    ts=rec.ts,
                    text=(
                        f"[工具] {rec.tool_name} {args_summary}"
                        f" → {granted}({approval},{rec.layer})"
                    ),
                    data={
                        "tool_name": rec.tool_name,
                        "tool_call_id": rec.tool_call_id,
                        "decision": rec.decision,
                        "layer": rec.layer,
                        "granted": rec.granted,
                        # 执行结论:`_is_executed` 优先读它(权限放行 ≠ 真的执行了)。
                        # 落进投影才能在离线复盘中区分「跑过」与「被拦下」。
                        "executed": rec.executed,
                        "asked": rec.asked,
                        "approved": rec.approved,
                        "is_error": rec.is_error,
                    },
                )
            )
            if rec.is_error:
                entries.append(
                    TraceEntry(kind="tool_call", ts=rec.ts, text=f"  ⚠ 错误: {_clip(rec.result)}")
                )
            elif rec.result and not rec.granted:
                entries.append(
                    TraceEntry(kind="tool_call", ts=rec.ts, text=f"  → {_clip(rec.result)}")
                )
        elif isinstance(rec, PhaseRecord):
            entries.append(
                TraceEntry(
                    kind="phase",
                    ts=rec.ts,
                    text=f"[阶段] {rec.phase}",
                    data={"phase": rec.phase, "detail": rec.data},
                )
            )

    # 汇总行:记录数 + 有 ts 的会话跨度。
    ts_list = [r.ts for r in records if r.ts is not None]
    span_s = (max(ts_list) - min(ts_list)) if ts_list else None
    entries.append(
        TraceEntry(
            kind="summary",
            text=(
                f"— 会话 {session_id or '(未命名)'}:{len(records)} 条记录"
                + (f",历时 {span_s:.1f}s" if span_s is not None else ",无时间戳(旧文件)")
            ),
            data={"run_id": effective_run_id, "session_id": session_id,
                  "records": len(records), "span_s": span_s},
        )
    )
    # 指标摘要并入末行 data:此前摘要只存在于 json/markdown 的输出包装里,
    # `--format jsonl` 只吐 TraceEntry 行,流式消费方(grep/jq)拿不到它。
    # 放进末行 data,三种格式就都能取到同一份摘要(单一来源)。
    entries[-1].data["summary"] = run_summary(entries)
    return entries


def _session_from(records: list[StoreRecord]) -> str:
    for rec in records:
        if isinstance(rec, HeaderRecord):
            return rec.session_id
    return ""


def _first_run_id(records: list[StoreRecord]) -> str:
    """记录里首个非空 run_id(旧文件 → "")。

    ⚠️ 只作**回退**:session 文件首条 header 的 run_id 由首次运行的 store 写死,
    多次 resume 的会话里它描述的是「第一次跑」,不代表当前这次。权威来源是
    `PhaseRecord(phase="run")` 的 detail.run_id(见 _last_run_id)。
    """
    for rec in records:
        if rec.run_id:
            return rec.run_id
    return ""


def _last_run_id(records: list[StoreRecord]) -> str:
    """最近一次运行的 run_id(取最后一条 phase="run" 的 detail.run_id)。"""
    for rec in reversed(records):
        if isinstance(rec, PhaseRecord) and rec.phase == "run":
            rid = str(rec.data.get("run_id", ""))
            if rid:
                return rid
    return ""


_CLIP_LIMIT = 200


def _clip(text: str, limit: int = _CLIP_LIMIT) -> str:
    """裁剪超长文本为单行(换行折叠 + 超长省略)。"""
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "…"


_KIND_LABEL: dict[str, str] = {
    "header": "任务",
    "message": "消息",
    "tool_call": "工具",
    "phase": "阶段",
    "gap": "间隔",
    "summary": "汇总",
}


def render_markdown(entries: list[TraceEntry]) -> str:
    """把 Trace 渲染为 markdown 文本(CLI 打印/归档)。"""
    lines: list[str] = ["# 会话 Trace", ""]
    for e in entries:
        if e.kind == "gap":
            lines.append(f"- **{e.text}** *(间隔 {_fmt_dur(e.duration_ms)})*")
            continue
        if e.kind == "summary":
            lines.append(f"\n_{e.text}_")
            continue
        dur = f" ({_fmt_dur(e.duration_ms)})" if e.duration_ms is not None else ""
        lines.append(f"- [{_fmt_ts(e.ts)}|{_KIND_LABEL.get(e.kind, e.kind)}] {e.text}{dur}")
    return "\n".join(lines)


def _fmt_dur(duration_ms: float | None) -> str:
    """耗时毫秒 → 可读 (x.xxs)。None → ''。"""
    if duration_ms is None:
        return ""
    if duration_ms < 1000:
        return f"{duration_ms:.0f}ms"
    return f"{duration_ms / 1000:.2f}s"


def to_summary_dict(entries: list[TraceEntry]) -> dict[str, Any]:
    """Trace → JSON 序列化 dict(供 CLI --format json / 前端)。"""
    return {
        "entries": [e.to_dict() for e in entries],
        "count": len(entries),
    }


def to_jsonl(entries: list[TraceEntry]) -> str:
    """Trace → JSONL(每行一个 TraceEntry,末尾换行;空 → "")。

    M12:与「单文档 JSON」并存——JSONL 可按行流式消费/追加(grep/jq/尾随),
    适合把多次 run 的 Trace 拼在一起做离线分析。每行都可独立 parse。
    """
    return "".join(
        json.dumps(e.to_dict(), ensure_ascii=False, default=str) + "\n" for e in entries
    )


def _is_executed(data: dict[str, Any]) -> bool:
    """这条 tool_call 是否**真的执行了**(区别于「权限放行了」)。

    `granted` 的语义是「权限放行」(见 `hooks/registry.py`)—— before Hook 拒绝时
    `granted` 仍为 True 但没有执行。若把 granted 当「已执行」展示/计数,被 Hook 拦下的
    调用会被记成跑过(审计 M10–M15 C1)。

    判据优先级:
    1. **有 `executed` 字段** → 以它为准(执行路径直接置位,见 tools/runner.py)。
       这是权威判据;它还能正确算上「执行了但失败」(超时/异常)的调用 —— 那些
       可能已产生副作用,旧启发式会把它们误判为「没执行」。
    2. **没有该字段** → 回退旧启发式 `granted and layer != "hook" and not is_error`。
       哪些记录会缺字段?两处:(a) 本次改动**之前**落盘的老会话 JSONL;(b) 测试等
       直接构造 `ToolCallRecord` 的路径(pydantic 会给默认值 False,和「缺键」区分
       不开)。回退保证这两类的统计口径不变 —— 代价是这些来源里「执行了但失败」
       仍按旧口径算作未执行(记录在案,不假装完美)。

    注:`layer == "hook"` 在权威分支下**无需**特判 —— before Hook 拒绝时执行路径根本
    没走到置位点,`executed` 保持 False。
    """
    if data.get("executed"):
        return True
    if data.get("executed") is False and data.get("layer") == "hook":
        return False
    return bool(data.get("granted")) and data.get("layer") != "hook" and not data.get(
        "is_error"
    )


def run_summary(entries: list[TraceEntry]) -> dict[str, Any]:
    """运行摘要(M12):纯从 TraceEntry 派生(不读原始记录,离线可测)。

    统计:run_id/session_id/起止时间/时长/消息数/各工具调用数/放行数/拒绝数/
    审批数/phase 列表/是否通过。`passed` 取最后一条 done phase 的 detail.passed
    (loop 落盘的**权威结论**),不靠启发式推断——resume 预验证失败等路径
    不会误判为通过。未过滤时计数即全会话累计;session_id/run_id 取 header 行。
    """
    msg_count = 0
    tool_calls: dict[str, int] = {}
    total_calls = 0
    granted = 0
    denied = 0
    executed = 0
    asked = 0
    phases: list[str] = []
    session_id = ""
    run_id = ""
    passed = False
    seen_done = False
    start_ts: float | None = None
    end_ts: float | None = None

    for e in entries:
        if e.ts is not None:
            start_ts = e.ts if start_ts is None else min(start_ts, e.ts)
            end_ts = e.ts if end_ts is None else max(end_ts, e.ts)
        if e.kind == "header":
            session_id = str(e.data.get("session_id", session_id))
            run_id = str(e.data.get("run_id", run_id))
        elif e.kind == "message":
            # `messages` 语义 = **渲染出的 message 行数**(不是 MessageRecord 条数):
            # 一条带 reasoning + tool_calls 的 assistant 记录会展开成两行(思考 + 发起工具),
            # 故这里按行计数是有意的。若要「记录条数,见 MessageRecord 统计。
            msg_count += 1
        elif e.kind == "tool_call":
            name = str(e.data.get("tool_name", ""))
            # 同一 tool_call 可能有「错误/结果」附加行(无 tool_name)→ 不计入调用数
            if name:
                total_calls += 1
                tool_calls[name] = tool_calls.get(name, 0) + 1
                if e.data.get("granted"):
                    granted += 1
                else:
                    denied += 1
                # executed = 真的跑到了工具执行(与 granted 区分:before Hook 拒绝时
                # 权限已放行但**没有执行**)。见 `_is_executed`。
                if _is_executed(e.data):
                    executed += 1
                if e.data.get("asked"):
                    asked += 1
        elif e.kind == "phase":
            phase = str(e.data.get("phase", ""))
            phases.append(phase)
            # passed 以运行权威结论为准:done phase 的 detail.passed(loop 落盘时写入)。
            # ⚠️ 只在**当前这一次运行**的 done 上取值:早先写法会把上一次 run 的 passed
            # 带进本次(本次崩溃未写 done → 报上一次的 true),即 M12 B1/M2 想堵的假阳性。
            # 判据:取**最后**一条 done;若其后又出现了 run phase(resume 起了新的一次),
            # 则本次尚无结论 → passed 保持 False(未完成 ≠ 通过)。
            if phase == "done":
                detail = e.data.get("detail")
                if isinstance(detail, dict) and "passed" in detail:
                    passed = bool(detail["passed"])
                    seen_done = True
            elif phase == "run" and seen_done:
                # 新一次运行开始且上一次已有结论 → 旧结论作废(本次还没 done)
                passed = False
                seen_done = False

    duration_s = None
    if start_ts is not None and end_ts is not None:
        duration_s = round(end_ts - start_ts, 3)
    return {
        "run_id": run_id,
        "session_id": session_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_s": duration_s,
        "messages": msg_count,
        "tool_calls": tool_calls,
        "total_tool_calls": total_calls,
        "granted": granted,
        "executed": executed,
        "denied": denied,
        "asked": asked,
        "phases": phases,
        "passed": passed,
    }


def load_session_trace(
    session_path: Path, *, run_id: str | None = None
) -> list[TraceEntry]:
    """从 session 文件路径直接构建 Trace(不存在 → [] )。run_id 给定则只取该次运行。"""
    if not session_path.exists():
        return []
    store = SessionStore(session_path)
    records = store.records()
    return build_trace(records, run_id=run_id)
