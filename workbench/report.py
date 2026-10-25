"""workbench/report.py —— RiskReport → JSON 可序列化 dict(Web 与终端共用)。

原实现在 `api/app.py`,靠 **FastAPI 的 `jsonable_encoder`** 处理 `Path`。
终端不需要 FastAPI,若各写一份必然漂移(前端据此渲染通过/失败 —— 口径必须唯一)。
改用 **pydantic v2 的 `mode="json"`**(领域层已有 pydantic,零新增依赖),
它同样处理 `Path` 与嵌套模型,且不引入 Web 框架。
"""

from __future__ import annotations

from typing import Any

from verify.report import RiskReport


def report_to_dict(report: RiskReport) -> dict[str, Any]:
    """RiskReport → JSON 可序列化 dict。

    - `mode="json"`:Path → str(repo 字段是 WindowsPath,`model_dump()` 直接产出
      WindowsPath 对象,无法 `json.dumps`)。
    - `passed` 是 `@property`,不在 `model_dump()` 里 → 显式并入(消费方据此显示
      通过/失败)。**不要改成从 rounds 推导** —— 它是单一来源(见 RiskReport.passed)。
    """
    out: dict[str, Any] = report.model_dump(mode="json")
    out["passed"] = report.passed
    return out
