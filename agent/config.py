"""LLM 与运行时配置加载。

支持 OpenAI 兼容端点(DeepSeek 等):
- 专用变量:DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
- 通用变量:LLM_API_KEY / LLM_BASE_URL / LLM_MODEL(可覆盖专用)
- 运行时:AGENT_MAX_STEPS / AGENT_MAX_TOKENS / AGENT_TEMPERATURE / TOOL_TIMEOUT

配置来源优先级:显式参数 > 环境变量 > .env 文件。
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    # 从项目根加载 .env(若存在);已存在的环境变量不被覆盖
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    max_steps: int = 20
    max_tokens: int = 4096
    temperature: float = 0.0
    request_timeout: float = 120.0
    # 单次工具执行超时(秒),供 M2 工具调度使用
    tool_timeout: float = 120.0
    # M10:所配模型是否为推理模型(会输出 reasoning_content)。
    # 推理模型通常不接受 temperature 参数 → 客户端据此省略该字段。
    # 注意:是否真正产生「思考」取决于 provider/模型本身;本字段只表达调用契约。
    supports_reasoning: bool = False
    # 协议族:openai = /chat/completions(OpenAI 兼容,默认 DeepSeek);
    # anthropic = /v1/messages(Anthropic Messages API)。
    # 决定客户端实现与消息/工具序列化,**不影响**上层(IR 是协议无关的)。
    provider: str = "openai"
    # Anthropic 认证头 anthropic-version(仅 provider="anthropic" 时使用)
    api_version: str = "2023-06-01"


# provider 专属的默认三元组(环境变量前缀, 默认 base_url, 默认 model)
_PROVIDER_DEFAULTS = {
    "openai": ("DEEPSEEK", "https://api.deepseek.com", "deepseek-chat"),
    "anthropic": ("ANTHROPIC", "https://api.anthropic.com", "claude-sonnet-4-6"),
}


def get_llm_config(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    request_timeout: float | None = None,
    tool_timeout: float | None = None,
    provider: str | None = None,
) -> LLMConfig:
    """从环境变量/.env 解析 LLM 配置;显式参数优先。

    变量优先级:`LLM_*` > provider 专属前缀(`DEEPSEEK_*` / `ANTHROPIC_*`)> 默认值。

    provider 决定后两级的**前缀与默认值**(见 `_PROVIDER_DEFAULTS`):
    `openai` → DeepSeek 默认;`anthropic` → Anthropic 官方端点。未知 provider
    回退 openai(不报错:配置值不应让整个进程起不来)。
    """
    _load_env()

    chosen = (provider or os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    prefix, default_url, default_model = _PROVIDER_DEFAULTS.get(
        chosen, _PROVIDER_DEFAULTS["openai"]
    )

    def _pick(generic: str, dedicated: str, default: str) -> str:
        return os.environ.get(generic) or os.environ.get(dedicated) or default

    cfg = LLMConfig(
        base_url=base_url or _pick("LLM_BASE_URL", f"{prefix}_BASE_URL", default_url),
        api_key=api_key or _pick("LLM_API_KEY", f"{prefix}_API_KEY", ""),
        model=model or _pick("LLM_MODEL", f"{prefix}_MODEL", default_model),
        max_steps=_env_int("AGENT_MAX_STEPS", 20),
        max_tokens=_env_int("AGENT_MAX_TOKENS", 4096),
        temperature=_env_float("AGENT_TEMPERATURE", 0.0),
        tool_timeout=_env_float("TOOL_TIMEOUT", 120.0),
        supports_reasoning=_env_flag("LLM_REASONING"),
        provider=chosen,
        api_version=os.environ.get("ANTHROPIC_VERSION") or "2023-06-01",
    )
    # 显式参数覆盖(逐个判断,避免 dataclasses.replace 的 **kwargs 类型问题)
    if max_steps is not None:
        cfg = dataclasses.replace(cfg, max_steps=max_steps)
    if max_tokens is not None:
        cfg = dataclasses.replace(cfg, max_tokens=max_tokens)
    if temperature is not None:
        cfg = dataclasses.replace(cfg, temperature=temperature)
    if request_timeout is not None:
        cfg = dataclasses.replace(cfg, request_timeout=request_timeout)
    if tool_timeout is not None:
        cfg = dataclasses.replace(cfg, tool_timeout=tool_timeout)
    return cfg


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量;空串视为未设置。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数,得到: {raw!r}") from None


# 同批工具调用的并发上限(env AGENT_MAX_CONCURRENCY):下界 1,上界该值。
# 上界存在是因为同步工具经默认 executor(线程池大小约 min(32, cpu+4))执行——
# 并发度超过线程池只会让工具在队列里排队,而 tool_timeout 从**入队**开始计时,
# 队列尾部可能「还没跑就超时」。取 32 与默认 executor 上限同量级。
MAX_CONCURRENCY_CEILING = 32
DEFAULT_MAX_CONCURRENCY = 4


def max_concurrency() -> int:
    """同批工具调用的并发上限(`AGENT_MAX_CONCURRENCY`,默认 4,clamp 到 [1, 32])。

    - 语义:同一批**并发安全**(Tool.parallel_safe)的只读调用最多几个同时跑;
      写/命令类调用仍是串行屏障(见 agent/tool_calls.partition_tool_calls)。
    - 默认 4:可观测(能测到水位)、对延迟有意义、且有界。
    - **库默认仍为 1**(AgentLoop / build_agent);生产装配(VerifiedLoop 的
      `_build_agent_loop`、Planner 的 `build_plan`、诊断脚本)从此处取值,
      保证并发策略只有一个来源。设 1 = 严格串行。
    """
    raw = os.environ.get("AGENT_MAX_CONCURRENCY")
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY
    return max(1, min(value, MAX_CONCURRENCY_CEILING))


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量;空串视为未设置。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字,得到: {raw!r}") from None


def _env_flag(name: str) -> bool:
    """读取布尔开关环境变量;空串/未设置视为 False。

    M10 用途:LLM_REASONING 标记所配模型为推理模型(输出 reasoning_content)。
    约定:未设置/空串/"0"/"false"/"no"(忽略大小写)→ False;其余非空值 → True。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off"}

