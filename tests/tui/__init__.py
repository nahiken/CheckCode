"""tui 测试包。

`pytest.importorskip("textual")` 放在**这里**(收集期):未安装 tui extra 的环境
会跳过整个目录,而不是在每条用例里各自 skip。这也让 CI 在没装 Textual 时仍能跑
其余套件 —— 与 pyproject 把 textual 放进 optional extra 的取舍配套。
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")
