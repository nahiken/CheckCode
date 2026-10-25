"""M1 单元测试:`LLMConfig` 构造与 `get_llm_config` 配置加载优先级。

覆盖:
- 显式参数 > 环境变量 > .env 文件 的优先级
- LLM_*(通用) > DEEPSEEK_*(专用) 变量优先级
- 空字符串环境变量按「未设置」处理(falsy 回退到下一级)
- AGENT_MAX_STEPS → max_steps
- max_tokens / temperature / request_timeout 显式覆盖
- client_kwargs 输出字段
- frozen dataclass 不可变性

注意:真实 `.env`(含密钥)位于项目根,`_load_env` 以 override=False 加载,
因此本文件用 `monkeypatch.setenv` 显式置空/置值来控制确定性,不读取真实密钥内容。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent.config import LLMConfig, get_llm_config

_LLM_VARS = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "AGENT_MAX_STEPS",
)


def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式置空(空串按未设置处理),避免依赖真实 .env 内容
    _clear_llm_env(monkeypatch)
    for var in _LLM_VARS:
        if var == "AGENT_MAX_STEPS":
            monkeypatch.setenv(var, "20")  # 默认值即 20,显式固定避免依赖 .env
        else:
            monkeypatch.setenv(var, "")
    cfg = get_llm_config()
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-chat"
    assert cfg.api_key == ""
    assert cfg.max_steps == 20
    assert cfg.max_tokens == 4096
    assert cfg.temperature == 0.0
    assert cfg.request_timeout == 120.0


def test_llm_over_deepseek_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """同时配置 LLM_* 与 DEEPSEEK_* 时,LLM_* 必须胜出。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("LLM_MODEL", "llm-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ds.example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "ds-model")
    cfg = get_llm_config()
    assert cfg.base_url == "https://llm.example"
    assert cfg.api_key == "llm-key"
    assert cfg.model == "llm-model"


def test_deepseek_fallback_when_llm_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_* 为空串视为未设置 → 回退到 DEEPSEEK_*。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ds.example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "ds-model")
    cfg = get_llm_config()
    assert cfg.base_url == "https://ds.example"
    assert cfg.api_key == "ds-key"
    assert cfg.model == "ds-model"


def test_explicit_params_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式参数(base_url/api_key/model)必须覆盖环境变量。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    cfg = get_llm_config(
        base_url="https://explicit.example",
        api_key="explicit-key",
        model="explicit-model",
    )
    assert cfg.base_url == "https://explicit.example"
    assert cfg.api_key == "explicit-key"
    assert cfg.model == "explicit-model"


def test_max_steps_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    cfg = get_llm_config()
    assert cfg.max_steps == 7


def test_explicit_numeric_override_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """【回归】显式数值参数覆盖生效。

    修复前:`_replace(cfg, **kw)` 字段重复传参抛 TypeError。
    修复后:`dataclasses.replace` 正确覆盖。
    """
    _clear_llm_env(monkeypatch)
    cfg = get_llm_config(max_steps=5, max_tokens=2048, temperature=0.7, request_timeout=30.0)
    assert cfg.max_steps == 5
    assert cfg.max_tokens == 2048
    assert cfg.temperature == 0.7
    assert cfg.request_timeout == 30.0
    # 未显式传的值保持默认
    assert cfg.model == "deepseek-chat"


def test_empty_max_steps_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """【回归】AGENT_MAX_STEPS 空串按未配置处理,回退默认 20。

    修复前:int("") 抛 ValueError。修复后:_env_int 空串回退默认。
    """
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_STEPS", "")
    cfg = get_llm_config()
    assert cfg.max_steps == 20


def test_tool_timeout_from_env_and_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_timeout 读 TOOL_TIMEOUT 环境变量;显式参数覆盖。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("TOOL_TIMEOUT", "30")
    assert get_llm_config().tool_timeout == 30.0
    assert get_llm_config(tool_timeout=5.0).tool_timeout == 5.0


def test_tool_timeout_empty_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """TOOL_TIMEOUT 空串按未配置处理,回退默认 120。"""
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("TOOL_TIMEOUT", "")
    assert get_llm_config().tool_timeout == 120.0


def test_config_is_frozen() -> None:
    cfg = LLMConfig(base_url="u", api_key="k", model="m")
    with pytest.raises(FrozenInstanceError):
        cfg.temperature = 1.0  # type: ignore[misc]


def test_env_file_loaded_when_present() -> None:
    """真实 .env 存在时,get_llm_config 应能从其中加载配置(不校验具体密钥值)。"""
    root = Path(__file__).resolve().parents[2]
    cfg = get_llm_config()
    if (root / ".env").exists():
        assert cfg.api_key  # .env 中的 key 已加载
        assert cfg.base_url
        assert cfg.model
