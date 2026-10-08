"""Execution pipeline models and computation functions.

Contains the core execution result models (TestCaseResult, PromptResult, etc.)
and the EvalRun top-level container, along with failure rate and generation
correctness computation functions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, model_validator

from ...metrics.models import (
    CodeQualityMetrics,
    CoreFunctionCoverage,
    DurationStatistics,
    ExperienceMetrics,
    QualityMetrics,
    StabilityMetrics,
    StateHandlingMetrics,
    SuccessRateMetrics,
)
from .errors import EvaluationError
from .summary import (
    EvalSummary,
    FailureDetail,
    FailureRateMetrics,
    GenerationCorrectnessMetrics,
    classify_failure,
)


class TestCaseResult(BaseModel):
    """Result of executing a single test case."""
    test_case_id: str
    passed: bool
    status: str = ""  # "PASS" or "FAIL"
    details: str = ""
    duration: float = 0.0
    # Path to the ai-ui-test generated report (report.html) on disk.
    # Captured from the CLI JSON output so we can later copy it into
    # the V2 evaluation output directory with a recognisable name.
    # When executor snapshotting is enabled this points at the snapshot
    # copy, not the original shared midscene_run location.
    report_path: str = ""
    # Unix seconds when the ai-ui-test CLI started for this case. Used
    # to validate that the exported report actually belongs to this run
    # (vs. a stale file left over in a shared report directory).
    report_started_at: float = 0.0
    # Unix seconds of the report file's mtime at capture time.
    report_generated_at: float = 0.0
    # Verification results from ai-ui-test (white_screen, real_backend, etc.)
    verifications: dict | None = None
    error_details: list[EvaluationError] = Field(default_factory=list)


class DurationMetrics(BaseModel):
    """Duration metrics collected from the generation pipeline."""

    understanding_ms: int | None = None
    planning_ms: int | None = None
    codegen_ms: int | None = None
    build_ms: int | None = None
    total_ms: int | None = None


class ProcessCollection(BaseModel):
    """Process data collected during evaluation (duration and token metrics)."""

    collector_name: str = ""
    session_id: str = ""
    project_id: str = ""
    work_dir: str = ""
    error_type: str = ""
    error_message: str = ""
    token_input: int | None = None
    token_output: int | None = None
    token_total: int | None = None
    durations: DurationMetrics = Field(default_factory=DurationMetrics)
    raw: dict[str, Any] = Field(default_factory=dict)


class E2EResult(BaseModel):
    """Framework-collected E2E verification result.

    NOTE: This represents E2E results **self-reported by the generation framework**
    (e.g. the generator's internal checks). It differs from PromptResult.test_results which
    contains results from the **independent evaluation engine** (ai-ui-test / Midscene).
    When both exist, PromptResult.test_results is authoritative for scoring.
    """

    pass_count: int = 0
    fail_count: int = 0
    total_count: int = 0
    pass_rate: float = 0.0
    test_results: list[TestCaseResult] = Field(default_factory=list)


class FrameworkResultCollection(BaseModel):
    """Framework self-collected result data for a single evaluation item."""

    task_id: str = ""
    system_id: str = ""
    requirement: str = ""
    platform: str = ""
    generation_status: str = "unknown"
    build_status: str = "unknown"
    install_status: str = "unknown"
    launch_status: str = "unknown"
    duration_build_ms: int | None = None
    duration_total_ms: int | None = None
    artifact_path: str = ""
    h5_url: str = ""
    e2e_result: E2EResult = Field(default_factory=E2EResult)
    cr_result: dict[str, Any] = Field(default_factory=dict)
    # Phase 1 usability metrics
    stability_metrics: StabilityMetrics | None = None
    core_function_coverage: CoreFunctionCoverage | None = None
    state_handling: StateHandlingMetrics | None = None
    code_quality: CodeQualityMetrics | None = None


class PromptResult(BaseModel):
    """Result of evaluating one prompt on one platform with one generator."""
    prompt_id: str
    platform: str
    generator_name: str
    item_type: str = "prompt"
    sample_id: str = ""
    sample_title: str = ""  # 新增: 样本中文标题
    sample_complexity: str = ""
    sample_top_category: str = ""  # 新增: TOP应用分类
    requirement: str = ""
    session_id: str = ""
    project_id: str = ""
    generation_success: bool
    generation_duration: float = 0.0
    project_path: str = ""
    error_message: str = ""
    process_data: ProcessCollection = Field(default_factory=ProcessCollection)
    result_data: FrameworkResultCollection = Field(default_factory=FrameworkResultCollection)
    test_results: list[TestCaseResult] = Field(default_factory=list)
    # NOTE: test_results contains results from the independent evaluation engine
    # (ai-ui-test / Midscene). This is the authoritative data source for E2E scoring.
    # Differs from result_data.e2e_result which is the framework's self-reported result.
    
    # 新顶层指标 (替换Phase1指标)
    success_rate: SuccessRateMetrics | None = None
    quality: QualityMetrics | None = None
    experience: ExperienceMetrics | None = None
    e2e_report_path: str = ""  # E2E测试报告相对路径
    requires_backend: bool = False  # 是否需要后端服务
    error_details: list[EvaluationError] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_identifiers(self) -> "PromptResult":
        """Ensure at least one of prompt_id or sample_id is non-empty."""
        if not self.prompt_id and not self.sample_id:
            raise ValueError("PromptResult requires at least one of prompt_id or sample_id to be non-empty")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def item_id(self) -> str:
        """Primary identifier: prefers sample_id, falls back to prompt_id."""
        return self.sample_id or self.prompt_id

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.test_results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.test_results if not r.passed)

    @property
    def total_count(self) -> int:
        return len(self.test_results)

    @property
    def pass_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.pass_count / self.total_count


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _is_status_success(status: str) -> bool:
    """Check if a status string indicates success."""
    return status.lower() in ("success", "passed", "ok")


def _is_status_skipped(status: str) -> bool:
    """Check if a status string indicates the phase was skipped (N/A).

    Skipped phases (e.g. install for miniprogram) should not be treated
    as failures and should not block downstream phases.
    """
    return status.lower() == "skipped"


# ---------------------------------------------------------------------------
# Generation correctness computation
# ---------------------------------------------------------------------------


def _compute_gc_from_results(
    results: list[PromptResult],
) -> GenerationCorrectnessMetrics:
    """Compute GenerationCorrectnessMetrics from a list of PromptResult."""
    total = len(results)
    if total == 0:
        return GenerationCorrectnessMetrics()

    gen_ok = 0
    install_ok = 0
    launch_ok = 0
    gen_failures: list[dict[str, str]] = []
    install_failures: list[dict[str, str]] = []
    launch_failures: list[dict[str, str]] = []
    platform_results: dict[str, list[PromptResult]] = {}
    complexity_results: dict[str, list[PromptResult]] = {}

    for pr in results:
        rd = pr.result_data
        # --- Generation success (includes build) ---
        build_skipped = rd is not None and _is_status_skipped(rd.build_status)
        gen_success = pr.generation_success and (
            True if build_skipped
            else (
                _is_status_success(rd.build_status)
                if rd and rd.build_status not in ("unknown", "")
                else pr.generation_success
            )
        )
        if gen_success:
            gen_ok += 1
        else:
            gen_failures.append({
                "item_id": pr.item_id,
                "platform": pr.platform,
                "error": pr.error_message[:300] if pr.error_message else (
                    "build_status=" + (rd.build_status if rd else "N/A")
                ),
            })
        # --- Install success ---
        install_skipped = rd is not None and _is_status_skipped(rd.install_status)
        install_success = (
            gen_success and (
                install_skipped
                or (rd is not None and _is_status_success(rd.install_status))
            )
        )
        if install_success:
            install_ok += 1
        elif gen_success:
            install_failures.append({
                "item_id": pr.item_id,
                "platform": pr.platform,
                "error": "install_status=" + (rd.install_status if rd else "unknown"),
            })
        # --- Launch success ---
        launch_success = (
            install_success and rd is not None and _is_status_success(rd.launch_status)
        )
        if launch_success:
            launch_ok += 1
        elif install_success:
            launch_failures.append({
                "item_id": pr.item_id,
                "platform": pr.platform,
                "error": "launch_status=" + (rd.launch_status if rd else "unknown"),
            })
        # Group for breakdown
        platform_results.setdefault(pr.platform, []).append(pr)
        if pr.sample_complexity:
            complexity_results.setdefault(pr.sample_complexity, []).append(pr)

    gen_rate = gen_ok / total
    install_rate = install_ok / total
    launch_rate = launch_ok / total
    composite = gen_rate * 0.45 + install_rate * 0.20 + launch_rate * 0.35

    per_platform: dict[str, GenerationCorrectnessMetrics] = {}
    for plat, plat_results in platform_results.items():
        per_platform[plat] = _compute_gc_flat(plat_results)
    per_complexity: dict[str, GenerationCorrectnessMetrics] = {}
    for comp, comp_results in complexity_results.items():
        per_complexity[comp] = _compute_gc_flat(comp_results)

    return GenerationCorrectnessMetrics(
        total_samples=total,
        generation_success_count=gen_ok,
        install_success_count=install_ok,
        launch_success_count=launch_ok,
        generation_success_rate=gen_rate,
        install_success_rate=install_rate,
        launch_success_rate=launch_rate,
        composite_score=round(composite * 100, 2),
        per_platform=per_platform,
        per_complexity=per_complexity,
        generation_failures=gen_failures,
        install_failures=install_failures,
        launch_failures=launch_failures,
    )


# ---------------------------------------------------------------------------
# Failure rate computation
# ---------------------------------------------------------------------------


def _determine_failure_stage(
    pr: PromptResult,
) -> str | None:
    """Determine which pipeline stage a PromptResult failed at.

    Returns the stage name string, or None if no failure.
    """
    rd = pr.result_data

    # Check generation (includes build)
    if not pr.generation_success:
        # Use error_type from process data to distinguish sub-stages
        et = pr.process_data.error_type.strip().lower()
        if et in ("requirements_analysis", "requirement_analysis", "requirement_understanding"):
            return "requirement_understanding"
        if et in ("planning", "plan", "design"):
            return "planning"
        if et in ("build", "compile"):
            return "build"
        return "generation"  # code_generation or generic generation failure

    # Check build failure (generation succeeded but build failed)
    if rd and rd.build_status not in ("unknown", "") and not _is_status_success(rd.build_status):
        return "build"

    # Check install failure
    if rd and not _is_status_success(rd.install_status):
        return "install"

    # Check launch failure
    if rd and not _is_status_success(rd.launch_status):
        return "launch"

    return None


def compute_failure_rate_metrics(
    results: list[PromptResult],
) -> FailureRateMetrics:
    """Compute failure rate metrics from a list of PromptResult.

    Per V2 design doc failure rate table:
    - 需求理解失败率 = requirement understanding failures / total
    - 规划失败率 = planning failures / total
    - 生成失败率 = generation (code gen + build) failures / total
    - 安装失败率 = install failures / total
    - 启动失败率 = launch failures / total
    """
    total = len(results)
    if total == 0:
        return FailureRateMetrics()

    ru_fail = 0
    plan_fail = 0
    gen_fail = 0
    install_fail = 0
    launch_fail = 0
    failures: list[FailureDetail] = []
    reason_dist: dict[str, int] = {}
    platform_results: dict[str, list[PromptResult]] = {}
    complexity_results: dict[str, list[PromptResult]] = {}

    for pr in results:
        stage = _determine_failure_stage(pr)

        # Group for breakdowns
        platform_results.setdefault(pr.platform, []).append(pr)
        if pr.sample_complexity:
            complexity_results.setdefault(pr.sample_complexity, []).append(pr)

        if stage is None:
            continue  # no failure

        # Classify the failure
        error_type = pr.process_data.error_type if pr.process_data else ""
        error_message = pr.error_message or (
            pr.process_data.error_message if pr.process_data else ""
        )
        category = classify_failure(error_type, error_message, stage)

        # Count per stage
        if stage == "requirement_understanding":
            ru_fail += 1
        elif stage == "planning":
            plan_fail += 1
        elif stage in ("generation", "build"):
            gen_fail += 1
        elif stage == "install":
            install_fail += 1
        elif stage == "launch":
            launch_fail += 1

        # Track failure detail
        failures.append(FailureDetail(
            item_id=pr.item_id,
            platform=pr.platform,
            category=category,
            stage=stage,
            error_type=error_type,
            error_message=error_message[:500],
        ))

        # Update reason distribution
        cat_key = category.value
        reason_dist[cat_key] = reason_dist.get(cat_key, 0) + 1

    # Compute per-platform and per-complexity breakdowns (flat)
    per_platform: dict[str, FailureRateMetrics] = {}
    for plat, plat_results in platform_results.items():
        per_platform[plat] = _compute_failure_rate_flat(plat_results)
    per_complexity: dict[str, FailureRateMetrics] = {}
    for comp, comp_results in complexity_results.items():
        per_complexity[comp] = _compute_failure_rate_flat(comp_results)

    return FailureRateMetrics(
        total_samples=total,
        requirement_understanding_failure_count=ru_fail,
        planning_failure_count=plan_fail,
        generation_failure_count=gen_fail,
        install_failure_count=install_fail,
        launch_failure_count=launch_fail,
        requirement_understanding_failure_rate=ru_fail / total,
        planning_failure_rate=plan_fail / total,
        generation_failure_rate=gen_fail / total,
        install_failure_rate=install_fail / total,
        launch_failure_rate=launch_fail / total,
        failure_reason_distribution=reason_dist,
        failures=failures,
        per_platform=per_platform,
        per_complexity=per_complexity,
    )


def _compute_failure_rate_flat(
    results: list[PromptResult],
) -> FailureRateMetrics:
    """Compute flat failure rate metrics (no nested breakdowns)."""
    total = len(results)
    if total == 0:
        return FailureRateMetrics()

    ru_fail = plan_fail = gen_fail = install_fail = launch_fail = 0
    reason_dist: dict[str, int] = {}

    for pr in results:
        stage = _determine_failure_stage(pr)
        if stage is None:
            continue

        error_type = pr.process_data.error_type if pr.process_data else ""
        error_message = pr.error_message or (
            pr.process_data.error_message if pr.process_data else ""
        )
        category = classify_failure(error_type, error_message, stage)

        if stage == "requirement_understanding":
            ru_fail += 1
        elif stage == "planning":
            plan_fail += 1
        elif stage in ("generation", "build"):
            gen_fail += 1
        elif stage == "install":
            install_fail += 1
        elif stage == "launch":
            launch_fail += 1

        cat_key = category.value
        reason_dist[cat_key] = reason_dist.get(cat_key, 0) + 1

    return FailureRateMetrics(
        total_samples=total,
        requirement_understanding_failure_count=ru_fail,
        planning_failure_count=plan_fail,
        generation_failure_count=gen_fail,
        install_failure_count=install_fail,
        launch_failure_count=launch_fail,
        requirement_understanding_failure_rate=ru_fail / total,
        planning_failure_rate=plan_fail / total,
        generation_failure_rate=gen_fail / total,
        install_failure_rate=install_fail / total,
        launch_failure_rate=launch_fail / total,
        failure_reason_distribution=reason_dist,
    )


def _compute_gc_flat(results: list[PromptResult]) -> GenerationCorrectnessMetrics:
    """Compute flat (no nested breakdowns) generation correctness metrics."""
    total = len(results)
    if total == 0:
        return GenerationCorrectnessMetrics()
    gen_ok = install_ok = launch_ok = 0
    for pr in results:
        rd = pr.result_data
        build_skipped = rd is not None and _is_status_skipped(rd.build_status)
        gen_success = pr.generation_success and (
            True if build_skipped
            else (
                _is_status_success(rd.build_status)
                if rd and rd.build_status not in ("unknown", "")
                else pr.generation_success
            )
        )
        if gen_success:
            gen_ok += 1
        install_skipped = rd is not None and _is_status_skipped(rd.install_status)
        install_success = (
            gen_success and (
                install_skipped
                or (rd is not None and _is_status_success(rd.install_status))
            )
        )
        if install_success:
            install_ok += 1
        launch_success = (
            install_success and rd is not None and _is_status_success(rd.launch_status)
        )
        if launch_success:
            launch_ok += 1
    gen_rate = gen_ok / total
    install_rate = install_ok / total
    launch_rate = launch_ok / total
    composite = gen_rate * 0.45 + install_rate * 0.20 + launch_rate * 0.35
    return GenerationCorrectnessMetrics(
        total_samples=total,
        generation_success_count=gen_ok,
        install_success_count=install_ok,
        launch_success_count=launch_ok,
        generation_success_rate=gen_rate,
        install_success_rate=install_rate,
        launch_success_rate=launch_rate,
        composite_score=round(composite * 100, 2),
    )


# ---------------------------------------------------------------------------
# EvalRun - top-level evaluation run container
# ---------------------------------------------------------------------------


class EvalRun(BaseModel):
    """A complete evaluation run."""
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    generator_name: str = ""
    run_type: str = "prompt"
    sample_source: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    prompt_results: list[PromptResult] = Field(default_factory=list)
    summary: EvalSummary = Field(default_factory=EvalSummary)

    def compute_summary(self, prompt_categories: dict[str, str] | None = None, *, workspace_path: Path | str | None = None) -> None:
        """Compute the summary from prompt results.
        
        Args:
            prompt_categories: mapping of item_id -> category
            workspace_path: optional workspace directory for reading scores.json
        """
        prompt_categories = prompt_categories or {}
        self._workspace_path_for_summary: Path | None = Path(workspace_path) if workspace_path else None

        total_tc = 0
        total_passed = 0
        platform_tc: dict[str, int] = {}
        platform_passed: dict[str, int] = {}
        category_tc: dict[str, int] = {}
        category_passed: dict[str, int] = {}

        prompt_ids = set()
        for pr in self.prompt_results:
            item_id = pr.item_id
            prompt_ids.add(item_id)
            tc_count = pr.total_count
            pass_count = pr.pass_count
            total_tc += tc_count
            total_passed += pass_count

            platform_tc[pr.platform] = platform_tc.get(pr.platform, 0) + tc_count
            platform_passed[pr.platform] = platform_passed.get(pr.platform, 0) + pass_count

            cat = prompt_categories.get(item_id, "general")
            category_tc[cat] = category_tc.get(cat, 0) + tc_count
            category_passed[cat] = category_passed.get(cat, 0) + pass_count

        self.summary = EvalSummary(
            total_prompts=len(prompt_ids),
            total_test_cases=total_tc,
            total_passed=total_passed,
            total_failed=total_tc - total_passed,
            overall_pass_rate=total_passed / total_tc if total_tc > 0 else 0.0,
            per_platform_pass_rate={
                p: platform_passed[p] / platform_tc[p]
                for p in platform_tc
                if platform_tc[p] > 0
            },
            per_category_pass_rate={
                c: category_passed[c] / category_tc[c]
                for c in category_tc
                if category_tc[c] > 0
            },
            duration_statistics=self._compute_duration_statistics(),
            failure_rate_metrics=self._compute_failure_rate_metrics(),
            top_level_summary=self._compute_top_level_summary(),
        )
        # cleanup temporary attribute
        self._workspace_path_for_summary = None

    def _compute_failure_rate_metrics(self) -> FailureRateMetrics:
        """Compute per-stage failure rate metrics from prompt results."""
        if not self.prompt_results:
            return FailureRateMetrics()
        return compute_failure_rate_metrics(self.prompt_results)

    def _compute_top_level_summary(self) -> dict:
        """计算顶层指标汇总
        
        包含全局平均和按平台分解的指标统计。
        任何子指标计算异常（如已下线字段引用）仅记录警告，
        不会导致整个汇总崩溃。
        """
        try:
            return self._compute_top_level_summary_impl()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "top_level_summary computation failed: %s", exc, exc_info=True,
            )
            return {}

    def _compute_top_level_summary_impl(self) -> dict:
        """顶层汇总的实际实现（由 ``_compute_top_level_summary`` 包裹错误隔离）。"""
        if not self.prompt_results:
            return {}
        
        total_samples = len(self.prompt_results)
        if total_samples == 0:
            return {}
        
        # 全局指标收集
        success_rates = []
        qualities = []
        experiences = []
        durations = []
        
        # 收集成功率详细数据
        initial_gen_rates = []
        issue_fix_rates = []
        req_ext_rates = []
        
        # 收集功能完整性详细数据
        func_completeness_scores = []
        stability_scores = []
        backend_completeness_scores = []
        compliance_scores = []
        total_e2e_pass = 0
        total_e2e_count = 0
        total_crashes = 0
        total_anrs = 0
        
        # 集体体验详细数据
        duration_scores = []
        aesthetics_scores = []
        
        # Token聚合统计
        token_totals = []
        total_token_input = 0
        total_token_output = 0
        
        # 新增: 按平台分组统计
        platform_metrics: dict[str, dict] = {}
        
        for pr in self.prompt_results:
            platform = pr.platform
            
            # 初始化平台指标容器
            if platform not in platform_metrics:
                platform_metrics[platform] = {
                    'sample_count': 0,
                    'success_rates': [],
                    'qualities': [],
                    'experiences': [],
                    'durations': [],
                    'initial_gen_rates': [],
                    'func_completeness_scores': [],
                    'stability_scores': [],
                    'backend_completeness_scores': [],
                    'duration_scores': [],
                    'e2e_pass': 0,
                    'e2e_count': 0,
                    'crashes': 0,
                    'anrs': 0,
                    'white_screens': 0,
                    'token_totals': [],
                    'aesthetics_scores': [],
                }
            
            platform_metrics[platform]['sample_count'] += 1
            
            # 成功率
            if pr.success_rate is not None:
                success_rates.append(pr.success_rate.composite_score)
                initial_gen_rates.append(pr.success_rate.initial_generation_rate)
                issue_fix_rates.append(pr.success_rate.issue_fix_rate)
                req_ext_rates.append(pr.success_rate.requirement_extension_rate)
                
                platform_metrics[platform]['success_rates'].append(pr.success_rate.composite_score)
                platform_metrics[platform]['initial_gen_rates'].append(pr.success_rate.initial_generation_rate)
            
            # 功能完整性
            if pr.quality is not None:
                qualities.append(pr.quality.composite_score)
                func_completeness_scores.append(pr.quality.usecase_completeness)
                if pr.quality.stability_score is not None:
                    stability_scores.append(pr.quality.stability_score)
                if pr.quality.backend_completeness is not None:
                    backend_completeness_scores.append(pr.quality.backend_completeness)
                compliance_scores.append(pr.quality.compliance_score)
                total_e2e_pass += pr.quality.e2e_pass_count
                total_e2e_count += pr.quality.e2e_total_count
                total_crashes += pr.quality.crash_count
                total_anrs += pr.quality.anr_count
                
                platform_metrics[platform]['qualities'].append(pr.quality.composite_score)
                platform_metrics[platform]['func_completeness_scores'].append(pr.quality.usecase_completeness)
                if pr.quality.stability_score is not None:
                    platform_metrics[platform]['stability_scores'].append(pr.quality.stability_score)
                if pr.quality.backend_completeness is not None:
                    platform_metrics[platform]['backend_completeness_scores'].append(pr.quality.backend_completeness)
                platform_metrics[platform]['e2e_pass'] += pr.quality.e2e_pass_count
                platform_metrics[platform]['e2e_count'] += pr.quality.e2e_total_count
                platform_metrics[platform]['crashes'] += pr.quality.crash_count
                platform_metrics[platform]['anrs'] += pr.quality.anr_count
                platform_metrics[platform]['white_screens'] += getattr(pr.quality, 'white_screen_count', 0)
            
            # 体验
            if pr.experience is not None:
                experiences.append(pr.experience.composite_score)
                durations.append(pr.experience.end_to_end_duration_ms)
                duration_scores.append(pr.experience.duration_score)
                
                platform_metrics[platform]['experiences'].append(pr.experience.composite_score)
                platform_metrics[platform]['durations'].append(pr.experience.end_to_end_duration_ms)
                platform_metrics[platform]['duration_scores'].append(pr.experience.duration_score)
                
                # 美观度收集
                aes_score = getattr(pr.experience, 'aesthetics_score', None)
                # fallback: 从 scores.json 读取 aesthetics_score
                if aes_score is None:
                    aes_score = self._read_aesthetics_from_scores_json(
                        pr.sample_id or pr.prompt_id, platform,
                    )
                if aes_score is not None:
                    aesthetics_scores.append(aes_score)
                    platform_metrics[platform]['aesthetics_scores'].append(aes_score)
                
                # Token聚合: 从experience中收集token数据
                if pr.experience.token_total > 0:
                    token_totals.append(pr.experience.token_total)
                    platform_metrics[platform]['token_totals'].append(pr.experience.token_total)
                total_token_input += pr.experience.token_input
                total_token_output += pr.experience.token_output
        
        # 计算按平台分解的指标
        per_platform_summary = {}
        for plat, metrics in platform_metrics.items():
            plat_sample_count = metrics['sample_count']
            per_platform_summary[plat] = {
                'sample_count': plat_sample_count,
                'mean_success_rate': round(sum(metrics['success_rates']) / len(metrics['success_rates']), 2) if metrics['success_rates'] else 0,
                'mean_quality': round(sum(metrics['qualities']) / len(metrics['qualities']), 2) if metrics['qualities'] else 0,
                'mean_functionality_completeness': round(sum(metrics['qualities']) / len(metrics['qualities']), 2) if metrics['qualities'] else 0,
                'mean_experience': round(sum(metrics['experiences']) / len(metrics['experiences']), 2) if metrics['experiences'] else 0,
                'mean_duration_ms': round(sum(metrics['durations']) / len(metrics['durations']), 2) if metrics['durations'] else 0,
                'mean_initial_generation_rate': round(sum(metrics['initial_gen_rates']) / len(metrics['initial_gen_rates']), 2) if metrics['initial_gen_rates'] else 0,
                'mean_usecase_completeness': round(sum(metrics['func_completeness_scores']) / len(metrics['func_completeness_scores']), 2) if metrics['func_completeness_scores'] else 0,
                'mean_stability_score': round(sum(metrics['stability_scores']) / len(metrics['stability_scores']), 2) if metrics['stability_scores'] else 0,
                'mean_backend_completeness': round(sum(metrics['backend_completeness_scores']) / len(metrics['backend_completeness_scores']), 2) if metrics['backend_completeness_scores'] else 0,
                'e2e_pass_rate': round(metrics['e2e_pass'] / metrics['e2e_count'] * 100, 2) if metrics['e2e_count'] > 0 else 0,
                'e2e_pass': metrics['e2e_pass'],
                'e2e_count': metrics['e2e_count'],
                'total_crashes': metrics['crashes'],
                'total_anrs': metrics['anrs'],
                'total_white_screens': metrics['white_screens'],
                'mean_token_total': round(sum(metrics['token_totals']) / len(metrics['token_totals'])) if metrics['token_totals'] else 0,
                'mean_duration_score': round(sum(metrics['duration_scores']) / len(metrics['duration_scores']), 2) if metrics['duration_scores'] else 0,
                'mean_aesthetics_score': round(sum(metrics['aesthetics_scores']) / len(metrics['aesthetics_scores']), 2) if metrics['aesthetics_scores'] else None,
            }
        
        mean_quality_value = round(sum(qualities) / len(qualities), 2) if qualities else 0
        return {
            'sample_count': total_samples,
            # 顶层指标 (全局平均)
            'mean_success_rate': round(sum(success_rates) / len(success_rates), 2) if success_rates else 0,
            'mean_quality': mean_quality_value,
            'mean_functionality_completeness': mean_quality_value,
            'mean_experience': round(sum(experiences) / len(experiences), 2) if experiences else 0,
            # 新增: 按平台分解
            'per_platform': per_platform_summary,
            # 成功率子指标
            'mean_initial_generation_rate': round(sum(initial_gen_rates) / len(initial_gen_rates), 2) if initial_gen_rates else 0,
            'mean_issue_fix_rate': round(sum(issue_fix_rates) / len(issue_fix_rates), 2) if issue_fix_rates else 0,
            'mean_requirement_extension_rate': round(sum(req_ext_rates) / len(req_ext_rates), 2) if req_ext_rates else 0,
            # 功能完整性子指标
            'mean_usecase_completeness': round(sum(func_completeness_scores) / len(func_completeness_scores), 2) if func_completeness_scores else 0,
            'mean_stability_score': round(sum(stability_scores) / len(stability_scores), 2) if stability_scores else 0,
            'mean_backend_completeness': round(sum(backend_completeness_scores) / len(backend_completeness_scores), 2) if backend_completeness_scores else 0,
            'mean_compliance_score': round(sum(compliance_scores) / len(compliance_scores), 2) if compliance_scores else 0,
            'e2e_pass_rate': round(total_e2e_pass / total_e2e_count * 100, 2) if total_e2e_count > 0 else 0,
            'total_crashes': total_crashes,
            'total_anrs': total_anrs,
            'total_white_screens': sum(platform_metrics[p].get('white_screens', 0) for p in platform_metrics),
            # 体验子指标
            # 体验指标的子维度:
            # - duration_score: 基于耗时计算的得分(0-100)
            # - composite_score: experience.composite_score = duration_score (当前仅基于耗时)
            'mean_duration_ms': round(sum(durations) / len(durations), 2) if durations else 0,
            'mean_duration_score': round(sum(duration_scores) / len(duration_scores), 2) if duration_scores else 0,
            'mean_aesthetics_score': round(sum(aesthetics_scores) / len(aesthetics_scores), 2) if aesthetics_scores else None,
            # Token聚合统计(仅展示,不参与体验评分)
            'mean_token_total': round(sum(token_totals) / len(token_totals), 2) if token_totals else 0,
            'total_token_input': total_token_input,
            'total_token_output': total_token_output,
        }

    def _read_aesthetics_from_scores_json(
        self, sample_id: str, platform: str,
    ) -> float | None:
        """从样本 scores.json 中读取对应平台的 aesthetics_score（fallback 路径）。"""
        workspace_path = getattr(self, '_workspace_path_for_summary', None)
        if not workspace_path or not sample_id:
            return None
        scores_path = workspace_path / sample_id / "scores.json"
        if not scores_path.exists():
            return None
        try:
            data = json.loads(scores_path.read_text(encoding="utf-8"))
            plat_data = data.get("platforms", {}).get(platform, {})
            score = plat_data.get("aesthetics_score")
            if isinstance(score, (int, float)):
                return float(score)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Failed to read aesthetics_score from %s: %s", scores_path, exc,
            )
        return None

    def _compute_duration_statistics(self) -> DurationStatistics:
        """Compute aggregate duration statistics from prompt results."""
        from ...metrics import compute_duration_statistics
    
        # 从 experience 中提取 duration 数据
        durations = []
        for pr in self.prompt_results:
            if pr.experience is not None:
                durations.append(DurationMetrics(total_ms=pr.experience.end_to_end_duration_ms))
    
        if not durations:
            return DurationStatistics()
    
        return compute_duration_statistics([], durations)
