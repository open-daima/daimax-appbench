"""中立错误元数据：记录已知来源，不参与评分或执行控制。"""

from __future__ import annotations

import subprocess
from typing import Literal

from pydantic import BaseModel

ErrorOrigin = Literal["generator", "evaluator", "environment", "unknown"]
ErrorStage = Literal[
    "generation", "evaluation", "build", "install", "launch", "test", "scoring", "unknown",
]


class EvaluationError(BaseModel):
    """错误代码可扩展，未知责任保留 unknown。"""

    origin: ErrorOrigin = "unknown"
    stage: ErrorStage = "unknown"
    code: str = "unknown"
    message: str = ""
    raw_error_type: str = ""


def error_from_exception(exc: BaseException, *, stage: ErrorStage = "unknown") -> EvaluationError:
    """仅异常类型能证明超时；阶段不等于根因责任。"""
    return EvaluationError(
        stage=stage,
        code="timeout" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "evaluation_exception",
        message=str(exc),
        raw_error_type=type(exc).__name__,
    )


def error_from_legacy(error_type: str, message: str, *, stage: ErrorStage = "unknown") -> EvaluationError:
    """只映射明确旧代码，不用消息关键词推断责任。"""
    normalized = error_type.strip().lower()
    origins: dict[str, ErrorOrigin] = {
        "collector_error": "evaluator", "eval_script": "evaluator",
        "environment": "environment", "infra": "environment", "emulator": "environment",
    }
    codes = {
        "collector_error": "evaluation_exception", "eval_script": "evaluation_exception",
        "timeout": "timeout", "build": "build_failed", "compile": "build_failed",
        "install": "install_failed", "launch": "launch_failed",
    }
    stages: dict[str, ErrorStage] = {
        "build": "build", "compile": "build", "build_failed": "build",
        "install": "install", "install_failed": "install",
        "launch": "launch", "launch_failed": "launch",
    }
    for code in ("build_failed", "install_failed", "launch_failed"):
        codes[code] = code
    return EvaluationError(
        origin=origins.get(normalized, "unknown"), stage=stages.get(normalized, stage),
        code=codes.get(normalized, "unknown"), message=message, raw_error_type=error_type,
    )
