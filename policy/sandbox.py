"""项目路径沙箱:所有路径参数必须先过沙箱,拒绝逃逸。

从 M1 `verify_deepseek.py` 的 `_sandbox_path` 正式化而来。作为**纵深防护**:
runner 层、工具层、工具内部都会调用,即使某层被绕过,下一层仍能拦截。

限制:检查(relative_to)与实际 IO 之间存在 TOCTOU 窗口(路径被并发替换)。
单用户本地场景可接受;高对抗环境需在 IO 层用句柄/打开标志加固。
"""

from __future__ import annotations

from pathlib import Path


def resolve_in_sandbox(workdir: Path, path_arg: object) -> Path:
    """把路径参数解析为 workdir 内的绝对路径;越界即拒绝。

    规则:
    - path 必须是非空字符串
    - resolve() 先跟随符号链接,再做包含性检查(防 symlink 逃逸)
    - 解析结果必须位于 workdir 内
    """
    if not isinstance(path_arg, str) or not path_arg.strip():
        raise ValueError("path 必须是非空字符串")
    base = workdir.resolve()
    resolved = (base / path_arg).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(f"路径越界,拒绝访问: {path_arg!r}") from None
    return resolved
