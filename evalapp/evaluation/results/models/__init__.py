"""Data models for evaluation results.

This package provides all Pydantic models for the evaluation pipeline.
Import paths remain backward-compatible::

    from evalapp.evaluation.results.models import EvalRun
    from evalapp.evaluation.results.models import ReportData, build_report_data
    from evalapp.evaluation.results.models import *
"""

from .errors import EvaluationError
from .execution import (
    DurationMetrics,
    E2EResult,
    EvalRun,
    FrameworkResultCollection,
    ProcessCollection,
    PromptResult,
    TestCaseResult,
    _compute_failure_rate_flat,
    _compute_gc_flat,
    _compute_gc_from_results,
    _determine_failure_stage,
    _is_status_skipped,
    _is_status_success,
    compute_failure_rate_metrics,
)
from .report import (
    ReportData,
    ReportMeta,
    ReportSampleResult,
    build_report_data,
)
from .summary import (
    EvalSummary,
    FailureCategory,
    FailureDetail,
    FailureRateMetrics,
    GenerationCorrectnessMetrics,
    _ENV_ERROR_KEYWORDS,
    _ERROR_TYPE_TO_CATEGORY,
    classify_failure,
)

__all__ = [
    "EvaluationError",
    # execution
    "TestCaseResult",
    "DurationMetrics",
    "ProcessCollection",
    "E2EResult",
    "FrameworkResultCollection",
    "PromptResult",
    "EvalRun",
    "_is_status_success",
    "_is_status_skipped",
    "_compute_gc_from_results",
    "_determine_failure_stage",
    "compute_failure_rate_metrics",
    "_compute_failure_rate_flat",
    "_compute_gc_flat",
    # summary
    "FailureCategory",
    "_ERROR_TYPE_TO_CATEGORY",
    "_ENV_ERROR_KEYWORDS",
    "classify_failure",
    "FailureDetail",
    "FailureRateMetrics",
    "GenerationCorrectnessMetrics",
    "EvalSummary",
    # report
    "ReportMeta",
    "ReportSampleResult",
    "ReportData",
    "build_report_data",
]
