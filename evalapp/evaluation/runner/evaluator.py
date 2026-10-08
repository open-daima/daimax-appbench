"""Evaluator: orchestrates generation, test execution, and data collection."""

from __future__ import annotations

from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    as_completed,
    wait as futures_wait,
)
import sys
import time
from pathlib import Path
from uuid import uuid4

from ...generators import AppGenerator, GenerationResult
from ..metrics.models import (
    ExperienceMetrics,
    QualityMetrics,
    SuccessRateMetrics,
)
from ...benchset.samples.models import EvalPrompt
from ..results.models.errors import EvaluationError, error_from_exception, error_from_legacy
from ..results.models import (
    EvalRun,
    ProcessCollection,
    PromptResult,
)
from ...benchset.samples.models import EvalSample
from ...benchset.testcases.store import TestCaseStore
from ...config import Config
from ..execution_manifest import (
    ExecutionManifest,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_RUNNING,
    PHASE_SKIPPED,
)
from ...utils.device_pool import DevicePool
from ...utils.env import EVALAPP_MAX_DEVICES, get_env_int
from ...utils.logging import get_logger
from ...workspace.sample_data import write_sample_scores
from .collectors import (
    build_framework_result_data,
    collect_process_data,
    extract_package_name,
    finalize_prompt_result,
    make_no_test_cases_result,
    prepare_existing_project_data,
)
from .executor import ExecutionResult, TestExecutor
from .validators import (
    compute_experience,
    compute_success_rate,
    filter_test_cases_by_end_case,
    filter_test_cases_by_priority,
)

logger = get_logger(__name__)

# 门控调度（生成/评测流水线）的轮询间隔（秒）
_GATE_POLL_INTERVAL = 15


def _emit_sample_state(sample_id: str, platform: str, phase: str,
                       status: str, error: str = "") -> None:
    """向 stdout 输出标准的 [SAMPLE_STATE] 状态行，供 TaskRunner 实时解析。

    格式：[SAMPLE_STATE] {sample_id}|{platform}|{phase}|{status}|{error_msg}
    error_msg 为最后一个字段，允许包含 `|`；只取首行并去换行，
    以避免破坏定长五段结构。使用 sys.stdout.write + flush 避免多线程交错。
    """
    err_one_line = (error or "").replace("\r", " ").replace("\n", " ").strip()
    line = f"[SAMPLE_STATE] {sample_id}|{platform}|{phase}|{status}|{err_one_line}\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except (OSError, ValueError) as e:
        # stdout 不可用（已关闭/重定向失败）时不应阻塞主流程，但需留痕
        logger.debug(
            "_emit_sample_state stdout write failed (sample=%s, platform=%s): %s",
            sample_id, platform, e,
        )


def _build_task_list(
    explicit_tasks: list | None,
    samples: list[EvalSample],
    platforms: list[str] | None = None,
) -> list[tuple]:
    """Build task list from explicit tasks or samples x platforms.

    Supports dict format {sample, platform, end_case?, priority?}
    and tuple format (sample, platform[, end_case[, priority]]).
    """
    if explicit_tasks:
        tasks = []
        for task_item in explicit_tasks:
            if isinstance(task_item, dict):
                tasks.append((
                    task_item["sample"],
                    task_item["platform"],
                    task_item.get("end_case"),
                    task_item.get("priority"),
                ))
            else:
                if len(task_item) >= 4:
                    tasks.append(task_item)
                elif len(task_item) == 3:
                    tasks.append((task_item[0], task_item[1], task_item[2], None))
                else:
                    sample, platform = task_item
                    tasks.append((sample, platform, None, None))
    elif platforms is not None:
        # Legacy mode: derive tasks from samples and explicit platforms
        tasks = [
            (sample, platform, None, None)
            for sample in samples
            for platform in (platforms or sample.platforms)
            if platform in sample.platforms
        ]
    else:
        tasks = [
            (sample, platform, None, None)
            for sample in samples
            for platform in sample.platforms
        ]
    return tasks


class Evaluator:
    """Top-level orchestrator that generates apps and runs tests."""

    def __init__(
        self,
        generator: AppGenerator,
        executor: TestExecutor,
        test_case_store: TestCaseStore,
        workspace_path: Path | None = None,
        config: Config | None = None,
        manifest: ExecutionManifest | None = None,
    ) -> None:
        self.generator = generator
        self.executor = executor
        self.test_case_store = test_case_store
        self.config = config
        self.manifest = manifest

        self.workspace_path = workspace_path or Path.cwd()

    def evaluate_prompt(
        self,
        prompt: EvalPrompt,
        platform: str,
    ) -> PromptResult:
        """Evaluate a single prompt on a single platform."""
        return self._evaluate_item(
            item_id=prompt.id,
            requirement=prompt.text,
            category=prompt.category,
            platforms=prompt.platforms,
            platform=platform,
            item_type="prompt",
            requires_backend=prompt.requires_backend,
        )

    def evaluate_sample(
        self,
        sample: EvalSample,
        platform: str,
        end_case: str | None = None,
        priority: str | None = None,
        device_id: str | None = None,
    ) -> PromptResult:
        """Evaluate a single benchmark sample on one platform.
        
        Args:
            sample: The benchmark sample to evaluate.
            platform: Target platform.
            end_case: Optional ending test case ID (e.g., "TC003"). 
                     If set, only execute test cases up to and including this case.
            priority: Optional priority filter (e.g., "P0" or "P0,P1").
                     If set, only execute test cases with matching priority.
        """
        return self._evaluate_item(
            item_id=sample.sample_id,
            requirement=sample.requirement,
            category=sample.app_type,
            platforms=sample.platforms,
            platform=platform,
            item_type="sample",
            sample=sample,
            end_case=end_case,
            priority=priority,
            requires_backend=sample.requires_backend,
            device_id=device_id,
        )

    def evaluate_all(
        self,
        prompts: list[EvalPrompt],
        platforms: list[str],
    ) -> EvalRun:
        """Evaluate all prompts on all platforms."""
        run = EvalRun(
            generator_name=self.generator.name,
            run_type="prompt",
        )

        for prompt in prompts:
            for platform in platforms:
                if platform not in prompt.platforms:
                    continue
                result = self.evaluate_prompt(prompt, platform)
                run.prompt_results.append(result)

        categories = {p.id: p.category for p in prompts}
        run.compute_summary(categories, workspace_path=self.workspace_path)

        logger.info(
            "Evaluation complete: %s overall (%s/%s)",
            f"{run.summary.overall_pass_rate:.0%}",
            run.summary.total_passed,
            run.summary.total_test_cases,
        )
        return run

    def evaluate_existing_samples(
        self,
        samples: list[EvalSample],
        platform_tasks: list | None = None,
        max_workers: int = 1,
        generator_name: str | None = None,
        ready_check=None,
    ) -> EvalRun:
        """Evaluate existing generated code without re-generation."""
        tasks = _build_task_list(platform_tasks, samples)
        return self._execute_tasks(
            tasks=tasks,
            item_func=self._evaluate_existing_item,
            samples=samples,
            max_workers=max_workers,
            generator_name=generator_name,
            log_prefix="Existing sample evaluation",
            ready_check=ready_check,
        )

    def _execute_tasks(
        self,
        tasks: list[tuple],
        item_func,
        samples: list[EvalSample],
        max_workers: int = 1,
        generator_name: str | None = None,
        log_prefix: str = "Evaluation",
        ready_check=None,
    ) -> EvalRun:
        """统一任务执行框架

        Args:
            tasks: 任务列表 [(sample, platform, end_case, priority), ...]
            item_func: 单任务执行函数，签名 (sample, platform, end_case, priority) -> PromptResult
            samples: 样本列表（用于compute_summary）
            max_workers: 并行数
            generator_name: 生成器名（用于EvalRun创建）
            log_prefix: 日志前缀
            ready_check: 可选门控函数，签名 (sample_id, platform) -> (state, reason)，
                state ∈ {"ready", "wait", "failed"}。用于生成/评测流水线：仅当
                样本生成完成后才投入评测；failed 时跳过评测执行，但必须以
                零分结果计入 run（否则汇总平均分会因失败样本缺席而虚高），
                manifest 标记 evaluate=skipped。
        Returns:
            EvalRun
        """
        run = EvalRun(
            generator_name=generator_name or self.generator.name,
            run_type="sample",
        )

        if max_workers <= 1:
            for sample, platform, end_case, priority in tasks:
                if self._manifest_should_skip(sample, platform):
                    continue
                if ready_check is not None:
                    ready, gate_reason = self._wait_until_ready(
                        ready_check, sample, platform
                    )
                    if not ready:
                        skipped_result = self._make_gate_skipped_result(
                            sample, platform, gate_reason
                        )
                        run.prompt_results.append(skipped_result)
                        self._persist_sample_scores(sample, platform, skipped_result)
                        continue
                self._manifest_mark_running(sample, platform)
                _emit_sample_state(sample.sample_id, platform, "evaluate", "running")
                try:
                    result = item_func(
                        sample=sample,
                        platform=platform,
                        end_case=end_case,
                        priority=priority,
                    )
                    run.prompt_results.append(result)
                    self._persist_sample_scores(sample, platform, result)
                    self._manifest_mark_result(sample, platform, result)
                    self._emit_evaluate_state_from_result(sample, platform, result)
                except Exception as exc:
                    logger.error("Evaluation failed for %s/%s: %s", sample.sample_id, platform, exc)
                    self._manifest_mark_failed(sample, platform, exc)
                    _emit_sample_state(
                        sample.sample_id, platform, "evaluate", "failed", str(exc),
                    )
                    raise
        else:
            # 按平台分组，确定是否需要创建设备池
            max_devices = get_env_int(EVALAPP_MAX_DEVICES)
            if self.config is not None:
                max_devices = self.config.install_app.max_devices

            has_android = any(p == "android" for _, p, _, _ in tasks)
            has_ios = any(p == "ios" for _, p, _, _ in tasks)

            android_pool: DevicePool | None = None
            ios_pool: DevicePool | None = None

            try:
                if has_android:
                    android_pool = self._init_pool_with_degrade(
                        "android", min(max_workers, max_devices),
                    )
                if has_ios:
                    ios_pool = self._init_pool_with_degrade(
                        "ios", min(max_workers, max_devices),
                    )

                # 设备初始化完成后，对 max_workers 与实际可用设备数进行断言。
                # 若超出，裁减为实际设备数并警告；若以设备池为主的场景完全不包含可用设备，
                # 保持 max_workers >= 1 以免阻塞（其他平台任务可能不依赖设备池）。
                total_available_devices = 0
                if android_pool is not None:
                    total_available_devices += android_pool.total_slots()
                if ios_pool is not None:
                    total_available_devices += ios_pool.total_slots()
                if total_available_devices > 0 and max_workers > total_available_devices:
                    logger.warning(
                        "max_workers (%d) exceeds total available devices (%d); "
                        "truncating to %d to avoid idle workers",
                        max_workers, total_available_devices, total_available_devices,
                    )
                    max_workers = total_available_devices
                assert max_workers >= 1, "max_workers must be >= 1 after truncation"

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {}
                    pending_tasks = []
                    for sample, platform, end_case, priority in tasks:
                        if self._manifest_should_skip(sample, platform):
                            continue
                        pending_tasks.append((sample, platform, end_case, priority))

                    def _submit(task_tuple):
                        sample, platform, end_case, priority = task_tuple
                        self._manifest_mark_running(sample, platform)
                        _emit_sample_state(sample.sample_id, platform, "evaluate", "running")
                        fut = executor.submit(
                            self._execute_with_device,
                            item_func,
                            sample,
                            platform,
                            end_case,
                            priority,
                            android_pool if platform == "android" else (ios_pool if platform == "ios" else None),
                        )
                        futures[fut] = (sample, platform)

                    def _harvest(fut):
                        sample, platform = futures.pop(fut)
                        sample_id = sample.sample_id
                        try:
                            result = fut.result()
                            run.prompt_results.append(result)
                            self._persist_sample_scores(sample, platform, result)
                            self._manifest_mark_result(sample, platform, result)
                            self._emit_evaluate_state_from_result(sample, platform, result)
                        except Exception as exc:
                            logger.error("Evaluation failed for %s/%s: %s", sample_id, platform, exc)
                            result = PromptResult(
                                prompt_id=sample_id,
                                sample_id=sample_id,
                                platform=platform,
                                generator_name=self.generator.name,
                                item_type="sample",
                                generation_success=False,
                                error_message=str(exc),
                                error_details=[error_from_exception(exc)],
                                requires_backend=False,
                            )
                            run.prompt_results.append(result)
                            self._persist_sample_scores(sample, platform, result)
                            self._manifest_mark_failed(sample, platform, exc)
                            _emit_sample_state(
                                sample_id, platform, "evaluate", "failed", str(exc),
                            )

                    if ready_check is None:
                        # 无门控：保持原有行为，一次性提交全部任务
                        for task_tuple in pending_tasks:
                            _submit(task_tuple)
                        for future in as_completed(dict(futures)):
                            _harvest(future)
                    else:
                        # 门控调度（生成/评测流水线）：就绪即提交，未就绪不占 worker，
                        # 避免阻塞式等待造成队头阻塞（先提交的慢样本卡住已就绪样本）
                        while pending_tasks or futures:
                            for task_tuple in pending_tasks[:]:
                                sample, platform = task_tuple[0], task_tuple[1]
                                try:
                                    state, reason = ready_check(sample.sample_id, platform)
                                except Exception as gate_exc:  # noqa: BLE001 — 门控异常降级为继续等待
                                    logger.warning(
                                        "ready_check 异常 (%s/%s)，继续等待: %s",
                                        sample.sample_id, platform, gate_exc,
                                    )
                                    state, reason = "wait", ""
                                if state == "ready":
                                    _submit(task_tuple)
                                    pending_tasks.remove(task_tuple)
                                elif state == "failed":
                                    self._manifest_mark_gate_skipped(sample, platform, reason)
                                    skipped_result = self._make_gate_skipped_result(
                                        sample, platform, reason
                                    )
                                    run.prompt_results.append(skipped_result)
                                    self._persist_sample_scores(
                                        sample, platform, skipped_result
                                    )
                                    _emit_sample_state(
                                        sample.sample_id, platform, "evaluate", "failed",
                                        reason or "生成未完成，跳过评测",
                                    )
                                    pending_tasks.remove(task_tuple)
                            if futures:
                                done, _ = futures_wait(
                                    set(futures), timeout=_GATE_POLL_INTERVAL,
                                    return_when=FIRST_COMPLETED,
                                )
                                for fut in done:
                                    _harvest(fut)
                            elif pending_tasks:
                                time.sleep(_GATE_POLL_INTERVAL)
            finally:
                if android_pool:
                    android_pool.shutdown()
                if ios_pool:
                    ios_pool.shutdown()

        categories = {sample.sample_id: sample.app_type for sample in samples}
        try:
            run.compute_summary(categories, workspace_path=self.workspace_path)
        except Exception as exc:
            # 汇总失败不应丢失逐样本已持久化的评分数据；
            # 记录错误但不向上抛出，让结果返回后仍可生成报告。
            logger.error(
                "Failed to compute run summary (per-sample scores already persisted): %s",
                exc,
                exc_info=True,
            )

        logger.info(
            "%s complete: %s overall (%s/%s)",
            log_prefix,
            f"{run.summary.overall_pass_rate:.0%}",
            run.summary.total_passed,
            run.summary.total_test_cases,
        )
        return run

    def _init_pool_with_degrade(
        self, platform: str, max_devices: int,
    ) -> DevicePool | None:
        """初始化设备池。

        设计原则（“硬失败”策略）：
        - pool.initialize() 抛异常或 available==0 都直接报错中止评测；
          不再静默返回 None 让任务“带病”进入 install 阶段然后报“未找到设备”。
        - 个别设备启动失败、但仍有可用设备时，仅警告并继续使用剩余设备。
        """
        pool = DevicePool(platform, max_devices=max_devices)
        try:
            pool.initialize()
        except Exception as exc:
            try:
                pool.shutdown()
            except Exception as _se:  # noqa: BLE001
                logger.debug("%s device pool shutdown failed: %s", platform, _se)
            logger.error(
                "%s device pool initialization failed: %s", platform, exc,
            )
            raise RuntimeError(
                f"{platform} device pool initialization failed: {exc}. "
                f"Please ensure the {platform} emulator/simulator is reachable "
                f"(e.g. start it manually before evaluation, or check "
                f"ANDROID_HOME / Xcode / AVD configuration)."
            ) from exc

        available = pool.total_slots()
        if available == 0:
            try:
                pool.shutdown()
            except Exception as _se:  # noqa: BLE001
                logger.debug("%s device pool shutdown failed: %s", platform, _se)
            raise RuntimeError(
                f"{platform} device pool initialization failed: no available "
                f"devices after attempting to start {max_devices}. Please check "
                f"emulator/simulator availability."
            )
        if available < max_devices:
            logger.warning(
                "%s device pool partially initialized: %d/%d devices available; "
                "continuing with reduced capacity",
                platform, available, max_devices,
            )
        return pool

    def _execute_with_device(
        self,
        item_func,
        sample,
        platform: str,
        end_case,
        priority,
        pool: DevicePool | None,
    ):
        """Wrap item_func with device acquire/release from pool."""
        slot = None
        if pool is not None:
            slot = pool.acquire()
        try:
            device_id = slot.device_id if slot else None
            return item_func(
                sample=sample,
                platform=platform,
                end_case=end_case,
                priority=priority,
                device_id=device_id,
            )
        finally:
            if slot is not None and pool is not None:
                pool.release(slot)

    def _evaluate_existing_item(
        self,
        sample: EvalSample,
        platform: str,
        end_case: str | None = None,
        priority: str | None = None,
        device_id: str | None = None,
        test_case_ids: list[str] | None = None,
    ) -> PromptResult:
        """Evaluate an existing generated project without re-generation."""
        item_id = sample.sample_id
        requirement = sample.requirement
        logger.info("Evaluating existing %s on %s with %s", item_id, platform, self.generator.name)

        test_cases = self.test_case_store.load(item_id, platform)

        # Filter test cases by end_case if specified
        if end_case and test_cases:
            test_cases = self._filter_test_cases_by_end_case(test_cases, end_case)
            logger.info("Filtered test cases by end_case=%s, remaining: %d", end_case, len(test_cases))

        # Filter test cases by priority if specified
        if priority and test_cases:
            test_cases = self._filter_test_cases_by_priority(test_cases, priority)
            logger.info("Filtered test cases by priority=%s, remaining: %d", priority, len(test_cases))

        # Filter test cases by explicit IDs if specified
        if test_case_ids and test_cases:
            test_cases = [tc for tc in test_cases if tc.id in test_case_ids]
            logger.info("Filtered by test_case_ids=%s, remaining: %d", test_case_ids, len(test_cases))

        if not test_cases:
            return make_no_test_cases_result(
                item_id=item_id,
                sample=sample,
                platform=platform,
                requirement=requirement,
                generator_name=self.generator.name,
                item_type="sample",
                build_result_data_func=lambda **kw: self._build_framework_result_data(**kw),
            )

        # Use existing project path from workspace
        session_id = str(uuid4())

        (
            generation_result,
            process_data,
            package_size_bytes,
        ) = prepare_existing_project_data(
            workspace_path=self.workspace_path,
            item_id=item_id,
            platform=platform,
            session_id=session_id,
            generator_name=self.generator.name,
        )

        # 报告目录：workspace/{item_id}/e2e_reports
        # 多 Expo 模式下，e2e_reports 子目录前缀已包含平台（如 expo_ios_TC_LAUNCH_xxx/），
        # 不需要额外建平台子目录
        report_dir = self.workspace_path / item_id / "e2e_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        execution_result = self.executor.execute_tests(
            test_cases,
            generation_result.project_path,
            platform,
            package_name=self._extract_package_name(generation_result.project_path),
            h5_url=generation_result.h5_url,
            report_dir=report_dir,
            collect_device_logs=True,
            requires_backend=sample.requires_backend,
            install_device_id=device_id,
        )

        result = PromptResult(
            prompt_id=item_id,
            sample_id=item_id,
            sample_title=sample.title,
            platform=platform,
            generator_name=self.generator.name,
            item_type="sample",
            sample_complexity=sample.complexity if sample else "medium",
            sample_top_category=sample.top_category or (sample.app_type if sample else ""),
            requirement=requirement,
            session_id=generation_result.session_id,
            generation_success=True,
            generation_duration=generation_result.duration,
            project_path=generation_result.project_path,
            error_message=execution_result.error_message,
            error_details=list(execution_result.error_details),
            process_data=process_data,
            test_results=execution_result.test_results,
            requires_backend=sample.requires_backend,
        )
        result.result_data = self._build_framework_result_data(
            item_id=item_id,
            requirement=requirement,
            platform=platform,
            generation_result=generation_result,
            process_data=process_data,
            execution_result=execution_result,
        )

        finalize_prompt_result(
            result=result,
            execution_result=execution_result,
            process_data=process_data,
            sample=sample,
            test_cases=test_cases,
            report_dir=report_dir,
            workspace_path=self.workspace_path,
            platform=platform,
            project_path=generation_result.project_path,
            initial_package_size_bytes=package_size_bytes,
        )

        return result

    def evaluate_samples(
        self,
        samples: list[EvalSample],
        platforms: list[str] | None = None,
        max_workers: int = 1,
        sample_platform_tasks: list | None = None,
    ) -> EvalRun:
        """Evaluate a set of benchmark samples."""
        tasks = _build_task_list(sample_platform_tasks, samples, platforms)
        return self._execute_tasks(
            tasks=tasks,
            item_func=self.evaluate_sample,
            samples=samples,
            max_workers=max_workers,
            log_prefix="Sample evaluation",
        )

    def _evaluate_item(
        self,
        item_id: str,
        requirement: str,
        category: str,
        platforms: list[str],
        platform: str,
        item_type: str,
        sample: EvalSample | None = None,
        end_case: str | None = None,
        priority: str | None = None,
        requires_backend: bool = False,
        device_id: str | None = None,
    ) -> PromptResult:
        logger.info("Evaluating %s on %s with %s", item_id, platform, self.generator.name)

        test_cases = self.test_case_store.load(item_id, platform)
        
        # Filter test cases by end_case if specified
        if end_case and test_cases:
            test_cases = self._filter_test_cases_by_end_case(test_cases, end_case)
            logger.info("Filtered test cases by end_case=%s, remaining: %d", end_case, len(test_cases))
        
        # Filter test cases by priority if specified
        if priority and test_cases:
            test_cases = self._filter_test_cases_by_priority(test_cases, priority)
            logger.info("Filtered test cases by priority=%s, remaining: %d", priority, len(test_cases))
        
        if not test_cases:
            return make_no_test_cases_result(
                item_id=item_id,
                sample=sample,
                platform=platform,
                requirement=requirement,
                generator_name=self.generator.name,
                item_type=item_type,
                build_result_data_func=lambda **kw: self._build_framework_result_data(**kw),
                requires_backend=requires_backend,
            )

        session_id = str(uuid4())
        generation_result = self.generator.generate(
            requirement,
            platform,
            session_id=session_id,
        )
        process_data = self._collect_process_data(generation_result)

        if not generation_result.success:
            logger.error(
                "Generation failed for %s/%s: %s",
                item_id,
                platform,
                generation_result.error,
            )
            result = PromptResult(
                prompt_id=item_id,
                sample_id=item_id if item_type == "sample" else "",
                sample_title=sample.title if sample else "",
                platform=platform,
                generator_name=self.generator.name,
                item_type=item_type,
                sample_complexity=sample.complexity if sample else "medium",
                sample_top_category=sample.top_category or (sample.app_type if sample else ""),
                requirement=requirement,
                session_id=generation_result.session_id,
                project_id=process_data.project_id or generation_result.project_id,
                generation_success=False,
                generation_duration=generation_result.duration,
                project_path=generation_result.project_path,
                error_message=generation_result.error or process_data.error_message,
                error_details=[
                    error_from_legacy(process_data.error_type, generation_result.error or process_data.error_message, stage="generation")
                    if process_data.error_type else EvaluationError(
                        origin="generator", stage="generation", code="generation_failed",
                        message=generation_result.error or process_data.error_message,
                    )
                ],
                process_data=process_data,
                requires_backend=requires_backend,
            )
            result.result_data = self._build_framework_result_data(
                item_id=item_id,
                requirement=requirement,
                platform=platform,
                generation_result=generation_result,
                process_data=process_data,
                execution_result=None,
            )
            # 计算新顶层指标（生成失败时只需成功率和体验指标）
            result.success_rate = compute_success_rate(result)
            result.experience = compute_experience(process_data)
            return result

        logger.info(
            "App generated at %s (%.1fs)",
            generation_result.project_path,
            generation_result.duration,
        )

        # 报告目录：workspace/{item_id}/e2e_reports
        # 多 Expo 模式下，e2e_reports 子目录前缀已包含平台（如 expo_ios_TC_LAUNCH_xxx/），
        # 不需要额外建平台子目录
        report_dir = self.workspace_path / item_id / "e2e_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        execution_result = self.executor.execute_tests(
            test_cases,
            generation_result.project_path,
            platform,
            package_name=self._extract_package_name(generation_result.project_path),
            h5_url=generation_result.h5_url,
            report_dir=report_dir,
            collect_device_logs=True,
            requires_backend=requires_backend,
            install_device_id=device_id,
        )

        result = PromptResult(
            prompt_id=item_id,
            sample_id=item_id if item_type == "sample" else "",
            sample_title=sample.title if sample else "",
            platform=platform,
            generator_name=self.generator.name,
            item_type=item_type,
            sample_complexity=sample.complexity if sample else "medium",
            sample_top_category=sample.top_category or (sample.app_type if sample else ""),
            requirement=requirement,
            session_id=generation_result.session_id,
            project_id=process_data.project_id or generation_result.project_id,
            generation_success=True,
            generation_duration=generation_result.duration,
            project_path=generation_result.project_path,
            error_message=execution_result.error_message,
            error_details=list(execution_result.error_details),
            process_data=process_data,
            test_results=execution_result.test_results,
            requires_backend=requires_backend,
        )
        result.result_data = self._build_framework_result_data(
            item_id=item_id,
            requirement=requirement,
            platform=platform,
            generation_result=generation_result,
            process_data=process_data,
            execution_result=execution_result,
        )

        finalize_prompt_result(
            result=result,
            execution_result=execution_result,
            process_data=process_data,
            sample=sample,
            test_cases=test_cases,
            report_dir=report_dir,
            workspace_path=self.workspace_path,
            platform=platform,
            project_path=generation_result.project_path,
        )

        # 样本级自动分析挂钩点：内部版在此调用诊断仓的 SampleAnalyzer；
        # 开源评测仓不内置诊断能力，待 appbench-diag 提供桥接后再接入，
        # 当前静默跳过，不影响评测主流程。

        return result

    # ── Thin delegation to collectors ──────────────────────────────────

    def _collect_process_data(
        self,
        generation_result: GenerationResult,
    ) -> ProcessCollection:
        """Delegate to collectors.collect_process_data."""
        return collect_process_data(generation_result)

    def _build_framework_result_data(
        self,
        item_id: str,
        requirement: str,
        platform: str,
        generation_result: GenerationResult | None,
        process_data: ProcessCollection,
        execution_result: ExecutionResult | None,
    ):
        """Delegate to collectors.build_framework_result_data."""
        return build_framework_result_data(
            generator_name=self.generator.name,
            item_id=item_id,
            requirement=requirement,
            platform=platform,
            generation_result=generation_result,
            process_data=process_data,
            execution_result=execution_result,
        )

    def _extract_package_name(self, project_path: str) -> str | None:
        """Delegate to collectors.extract_package_name."""
        return extract_package_name(project_path)

    # ── Thin delegation to validators ──────────────────────────────────

    @staticmethod
    def _filter_test_cases_by_end_case(test_cases: list, end_case: str) -> list:
        """Delegate to validators.filter_test_cases_by_end_case."""
        return filter_test_cases_by_end_case(test_cases, end_case)

    @staticmethod
    def _filter_test_cases_by_priority(test_cases: list, priority: str) -> list:
        """Delegate to validators.filter_test_cases_by_priority."""
        return filter_test_cases_by_priority(test_cases, priority)

    # ── Per-sample score persistence ────────────────────────

    def _persist_sample_scores(
        self, sample: EvalSample, platform: str, result: PromptResult
    ) -> None:
        """样本×平台评测完成后立即持久化该样本的评测产物。

        落盘三类文件：

        * ``sample_scores.json`` — 完整 PromptResult 快照（历史行为）
        * ``evaluation.json`` / ``scores.json`` — 报告与聚合器直接消费的两个
          文件。以前它们只在整批评测正常返回后的 ``persist_all`` 里写，
          进程被中断（kill / OOM / 超时）时已完成样本的结果会停留在上一轮
          的旧文件里，报告阶段再基于旧数据重写一次，UI 就显示假 0 分。
          现在逐样本实时写入，中断也不丢数据（全量跑时 ``persist_all``
          幂等覆盖）。

        写入失败仅记录 warning，不中断评测主流程。
        """
        from datetime import datetime as _datetime

        # 同一完成事件只取一次时间戳，传给 scores.json 与 sample_scores.json，
        # 使两文件的 updated_at 严格相等：聚合器新鲜度仲裁在相等时一律选
        # 信息更完整的 scores.json，避免跨秒边界时非确定性地选中降级
        # 转换结构（丢 backend_completeness 等字段，可复发假 0 分）。
        now = _datetime.now().isoformat(timespec="seconds")

        # 逐样本实时落盘 evaluation.json + scores.json（与 persist_all 共用
        # 序列化逻辑）。放在 model_dump 之前，避免序列化失败时早退跳过。
        try:
            from ...services.evaluation import EvaluationService

            EvaluationService(self.workspace_path).persist_sample(
                result, updated_at=now,
            )
        except Exception as exc:  # noqa: BLE001 — 单样本落盘失败不阻断评测
            logger.warning(
                "Failed to immediately persist evaluation/scores for %s/%s: %s",
                sample.sample_id, platform, exc,
            )

        try:
            prompt_payload = result.model_dump(mode="json")
        except Exception as exc:  # pragma: no cover - 防御性分支
            logger.warning(
                "Skip persisting sample_scores for %s/%s: serialize failed: %s",
                sample.sample_id, platform, exc,
            )
            return

        scores_summary: dict = {
            "generation_success": getattr(result, "generation_success", False),
            "pass_count": getattr(result, "pass_count", 0),
            "total_count": getattr(result, "total_count", 0),
            "pass_rate": getattr(result, "pass_rate", 0.0),
        }
        if getattr(result, "success_rate", None) is not None:
            scores_summary["success_rate"] = getattr(
                result.success_rate, "composite_score", None
            )
        if getattr(result, "quality", None) is not None:
            scores_summary["quality"] = getattr(
                result.quality, "composite_score", None
            )
        if getattr(result, "experience", None) is not None:
            scores_summary["experience"] = getattr(
                result.experience, "composite_score", None
            )

        try:
            write_sample_scores(
                self.workspace_path,
                sample.sample_id,
                platform,
                prompt_result=prompt_payload,
                scores=scores_summary,
                updated_at=now,
            )
        except Exception as exc:
            logger.warning(
                "Failed to persist sample_scores for %s/%s: %s",
                sample.sample_id, platform, exc,
            )

    # ── Execution manifest helpers ───────────────────────────

    def _wait_until_ready(
        self, ready_check, sample: EvalSample, platform: str
    ) -> tuple[bool, str]:
        """串行模式下阻塞等待单个任务就绪。

        Returns:
            (ready, reason)：ready=True 表示就绪可执行；ready=False 表示
            门控判定失败（已标记 skipped），reason 为失败原因。
        """
        while True:
            try:
                state, reason = ready_check(sample.sample_id, platform)
            except Exception as gate_exc:  # noqa: BLE001 — 门控异常降级为继续等待
                logger.warning(
                    "ready_check 异常 (%s/%s)，继续等待: %s",
                    sample.sample_id, platform, gate_exc,
                )
                state, reason = "wait", ""
            if state == "ready":
                return True, ""
            if state == "failed":
                self._manifest_mark_gate_skipped(sample, platform, reason)
                _emit_sample_state(
                    sample.sample_id, platform, "evaluate", "failed",
                    reason or "生成未完成，跳过评测",
                )
                return False, reason
            time.sleep(_GATE_POLL_INTERVAL)

    def _make_gate_skipped_result(
        self, sample: EvalSample, platform: str, reason: str
    ) -> PromptResult:
        """构建门控失败（生成失败/超时）样本的零分评测结果。

        生成失败的样本不能从报告统计中消失：以 0 分计入成功率/功能完整性/
        稳定性/体验/美观度均值，否则汇总平均分会虚高。
        """
        message = reason or "生成未完成，跳过评测"
        result = PromptResult(
            prompt_id=sample.sample_id,
            sample_id=sample.sample_id,
            sample_title=sample.title,
            platform=platform,
            generator_name=self.generator.name,
            item_type="sample",
            sample_complexity=sample.complexity,
            sample_top_category=sample.top_category or sample.app_type,
            requirement=sample.requirement,
            generation_success=False,
            error_message=message,
            error_details=[EvaluationError(stage="generation", code="generation_not_ready", message=message)],
            requires_backend=sample.requires_backend,
        )
        result.success_rate = SuccessRateMetrics(
            initial_generation_rate=0.0,
            initial_generation_reason="应用生成失败，未能产出可运行代码",
            composite_score=0.0,
        )
        result.quality = QualityMetrics(
            usecase_completeness=0.0,
            usecase_reason="生成失败，未执行E2E测试",
            stability_score=0.0,
            stability_reason="生成失败，无法评估稳定性",
            backend_completeness=0.0 if sample.requires_backend else None,
            backend_completeness_reason=(
                "生成失败，无法评估后端服务" if sample.requires_backend else ""
            ),
            composite_score=0.0,
        )
        result.experience = ExperienceMetrics(
            duration_reason="生成失败，体验维度计 0 分",
            aesthetics_score=0.0,
            aesthetics_reason="生成失败，无界面可评估",
            composite_score=0.0,
        )
        return result

    def _safe_manifest_update(
        self,
        sample_id: str,
        platform: str,
        phase: str,
        status: str,
        *,
        error: str | None = None,
        pass_rate: float | None = None,
        retries: int = 3,
    ) -> bool:
        """带指数退避重试地将单个 phase 状态落盘到 manifest。

        落盘是辅助观测层：磁盘写入失败不应中断评测主流程，故本方法吞掉异常
        并返回 False，绝不上抛。写入失败时以指数退避重试最多 ``retries`` 次
        （sleep 1s/2s/3s），全部失败后记 logger.error 明确提示磁盘 manifest
        可能已滞后。``self.manifest is None`` 时直接返回（无需落盘）。
        """
        if self.manifest is None:
            return True
        last_exc: BaseException | None = None
        for attempt in range(1, retries + 1):
            try:
                self.manifest.update_item(
                    sample_id, platform, phase, status,
                    error=error, pass_rate=pass_rate,
                )
                return True
            except Exception as exc:  # pragma: no cover - defensive
                last_exc = exc
                time.sleep(attempt)  # 指数退避：1s / 2s / 3s
        logger.error(
            "Manifest update failed after %d retries for %s/%s phase=%s status=%s; "
            "Disk manifest may be stale. last error: %s",
            retries, sample_id, platform, phase, status, last_exc,
        )
        return False

    def _manifest_mark_gate_skipped(
        self, sample: EvalSample, platform: str, reason: str
    ) -> None:
        """门控判定失败（如生成失败/超时）时标记 evaluate 阶段为 skipped。

        若 generate 阶段仍停留在 running（生成进程异常退出、未回写终态），
        一并将其补写为 failed——依赖 manifest 的 per-phase 优先级合并
        （FAILED 高于 RUNNING、但低于 COMPLETED），不会误覆盖已完成的 generate。
        """
        if self.manifest is None:
            return
        # 生成侧超时/异常退出时 generate 可能卡在 running，补写 failed 终态。
        if (
            self.manifest.get_phase_status(sample.sample_id, platform, "generate")
            == PHASE_RUNNING
        ):
            self._safe_manifest_update(
                sample.sample_id, platform, "generate", PHASE_FAILED,
                error="生成进程异常退出（由评测侧超时检测标记）",
            )
        self._safe_manifest_update(
            sample.sample_id, platform, "evaluate", PHASE_SKIPPED,
            error=reason or "生成未完成，跳过评测",
        )

    def _manifest_should_skip(self, sample: EvalSample, platform: str) -> bool:
        """Return True if manifest already records evaluate phase as completed."""
        if self.manifest is None:
            return False
        try:
            return self.manifest.is_phase_completed(sample.sample_id, platform, "evaluate")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Manifest skip-check failed for %s/%s: %s", sample.sample_id, platform, exc)
            return False

    def _manifest_mark_running(self, sample: EvalSample, platform: str) -> None:
        self._safe_manifest_update(sample.sample_id, platform, "evaluate", PHASE_RUNNING)

    def _manifest_mark_result(
        self, sample: EvalSample, platform: str, result: PromptResult
    ) -> None:
        if self.manifest is None:
            return
        pass_rate = getattr(result, "pass_rate", None)
        if result.generation_success and not result.error_message:
            self._safe_manifest_update(
                sample.sample_id, platform, "evaluate", PHASE_COMPLETED,
                pass_rate=pass_rate,
            )
        else:
            self._safe_manifest_update(
                sample.sample_id, platform, "evaluate", PHASE_FAILED,
                error=result.error_message or "evaluation failed",
                pass_rate=pass_rate,
            )

    def _manifest_mark_failed(
        self, sample: EvalSample, platform: str, exc: BaseException
    ) -> None:
        self._safe_manifest_update(
            sample.sample_id, platform, "evaluate", PHASE_FAILED, error=str(exc),
        )

    def _emit_evaluate_state_from_result(
        self, sample: EvalSample, platform: str, result: PromptResult,
    ) -> None:
        """根据单个评测结果输出 [SAMPLE_STATE] 完成/失败行。与 _manifest_mark_result 逻辑对齐。"""
        try:
            if result.generation_success and not result.error_message:
                _emit_sample_state(sample.sample_id, platform, "evaluate", "completed")
            else:
                _emit_sample_state(
                    sample.sample_id, platform, "evaluate", "failed",
                    result.error_message or "evaluation failed",
                )
        except Exception:
            # 状态输出不应中断评测主流程
            pass
