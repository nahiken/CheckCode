"""WhyCode 主 CLI 入口。

子命令(随里程碑逐步实现):
- mewcode run       执行任务(Verified Coding Loop, M3 起完整闭环)
- mewcode resume    恢复会话(M4 起)
- mewcode eval      运行评测(M8 起;固定任务集 + 指标汇总 + 基线回归)
- mewcode worker    消费共享邮箱(任务 + Agent 间消息,M6/M14)
- mewcode send     投递消息/任务到共享邮箱(M14)
- mewcode inbox    查看某 agent 的收件箱(M14,只读)

使用 subparsers 组织子命令;--task 仅属于 run/eval。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from agent.config import get_llm_config
from policy.models import PermissionDecision
from verify.loop import VerifiedLoop
from verify.prepare import prepare_repo
from worktree.manager import WorktreeManager

VERSION = "whycode 0.6.0"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mewcode", description="WhyCode 安全可控 Coding Agent 工作台"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", metavar="command")

    run = sub.add_parser("run", help="执行任务(Verified Coding Loop)")
    run.add_argument("--task", required=True, help="任务描述")
    run.add_argument("--repo", default=None, help="目标仓库路径(默认复制 demo_repo 到临时目录)")
    run.add_argument("--strict", action="store_true", help="严格模式(只读 Planner 生成计划 + 审批)")
    run.add_argument("--auto-approve", action="store_true", help="审批自动放行(非交互)")
    run.add_argument("--max-fix-rounds", type=int, default=None, help="失败修复轮次上限(默认 2)")
    run.add_argument("--test-command", default=None, help="验证命令(默认取计划或 pytest)")
    run.add_argument("--config", default=None, metavar="TOML",
                     help="whycode.toml(装配 MCP servers + 技能选择)")
    run.add_argument("--skills", action="append", default=None, metavar="NAME",
                     help="只启用这些技能(M15:其 allowedTools 同时收窄可用工具;"
                          "缺省 = 全部可见技能)")
    run.set_defaults(handler=_handle_run)

    resume = sub.add_parser("resume", help="恢复会话(不重放已完成副作用操作)")
    resume.add_argument("--session", required=True, help="会话 ID(即 run 的分支名)")
    resume.add_argument("--repo", default=None, help="目标仓库路径(必须与 run 相同)")
    resume.add_argument("--auto-approve", action="store_true", help="审批自动放行(非交互)")
    resume.add_argument("--max-fix-rounds", type=int, default=None, help="失败修复轮次上限")
    resume.add_argument("--test-command", default=None, help="验证命令(默认取计划或 pytest)")
    resume.add_argument("--config", default=None, metavar="TOML",
                        help="whycode.toml(装配 MCP servers + 技能选择)")
    resume.add_argument("--skills", action="append", default=None, metavar="NAME",
                        help="只启用这些技能(其 allowedTools 同时收窄可用工具)")
    resume.set_defaults(handler=_handle_resume)

    trace = sub.add_parser("trace", help="导出会话 Trace(离线,读 store 记录)")
    trace.add_argument("--session", required=True, help="会话 ID(即 run 的分支名)")
    trace.add_argument("--repo", default=None, help="目标仓库路径(必须与 run 相同)")
    trace.add_argument("--format", default="markdown", choices=["markdown", "json", "jsonl"],
                       help="输出格式(默认 markdown;jsonl = 每行一个 TraceEntry)")
    trace.add_argument("--run-id", default=None, metavar="RUN",
                       help="只导出该次运行(run-xxxx)的记录;并附运行摘要")
    trace.set_defaults(handler=_handle_trace)

    worker = sub.add_parser("worker", help="跑 Mailbox worker(认领任务→执行→ack/nack)")
    worker.add_argument("--queue", required=True, help="mailbox 快照文件路径(队列持久化)")
    worker.add_argument("--repo", default=None, help="worker 默认目标仓库(--repo 或 payload.repo)")
    worker.add_argument("--auto-approve", action="store_true", help="审批自动放行")
    worker.add_argument("--idle", type=int, default=0, metavar="N",
                        help="队列空后连续空转 N 轮退出(0 = 常驻)")
    worker.add_argument("--config", default=None, metavar="TOML",
                        help="whycode.toml(装配 MCP servers + 技能选择)")
    worker.set_defaults(handler=_handle_worker)

    send = sub.add_parser("send", help="向共享邮箱投递一条 Agent 间消息(或任务)")
    send.add_argument("--queue", required=True, help="mailbox 快照文件路径(与 worker 共用)")
    send.add_argument("--id", required=True, help="消息/任务 id(幂等键:重复投递不重复入队)")
    send.add_argument("--from", dest="sender", required=True, help="发件 agent_id")
    send.add_argument("--to", dest="recipient", default=None,
                      help="收件 agent_id(省略 = 广播,任一 agent 可领)")
    send.add_argument("--text", required=True, help="消息正文")
    send.add_argument("--corr-id", default=None, help="关联 id(默认取消息 id)")
    send.add_argument("--task", action="store_true",
                      help="投递为**任务**(进 worker 车道)而非消息(进 inbox 车道)")
    send.set_defaults(handler=_handle_send)

    inbox = sub.add_parser("inbox", help="以某 agent 身份查看收件箱(只读,不认领)")
    inbox.add_argument("--queue", required=True, help="mailbox 快照文件路径")
    inbox.add_argument("--agent", required=True, help="agent_id(定向消息 + 广播的可见集合)")
    inbox.add_argument("--json", action="store_true", help="输出 JSON(默认人类可读)")
    inbox.set_defaults(handler=_handle_inbox)

    eval_ = sub.add_parser("eval", help="运行评测(固定任务集 + 指标汇总 + 基线回归)")
    eval_.add_argument("--task", default=None, help="只跑指定任务 id(默认全部 11 条)")
    eval_.add_argument("--baseline", default=None, help="基线 EvalSummary JSON 路径(回归比对)")
    eval_.add_argument("--save-baseline", default=None, help="把本次摘要保存为基线 JSON")
    # 评测语义:衡量 Agent 能力,恒全放行审批(无 --auto-approve 歧义)
    eval_.set_defaults(handler=_handle_eval)
    return parser


def _make_approval_gate(
    auto_approve: bool,
) -> Callable[[str, dict[str, Any], PermissionDecision], Awaitable[bool]]:
    """审批器:--auto-approve 全放行;否则交互输入 y/n(默认拒绝)。"""
    async def gate(name: str, args: dict[str, Any], decision: PermissionDecision) -> bool:
        context = (
            f"\n[审批] 工具={name} 判定={decision.decision}({decision.layer})\n"
            f"  理由: {decision.reason}\n"
            f"  参数: {args}"
        )
        return await _confirm(auto_approve, context, "批准执行? [y/N] ")

    return gate


def _make_plan_gate(
    auto_approve: bool,
) -> Callable[[Any], Awaitable[bool]]:
    """计划审批器(M10):--auto-approve 全放行;否则打印计划并交互 y/n(默认拒绝)。"""
    async def gate(plan: Any) -> bool:
        context = (
            "\n[计划审批] Planner 生成的任务专属计划:\n"
            f"  文件: {plan.files or '(无)'}\n"
            f"  命令: {plan.commands or '(无)'}"
        )
        return await _confirm(auto_approve, context, "批准该计划并进入写阶段? [y/N] ")

    return gate


async def _confirm(auto_approve: bool, context: str, question: str) -> bool:
    """两类 CLI 审批共用的确认骨架:--auto-approve 直接放行,否则打印上下文并问 y/N。

    默认拒绝(回车/无法识别 → False):审批是安全边界,不做「默认放行」。
    """
    if auto_approve:
        return True
    print(context)
    return input(question).strip().lower() in {"y", "yes"}


def _handle_run(args: argparse.Namespace) -> None:
    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)
    repo = prepare_repo(args.repo)
    print(f"目标仓库: {repo}\n任务: {args.task}")
    if args.strict:
        print("[严格模式] 先由只读 Planner 生成计划,提交批准后再进入写阶段")
    plan = _build_plan_if_strict(config, repo, args.task, strict=args.strict)
    asyncio.run(
        _run_loop(
            config,
            repo,
            args.task,
            strict=args.strict,
            auto_approve=args.auto_approve,
            max_fix_rounds=args.max_fix_rounds,
            test_command=args.test_command,
            config_path=Path(args.config) if args.config else None,
            plan=plan,
            skills=args.skills,
        )
    )


async def _run_loop(
    config: Any,
    repo: Path,
    task: str,
    *,
    strict: bool,
    auto_approve: bool,
    max_fix_rounds: int | None,
    test_command: str | None,
    session_id: str | None = None,
    config_path: Path | None = None,
    plan: Any = None,
    skills: list[str] | None = None,
) -> None:
    # 严格模式:计划已由调用方(build_plan)生成并按需审批;此处不再重复生成。
    if strict and plan is not None:
        print(f"  计划: files={plan.files}, commands={plan.commands}")

    # M9 W3:装配层(MCP 工具 + 技能)注入 VerifiedLoop。
    # 有 config 时 async with 持有 MCP 连接;无 → 空装配(纯本地工具,与现状等价)。
    from assembly.build import build_assembly, default_user_dir

    extra_tools: dict[str, Any] = {}
    skills_extra = ""
    errors: list[str] = []
    # 生产路径显式传用户级技能根(~/.whycode,可用 WHYCODE_USER_DIR 覆盖):
    # 三级加载的「用户级」自此真正生效,不再是只有测试覆盖的库能力。
    async with build_assembly(
        repo, config_path, user_dir=default_user_dir(), allow=skills
    ) as asm:
        extra_tools = dict(asm.extra_tools)
        skills_extra = asm.skills.system_prompt_extra
        errors = asm.errors
        # M15:技能声明了 allowedTools → 收窄本次运行可用的本地工具
        allowed_local_tools = asm.skills.allowed_local_tools
        if errors:
            print("[装配告警] " + " | ".join(errors))
        if extra_tools:
            print(f"[装配] MCP 工具 {len(extra_tools)} 个接入安全链")

        loop = VerifiedLoop(
            config,
            plan=plan,
            strict=strict,
            approval_gate=_make_approval_gate(auto_approve),
            max_fix_rounds=max_fix_rounds,
            test_command=test_command,
            session_id=session_id,
            extra_tools=extra_tools,
            system_prompt_extra=skills_extra,
            # M10:严格模式 → 计划在任何写入前提交人工审批(--auto-approve 放行)
            plan_approval_gate=_make_plan_gate(auto_approve),
            require_plan_approval=strict,
            # M15:技能 allowedTools → 运行时本地工具白名单(None = 不限制)
            allowed_local_tools=allowed_local_tools,
        )
        report = await loop.run(repo, task)

    print("\n" + report.render())
    # 审计落盘在 worktree 之外(单一来源:WorktreeManager.audit_path)
    audit_path = WorktreeManager(repo).audit_path(report.branch)
    session_path = WorktreeManager(repo).session_path(report.branch)
    print(f"\n审计日志: {audit_path}")
    print(f"会话记录: {session_path}(resume 用 --session {report.branch})")


def _handle_resume(args: argparse.Namespace) -> None:
    """恢复会话:以既有 session_id 复用分支,不重放已完成副作用操作。"""
    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)
    repo = prepare_repo(args.repo)
    # 校验会话存在(store 记录文件在 repo/.git/whycode-sessions/<session>.jsonl)
    session_file = WorktreeManager(repo).session_path(args.session)
    if not session_file.exists():
        print(f"错误:会话不存在: {args.session}(store 文件缺失: {session_file})")
        sys.exit(1)
    # 从 store 读回原任务(header.task),用于恢复上下文
    from store.session import SessionStore

    store = SessionStore(session_file)
    records = store.records()
    from store.models import HeaderRecord

    task = next(
        (r.task for r in records if isinstance(r, HeaderRecord)),
        None,
    )
    if task is None:
        print(f"错误:会话 {args.session} 无任务记录,无法恢复。")
        sys.exit(1)
    print(f"恢复会话: {args.session}\n仓库: {repo}\n任务: {task}")
    asyncio.run(
        _run_loop(
            config,
            repo,
            task,
            strict=False,
            auto_approve=args.auto_approve,
            max_fix_rounds=args.max_fix_rounds,
            test_command=args.test_command,
            session_id=args.session,
            config_path=Path(args.config) if args.config else None,
            skills=args.skills,
        )
    )


def _build_plan_if_strict(
    config: Any, repo: Path, task: str, *, strict: bool
) -> Any:
    """严格模式:只读 Planner 生成计划(在事件循环外,与 _run_loop 解耦)。

    计划为空 → ValueError(无写意图);此时返回 None,交给闭环走只读分析 pass(W6)。
    """
    if not strict:
        return None
    from policy.planner import build_plan

    try:
        return asyncio.run(build_plan(config, repo, task))
    except ValueError:
        return None


def _handle_trace(args: argparse.Namespace) -> None:
    """导出会话 Trace:读 store 记录,离线重建消息/工具/阶段轨迹。

    格式:markdown(默认,带时间轴)/ json(单文档)/ jsonl(M12:每行一个 TraceEntry)。
    --run-id 给定则只导出该次运行(run-xxxx),并以**运行摘要**作为输出头部。
    """
    import json

    repo = prepare_repo(args.repo)
    session_file = WorktreeManager(repo).session_path(args.session)
    if not session_file.exists():
        print(f"错误:会话不存在: {args.session}(store 文件缺失: {session_file})")
        sys.exit(1)
    # 离线:纯读 store,不调用 LLM、不改仓库
    from verify.trace import (
        load_session_trace,
        render_markdown,
        run_summary,
        to_jsonl,
        to_summary_dict,
    )

    entries = load_session_trace(session_file, run_id=args.run_id)
    if not entries:
        print(f"(会话 {args.session} 无记录)")
        return
    summary = run_summary(entries)
    if args.format == "jsonl":
        print(to_jsonl(entries), end="")
    elif args.format == "json":
        payload = to_summary_dict(entries)
        payload["summary"] = summary  # M12:单文档 JSON 附带运行摘要
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        # markdown:摘要作为头部,便于人读
        print(f"_运行摘要: {json.dumps(summary, ensure_ascii=False)}_\n")
        print(render_markdown(entries))


def _handle_worker(args: argparse.Namespace) -> None:
    """Mailbox worker:从队列认领任务 → 装配 VerifiedLoop 执行 → ack/nack。

    payload 契约见 assembly/worker.py。每次认领都现场装配(repo/config 来自 payload),
    worker 本身不持有连接(任务间隔释放),常驻循环直到队列空 + idle 空转 N 轮。
    """
    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)

    from mailbox.queue import MailboxQueue
    from mailbox.store import MailboxStore
    from pathlib import Path as _Path

    from verify.loop import VerifiedLoop
    from verify.prepare import prepare_repo

    queue_path = _Path(args.queue)
    default_repo = args.repo
    default_approve = args.auto_approve
    # 装配用 config 路径:worker 级 --config 优先,否则用 payload.config
    worker_config = _Path(args.config) if args.config else None

    async def _run_task(payload: dict[str, object]) -> str | None:
        """处理一条任务:返回错误消息(None = 成功)。"""
        task = str(payload.get("task", ""))
        if not task:
            return "payload 缺 task"
        # repo:payload.repo > worker --repo > 复制 demo_repo
        repo_arg = payload.get("repo")
        repo_path = prepare_repo(str(repo_arg) if isinstance(repo_arg, str) else default_repo)
        # config:payload.config > worker --config > 无
        cfg_raw = payload.get("config")
        cfg_path = _Path(str(cfg_raw)) if isinstance(cfg_raw, str) else worker_config
        strict = bool(payload.get("strict", False))
        approve = bool(payload.get("auto_approve", default_approve))
        # 装配 + 执行(任务结束关闭 MCP 连接)
        from assembly.build import build_assembly, default_user_dir

        async with build_assembly(
            repo_path, cfg_path, user_dir=default_user_dir()
        ) as asm:
            for err in asm.errors:
                print(f"[装配告警] {err}")
            loop = VerifiedLoop(
                config,
                plan=None,
                strict=strict,
                approval_gate=_make_approval_gate(approve),
                session_id=f"wkr-{queue_path.name[:8]}-{_short()}",
                extra_tools=asm.extra_tools,
                system_prompt_extra=asm.skills.system_prompt_extra,
                # M15:技能 allowedTools → 运行时本地工具白名单(None = 不限制)
                allowed_local_tools=asm.skills.allowed_local_tools,
            )
            report = await loop.run(repo_path, task)
        # 失败不 ack → nack 重投;报告通过 → 成功
        return None if report.passed else f"任务未通过: {report.remaining_risk}"

    worker_id = f"cli-{_short()}"

    async def _deliver_message(record: Any) -> dict[str, Any] | None:
        """收消息 → 把正文当任务跑一遍 → 回复结果摘要。

        这是「agent 间消息」在本工作台里的落地含义:一个 agent 发来的请求,
        由另一个 worker 进程真正执行,再回执给原发件人(corr_id 贯穿)。
        """
        text = str(record.payload.get("text", ""))
        if not text:
            return {"error": "消息缺 text"}
        err = await _run_task({"task": text})
        return {"ok": err is None, "error": err or ""}

    async def _drain() -> None:
        import asyncio

        from assembly.worker import MailboxWorker

        queue = MailboxQueue(store=MailboxStore(queue_path))
        worker = MailboxWorker(queue, _run_task, worker_id=worker_id)
        idle = 0
        print(f"[worker] 队列 {queue_path} 常驻 id={worker_id}(空转 {args.idle} 轮退出)")
        try:
            while True:
                # 两条车道都抽干:先任务(工作台主业),再收件箱(agent 间消息)
                result = await worker.process_once()
                if result.claimed:
                    idle = 0
                    tag = "✓" if result.acked else "✗"
                    print(f"[worker] 任务 {result.task_id} {tag}"
                          + (f" {result.error}" if result.error else ""))
                    continue
                inbox = await worker.process_inbox(_deliver_message)
                if inbox.claimed:
                    idle = 0
                    tag = "✓" if inbox.acked else "✗"
                    reply = " ↩回执" if inbox.replied else ""
                    print(f"[worker] 消息 {inbox.message_id} {tag}{reply}"
                          + (f" {inbox.error}" if inbox.error else ""))
                    continue
                # 队列抽干 → 回收超租约投递(崩溃 worker 留下的 claimed 任务)。
                # 放在这里是因为本循环是唯一「还活着在看队列」的节拍;空闲时做,
                # 不占用处理路径。返回条数 > 0 说明确有 worker 崩溃过,如实打印。
                recycled = await worker.recover_expired()
                if recycled:
                    print(f"[worker] 回收 {recycled} 条超租约投递(原 worker 疑似崩溃)")
                    idle = 0
                    continue
                idle += 1
                if args.idle and idle >= args.idle:
                    print(f"[worker] 队列空,空转 {idle} 轮退出")
                    break
                await asyncio.sleep(0.5)
        finally:
            queue.close()

    asyncio.run(_drain())


def _handle_send(args: argparse.Namespace) -> None:
    """投递一条消息(默认)或任务(--task)到共享邮箱快照。

    幂等:同 `--id` 重复投递不重复入队(返回既有记录)。这是**跨进程**语义 ——
    投递方与消费方(worker)是两个进程,靠快照文件 + OS 文件锁交接。

    `--to ""` 归一为**广播**(空串不是 agent id;若原样存下,这条消息既非广播
    也不匹配任何 agent,会变成永远投不出去的黑洞)。
    """
    from mailbox.models import TaskKind
    from mailbox.queue import MailboxQueue
    from mailbox.store import MailboxStore

    recipient = args.recipient or None
    if args.task and recipient is not None:
        print("错误:--task 是任务投递,--to 只对消息有意义,两者不能同时使用。")
        sys.exit(2)

    queue = MailboxQueue(store=MailboxStore(Path(args.queue)))

    async def _send() -> tuple[str, str]:
        if args.task:
            rec = await queue.submit(args.id, {"task": args.text})
            return rec.task_id, "任务"
        rec = await queue.send_message(
            args.id,
            {"text": args.text},
            sender=args.sender,
            recipient=recipient,
            corr_id=args.corr_id,
            kind=TaskKind.MESSAGE,
        )
        return rec.task_id, "广播" if rec.recipient is None else f"→ {rec.recipient}"

    try:
        msg_id, scope = asyncio.run(_send())
    except ValueError as exc:
        # 幂等命中但车道不符(id 已被另一条车道占用)→ 明确报错而非静默 no-op
        print(f"错误:投递失败 —— {exc}")
        sys.exit(1)
    finally:
        queue.close()
    print(f"[send] {msg_id} {scope} 已入队 {args.queue}")


def _handle_inbox(args: argparse.Namespace) -> None:
    """只读查看某 agent 的收件箱:定向给我的 + 广播,且仍是 pending(可认领)。"""
    import json
    from mailbox.models import TaskKind
    from mailbox.queue import MailboxQueue
    from mailbox.store import MailboxStore

    queue = MailboxQueue(store=MailboxStore(Path(args.queue)))
    try:
        inbox = sorted(
            (
                rec
                for rec in queue.state().values()
                if rec["kind"] != TaskKind.TASK.value
                and rec["status"] == "pending"
                and (rec["recipient"] is None or rec["recipient"] == args.agent)
            ),
            key=lambda rec: rec["created_at"],
        )
    finally:
        queue.close()

    if args.json:
        print(json.dumps(inbox, ensure_ascii=False, indent=2))
        return
    if not inbox:
        print(f"[inbox] {args.agent} 无待处理消息")
        return
    for rec in inbox:
        scope = "广播" if rec["recipient"] is None else "定向"
        text = rec["payload"].get("text", "")
        print(f"[inbox] {rec['task_id']} ({scope}) from={rec['sender']}: {text}")


def _short() -> str:
    """短随机串(session/worker id 后缀;避免常驻进程 id 冲突)。"""
    import secrets

    return secrets.token_hex(4)


def _handle_eval(args: argparse.Namespace) -> None:
    """运行评测:固定任务集 → 指标汇总 → 基线回归比对。"""
    import json

    from eval.metrics import EvalSummary
    from eval.runner import run_eval_all
    from eval.tasks import EVAL_TASKS, get_task

    config = get_llm_config()
    if not config.api_key:
        print("错误:未配置 API key。请在 .env 中填写 DEEPSEEK_API_KEY 或 LLM_API_KEY。")
        sys.exit(1)

    tasks = None
    if args.task:
        try:
            tasks = [get_task(args.task)]
        except KeyError:
            print(f"错误:未知任务 id: {args.task}")
            sys.exit(1)

    print(f"评测开始({len(tasks) if tasks else len(EVAL_TASKS)} 条任务)...")
    results = asyncio.run(run_eval_all(config, tasks=tasks))  # 评测恒全放行审批
    summary = EvalSummary.from_results(results)

    # 基线回归比对
    baseline = None
    if args.baseline:
        base_path = Path(args.baseline)
        if not base_path.exists():
            print(f"错误:基线文件不存在: {base_path}")
            sys.exit(1)
        baseline = EvalSummary(**json.loads(base_path.read_text(encoding="utf-8")))
        issues = summary.regression(baseline)
        if issues:
            print("\n⚠ 基线回归:")
            for i in issues:
                print(f"  - {i}")
        else:
            print("\n✓ 无基线回归")

    if args.save_baseline:
        Path(args.save_baseline).write_text(
            json.dumps(summary.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"基线已保存: {args.save_baseline}")

    print("\n" + summary.render())
    for r in results:
        mark = "✓" if r.ok else "✗"
        print(f"  [{mark}] {r.task_id}: ok={r.ok} 越权={r.out_of_scope} "
              f"tokens={r.tokens} 耗时={r.elapsed:.1f}s")


def main() -> None:
    # Windows 管道/终端下默认 ANSI 代码页,强制 UTF-8 避免中文乱码。
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")
    parser = _build_parser()
    args = parser.parse_args()
    if getattr(args, "handler", None) is None:
        parser.print_help()
        return
    args.handler(args)


def eval_main() -> None:
    """mewcode-eval entry:直接跑评测(等价 `mewcode eval`,避免别名打印通用 usage)。

    b-2 review:之前 mewcode-eval 是 cli.main:main 同 argv 别名,跑起来是通用 usage;
    现在独立 entry 固定走 eval 子命令。
    """
    for stream in (sys.stdout, sys.stderr):
        cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8", errors="replace")
    parser = _build_parser()
    # 注入 eval 子命令(等价 mewcode eval <args>),评估参数由其自带 argparse 处理
    args = parser.parse_args(["eval", *__import__("sys").argv[1:]])
    args.handler(args)


if __name__ == "__main__":
    main()
