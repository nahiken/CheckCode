"""只读 Planner:严格模式的第一步——生成待审批计划。

流程:
1. 用"只读安全调度"(全部工具 read_only → 放行,无需审批)跑一轮 Agent,
   任务是输出"修改计划":要改哪些文件 / 跑哪些命令。
2. 把计划解析为 Plan(files + commands),交给用户审批;批准后才进入真正的写阶段。

只读性如何保证(没有复制隔离):
- Planner 直接操作调用方传入的 workdir,不做复制——隔离由调用方负责
  (verify_deepseek.py 已把 demo_repo 复制到临时目录再规划,见 `verify_deepseek.py:93-96`)。
- 只读是行为保证而非拷贝保证:工具全部 read_only → 权限层放行读、拒绝写;
  即使调度被绕过,write_file 的沙箱 + Ask、run_command 的黑名单仍是第二道防线。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.config import LLMConfig, max_concurrency
from agent.loop import build_agent
from hooks.audit import AuditLog
from policy.models import Plan
from tools.registry import build_tools, tools_spec_from
from tools.runner import ToolRunner

# 计划文本中的文件/命令列表标记(与系统提示词约定一致)
PLAN_MARKER_RE = re.compile(r"\[计划\](.*?)(?:\[/计划\]|$)", re.DOTALL)


def parse_plan_from_text(text: str) -> Plan:
    """从模型输出的 [计划]...[/计划] 文本解析 Plan(files + commands)。"""
    files: list[str] = []
    commands: list[str] = []
    m = PLAN_MARKER_RE.search(text)
    if m:
        for raw in m.group(1).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("file:"):
                files.append(line[len("file:"):].strip())
            elif line.startswith("cmd:"):
                commands.append(line[len("cmd:"):].strip())
    return Plan(description=text[:200], files=files, commands=commands)


PLANNER_SYSTEM = """你是 WhyCode 的只读规划器。你只能读取文件,不能修改任何内容。
你的任务:阅读任务描述,然后输出一份修改计划,格式如下:

[计划]
file: <要修改/创建的文件路径(相对仓库根)>
file: <另一个文件>
cmd: <要运行的命令,如 python -m pytest>
[/计划]

计划必须:
- 只列出真正需要修改的文件与需要运行的命令
- 不包含任何与任务无关的改动
- file 与 cmd 行至少各一个(如果任务需要)
只输出计划,不要执行任何修改。"""


async def build_plan(
    config: LLMConfig,
    workdir: Path,
    task: str,
    *,
    audit_path: Path | None = None,
    transport: Any = None,
) -> Plan:
    """只读规划:读工作区 → 模型输出计划 → 解析为 Plan。

    只用 read_only 工具(读放行、写拒绝);Planner 本身不写任何东西。
    """
    tools = build_tools(workdir, tool_timeout=config.tool_timeout)
    read_only_spec = tools_spec_from(tools)
    runner = ToolRunner(
        workdir,
        tools=tools,
        evaluator=None,  # 兜底:仍走模式判定(read_only → permit)
        audit=AuditLog(audit_path or workdir / ".whycode" / "planner-audit.jsonl"),
    )

    loop = build_agent(
        config,
        read_only_spec,
        runner.dispatch,
        system_prompt=PLANNER_SYSTEM,
        transport=transport,
        # M13:Planner 的多文件只读扫描同样享受并发(与 VerifiedLoop 同源取上限;
        # `agent.config` 是中立模块,policy 不依赖 verify)。
        max_concurrency=max_concurrency(),
    )
    messages = await loop.run(task)
    final = messages[-1]
    plan = parse_plan_from_text(final.content)
    if not plan.files and not plan.commands:
        raise ValueError(f"Planner 未能输出有效计划(空)。模型回复: {final.content[:200]}")
    return plan
