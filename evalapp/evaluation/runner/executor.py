"""TestExecutor: build, install, and test generated apps via built-in tools."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .state import (
    CommandResult,
    ExecutionResult,
    _BUILTIN_AI_UI_TEST_DIR,
    _DEFAULT_AI_UI_TEST_DIR,
    _DEFAULT_BUILD_APP_SCRIPT,
    _DEFAULT_INSTALL_APP_SCRIPT,
    _DEFAULT_REPORTS_CACHE_ROOT,
    _resolve_ai_ui_test_entry,
    best_effort_error,
    run_command,
)
from .build_phase import (
    build_project,
)
from .install_phase import (
    install_artifact,
    uninstall_app,
    resolve_package_name,
)
from .test_phase import (
    _result_has_white_screen,
    build_and_serve_h5,
    ensure_ai_ui_test_ready,
    execute_h5_tests,
    infer_launch_status,
    collect_test_errors,
    make_failure_results,
    run_single_test,
    parse_results,
    try_parse_json_results,
    match_results,
    fallback_parse,
)
from ...config import Config
from ..metrics.collectors.device_logs import DeviceLogCollector
from ..metrics.models import ANREvent, CrashEvent
from ..results.models import TestCaseResult
from ..results.models.errors import EvaluationError, error_from_exception
from ...benchset.testcases.models import TestCase
from ...utils.logging import get_logger

logger = get_logger(__name__)

# Re-export for backward compatibility (external code may import these)
__all__ = ["TestExecutor", "CommandResult", "ExecutionResult"]


class TestExecutor:
    """Builds, installs, and tests a generated project using local skill scripts."""

    def __init__(
        self,
        config: Config | None = None,
        ai_ui_test_dir: Path | None = None,
        timeout: int | None = None,
        build_app_script: Path | None = None,
        install_app_script: Path | None = None,
        build_timeout: int | None = None,
        install_timeout: int | None = None,
        build_type: str | None = None,
        clean_build: bool | None = None,
        install_device_id: str | None = None,
        install_auto_install: bool | None = None,
        auto_uninstall: bool | None = None,
        show_browser: bool = False,
    ) -> None:
        self.config = config
        self.show_browser = show_browser
        if config is not None:
            self.ai_ui_test_dir = (
                ai_ui_test_dir
                or (Path(config.ai_ui_test.script_dir).expanduser()
                    if config.ai_ui_test.script_dir
                    else _BUILTIN_AI_UI_TEST_DIR)
            )
            self.timeout = timeout or config.ai_ui_test.timeout
            self.build_app_script = (
                build_app_script
                or Path(config.build_app.script_path).expanduser()
            )
            self.install_app_script = (
                install_app_script
                or Path(config.install_app.script_path).expanduser()
            )
            self.build_timeout = build_timeout or config.build_app.timeout
            self.install_timeout = install_timeout or config.install_app.timeout
            self.build_type = build_type or config.build_app.build_type
            self.clean_build = (
                config.build_app.clean if clean_build is None else clean_build
            )
            self.install_device_id = install_device_id or config.install_app.device_id
            self.install_auto_install = (
                config.install_app.auto_install
                if install_auto_install is None
                else install_auto_install
            )
            self.auto_uninstall = (
                config.install_app.auto_uninstall
                if auto_uninstall is None
                else auto_uninstall
            )
            self.android_output_format = config.build_app.android_output_format
            self.ios_output_format = config.build_app.ios_output_format
            self.stream_output = config.stream_output
            # 确保内置ai-ui-test已初始化
            if self.ai_ui_test_dir == _BUILTIN_AI_UI_TEST_DIR:
                ensure_ai_ui_test_ready(self.ai_ui_test_dir)
        else:
            self.ai_ui_test_dir = ai_ui_test_dir or _DEFAULT_AI_UI_TEST_DIR
            self.timeout = timeout or 300
            self.build_app_script = build_app_script or _DEFAULT_BUILD_APP_SCRIPT
            self.install_app_script = (
                install_app_script or _DEFAULT_INSTALL_APP_SCRIPT
            )
            self.build_timeout = build_timeout or 3600
            self.install_timeout = install_timeout or 300
            self.build_type = build_type or "debug"
            self.clean_build = False if clean_build is None else clean_build
            self.install_device_id = install_device_id
            self.install_auto_install = (
                False if install_auto_install is None else install_auto_install
            )
            self.auto_uninstall = True if auto_uninstall is None else auto_uninstall
            self.android_output_format = "apk"
            self.ios_output_format = "app"
            self.stream_output = False

        # 确保内置ai-ui-test已初始化（无config时也生效）
        if self.ai_ui_test_dir == _BUILTIN_AI_UI_TEST_DIR:
            ensure_ai_ui_test_ready(self.ai_ui_test_dir)

        # Per-executor snapshot root: each case's report is copied here
        # immediately after ai-ui-test returns, so that later export only
        # ever references this private location (never the shared
        # midscene_run/report folder that accumulates historical files).
        instance_tag = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_pid{os.getpid()}"
        )
        self._reports_cache_root: Path = _DEFAULT_REPORTS_CACHE_ROOT / instance_tag

    # ── Main orchestration ───────────────────────────────────────────

    def execute_tests(
        self,
        test_cases: list[TestCase],
        project_path: str,
        platform: str,
        package_name: str | None = None,
        collect_device_logs: bool = True,
        h5_url: str = "",
        report_dir: Path | None = None,
        requires_backend: bool = False,
        install_device_id: str | None = None,
    ) -> ExecutionResult:
        """Build, install, and execute test cases against a generated project.

        Args:
            test_cases: Test cases to execute.
            project_path: Path to the generated project.
            platform: Target platform (android/ios/miniprogram).
            package_name: Optional app package name.
            collect_device_logs: Whether to collect device logs for
                crash/ANR detection during E2E execution.
            h5_url: Optional H5 URL for miniprogram web testing.
                When provided, build/install are skipped and tests
                run against this URL directly.
            report_dir: Per-item isolated directory used as the cwd of the
                ai-ui-test subprocess so Midscene writes its
                ``.test_intermediates`` there instead of the shared
                ``midscene_run/report`` location. Enables per-sample
                snapshot isolation and precise report-to-sample mapping.
                Falls back to ``ai_ui_test_dir`` when omitted.
            requires_backend: Whether the sample requires real backend services.
            install_device_id: Override device ID for this execution (from DevicePool).
                When provided, takes precedence over self.install_device_id.
                Passed as a parameter instead of mutating instance state
                to ensure thread-safety under concurrent execution.
        """
        # Resolve effective device ID without mutating instance state
        effective_device_id = install_device_id if install_device_id is not None else self.install_device_id
        return self._execute_tests_impl(
            test_cases=test_cases,
            project_path=project_path,
            platform=platform,
            package_name=package_name,
            collect_device_logs=collect_device_logs,
            h5_url=h5_url,
            report_dir=report_dir,
            requires_backend=requires_backend,
            device_id=effective_device_id,
        )

    def _execute_tests_impl(
        self,
        test_cases: list[TestCase],
        project_path: str,
        platform: str,
        package_name: str | None = None,
        collect_device_logs: bool = True,
        h5_url: str = "",
        report_dir: Path | None = None,
        requires_backend: bool = False,
        device_id: str | None = None,
    ) -> ExecutionResult:
        """Internal implementation of execute_tests.

        Args:
            device_id: Effective device ID for this execution, resolved by
                execute_tests() from install_device_id override or
                self.install_device_id default.
        """
        # pylint: disable=too-many-locals,too-many-return-statements
        if not test_cases:
            logger.warning("No test cases to execute")
            return ExecutionResult()

        # H5/miniprogram URL-based testing: skip build and install
        if h5_url:
            return self._execute_h5_tests(test_cases, h5_url, platform, report_dir=report_dir, requires_backend=requires_backend)

        # miniprogram / expo_web / h5: build H5 locally and serve it, then run tests
        if platform in ("miniprogram", "expo_web", "h5"):
            return self._build_and_serve_h5(
                test_cases, project_path, platform,
                report_dir=report_dir,
                requires_backend=requires_backend,
            )

        # 预安装包模式（tusi等）：APK/IPA已在generate阶段安装到设备，跳过build和install
        package_installed_marker = Path(project_path) / ".package_installed"
        if package_installed_marker.exists():
            marker_content = package_installed_marker.read_text(encoding="utf-8").strip()
            # 兼容新旧格式：新格式为 JSON，旧格式为纯路径
            try:
                marker_data = json.loads(marker_content)
                apk_path = marker_data["apk_path"]
                installed_device_id = marker_data.get("device_id") or device_id
            except (json.JSONDecodeError, KeyError):
                apk_path = marker_content
                installed_device_id = device_id
            resolved_package_name = package_name or self._extract_package_name_from_apk(apk_path, platform)
            logger.info(
                "Pre-installed package detected, skipping build/install: %s (package=%s)",
                apk_path, resolved_package_name,
            )
            return self._run_tests_on_installed_app(
                test_cases=test_cases,
                platform=platform,
                package_name=resolved_package_name,
                artifact_path=apk_path,
                collect_device_logs=collect_device_logs,
                report_dir=report_dir,
                requires_backend=requires_backend,
                device_id=installed_device_id,
            )

        build_result = self._build_project_with_retry(project_path, platform, device_id=device_id)
        if not build_result["success"]:
            message = build_result["message"]
            logger.error("Build failed: %s", message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, f"Build failed: {message}"),
                build_status="failed",
                install_status="skipped",
                launch_status="failed",
                build_duration_ms=build_result["duration_ms"],
                error_message=message,
                error_details=[EvaluationError(stage="build", code="build_failed", message=message)],
            )

        artifact_path = build_result["artifact_path"]
        resolved_package_name = package_name or self._resolve_package_name(
            platform, project_path, artifact_path
        )

        # expo_android / expo_ios: previously skipped install because
        # `npx expo run:xxx` used to build+install. Now using separated
        # prebuild+xcodebuild/gradle which only builds, so install is needed.
        install_result = self._install_artifact_with_retry(platform, artifact_path, device_id=device_id)
        if not install_result["success"]:
            message = install_result["message"]
            logger.error("Install failed: %s", message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, f"Install failed: {message}"),
                build_status="success",
                install_status="failed",
                launch_status="failed",
                artifact_path=artifact_path,
                package_name=resolved_package_name,
                build_duration_ms=build_result["duration_ms"],
                error_message=message,
                error_details=[EvaluationError(stage="install", code="install_failed", message=message)],
            )

        # Verify ai-ui-test is available only after the app is prepared.
        index_js = _resolve_ai_ui_test_entry(self.ai_ui_test_dir)
        if not index_js.exists():
            message = f"ai-ui-test not found at {index_js}"
            logger.error(message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, message),
                build_status="success",
                install_status="success",
                launch_status="unknown",
                artifact_path=artifact_path,
                package_name=resolved_package_name,
                build_duration_ms=build_result["duration_ms"],
                error_message=message,
                error_details=[EvaluationError(origin="environment", stage="test", code="test_tool_unavailable", message=message)],
            )

        logger.info(
            "Executing %s tests on %s (%s)",
            len(test_cases),
            project_path,
            platform,
        )

        # Start device log collection for crash/ANR detection
        log_collector: DeviceLogCollector | None = None
        if collect_device_logs:
            log_collector = DeviceLogCollector()
            log_collector.start(platform, package_name=resolved_package_name, device_id=device_id)

        results: list[TestCaseResult] = []
        max_parallel_tc = self._resolve_max_parallel_tc()

        # Detect TC_LAUNCH presence for short-circuit logic
        tc_launch_idx = next(
            (i for i, tc in enumerate(test_cases) if tc.id == "TC_LAUNCH"),
            None,
        )

        if max_parallel_tc > 1 and len(test_cases) > 1 and tc_launch_idx is None:
            # 并发执行 TC：同一设备上允许多个 TC 同时跳转，需业务侧并行友好。
            # 为保证输出顺序与串行一致，按 test_cases 原顺序回填。
            actual_workers = min(max_parallel_tc, len(test_cases))
            logger.info(
                "Executing TCs concurrently: max_parallel_tc=%d, actual=%d",
                max_parallel_tc, actual_workers,
            )
            results = [None] * len(test_cases)  # type: ignore[list-item]
            with ThreadPoolExecutor(
                max_workers=actual_workers,
                thread_name_prefix="tc",
            ) as tc_pool:
                futures = {
                    tc_pool.submit(
                        self._run_single_test,
                        tc, platform, resolved_package_name,
                        report_dir=report_dir, device_id=device_id,
                    ): (i, tc)
                    for i, tc in enumerate(test_cases)
                }
                completed = 0
                for fut in as_completed(futures):
                    idx, tc = futures[fut]
                    completed += 1
                    try:
                        result = fut.result()
                    except Exception as exc:
                        logger.error("TC %s raised: %s", tc.id, exc, exc_info=True)
                        result = make_failure_results([tc], f"TC raised: {exc}")[0]
                        result.error_details = [error_from_exception(exc, stage="test")]
                    results[idx] = result
                    logger.info(
                        "  [%s/%s] %s: %s -> %s: %s",
                        completed, len(test_cases), tc.id, tc.name,
                        result.status, result.details[:120],
                    )
        elif max_parallel_tc > 1 and len(test_cases) > 1 and tc_launch_idx is not None:
            # 并发模式 + TC_LAUNCH 存在：先单独执行 TC_LAUNCH，
            # 成功后再并发剩余用例，失败则跳过所有剩余用例。
            launch_tc = test_cases[tc_launch_idx]
            logger.info(
                "TC_LAUNCH detected (idx=%d), executing it first before concurrent TCs",
                tc_launch_idx,
            )
            launch_result = self._run_single_test(
                launch_tc, platform, resolved_package_name,
                report_dir=report_dir, device_id=device_id,
            )

            # Pre-allocate results list
            results = [None] * len(test_cases)  # type: ignore[list-item]
            results[tc_launch_idx] = launch_result

            if not launch_result.passed or _result_has_white_screen(launch_result):
                # TC_LAUNCH failed or white-screen detected: skip all remaining cases
                logger.warning(
                    "TC_LAUNCH failed -> skipping %d remaining test cases",
                    len(test_cases) - 1,
                )
                for i, tc in enumerate(test_cases):
                    if i == tc_launch_idx:
                        continue
                    results[i] = TestCaseResult(
                        test_case_id=tc.id,
                        passed=False,
                        status="SKIPPED",
                        details="Skipped due to TC_LAUNCH failure (app failed to launch)",
                    )
            else:
                # TC_LAUNCH passed: run remaining cases concurrently
                remaining = [(i, tc) for i, tc in enumerate(test_cases) if i != tc_launch_idx]
                actual_workers = min(max_parallel_tc, len(remaining))
                logger.info(
                    "TC_LAUNCH passed, executing %d remaining TCs concurrently (workers=%d)",
                    len(remaining), actual_workers,
                )
                with ThreadPoolExecutor(
                    max_workers=actual_workers,
                    thread_name_prefix="tc",
                ) as tc_pool:
                    futures = {
                        tc_pool.submit(
                            self._run_single_test,
                            tc, platform, resolved_package_name,
                            report_dir=report_dir, device_id=device_id,
                        ): (i, tc)
                        for i, tc in remaining
                    }
                    completed = 0
                    for fut in as_completed(futures):
                        idx, tc = futures[fut]
                        completed += 1
                        try:
                            result = fut.result()
                        except Exception as exc:
                            logger.error("TC %s raised: %s", tc.id, exc, exc_info=True)
                            result = make_failure_results([tc], f"TC raised: {exc}")[0]
                            result.error_details = [error_from_exception(exc, stage="test")]
                        results[idx] = result
                        logger.info(
                            "  [%s/%s] %s: %s -> %s: %s",
                            completed, len(remaining), tc.id, tc.name,
                            result.status, result.details[:120],
                        )
        else:
            for i, tc in enumerate(test_cases):
                logger.info("  [%s/%s] %s: %s", i + 1, len(test_cases), tc.id, tc.name)
                result = self._run_single_test(tc, platform, resolved_package_name, report_dir=report_dir, device_id=device_id)
                results.append(result)
                logger.info("    -> %s: %s", result.status, result.details[:120])

                # TC_LAUNCH short-circuit: if launch check fails or white screen is
                # detected, skip remaining cases (honour the design intent that a
                # white screen means "don't proceed further").
                if tc.id == "TC_LAUNCH" and (not result.passed or _result_has_white_screen(result)):
                    remaining_count = len(test_cases) - (i + 1)
                    if remaining_count > 0:
                        logger.warning(
                            "TC_LAUNCH failed -> skipping %d remaining test cases",
                            remaining_count,
                        )
                        for skip_tc in test_cases[i + 1:]:
                            results.append(TestCaseResult(
                                test_case_id=skip_tc.id,
                                passed=False,
                                status="SKIPPED",
                                details="Skipped due to TC_LAUNCH failure (app failed to launch)",
                            ))
                    break

        # Stop log collection and extract crash/ANR events
        crash_events: list[CrashEvent] = []
        anr_events: list[ANREvent] = []
        if log_collector is not None:
            crash_events, anr_events = log_collector.stop()
            if crash_events:
                logger.warning(
                    "Detected %d crash event(s) during E2E execution",
                    len(crash_events),
                )
            if anr_events:
                logger.warning(
                    "Detected %d ANR event(s) during E2E execution",
                    len(anr_events),
                )

            # 保存原始日志文件
            if report_dir:
                log_file = report_dir / 'device.log'
                try:
                    log_collector.save_raw_logs(log_file)
                except Exception:
                    logger.warning("Failed to save raw logs to %s", log_file, exc_info=True)

        passed = sum(1 for result in results if result.passed)
        logger.info("Test execution complete: %s/%s passed", passed, len(results))

        # 卸载应用（可配置，默认开启）
        if self.auto_uninstall and platform in ("ios", "android", "expo_ios", "expo_android") and resolved_package_name:
            uninstall_result = uninstall_app(platform, resolved_package_name, device_id)
            if not uninstall_result["success"]:
                logger.warning("Post-test app uninstall failed: %s", uninstall_result["message"])

        return ExecutionResult(
            test_results=results,
            error_details=collect_test_errors(results),
            build_status="success",
            install_status="success",
            launch_status=infer_launch_status(results),
            artifact_path=artifact_path,
            package_name=resolved_package_name,
            build_duration_ms=build_result["duration_ms"],
            crash_events=crash_events,
            anr_events=anr_events,
        )

    # ── Phase delegation methods ──────────────────────────────────────

    def _build_project(self, project_path: str, platform: str, device_id: str | None = None) -> dict[str, object]:
        """Delegate to build_phase.build_project."""
        return build_project(
            build_app_script=self.build_app_script,
            project_path=project_path,
            platform=platform,
            build_type=self.build_type,
            clean_build=self.clean_build,
            android_output_format=self.android_output_format,
            ios_output_format=self.ios_output_format,
            build_timeout=self.build_timeout,
            stream_output=self.stream_output,
            device_id=device_id,
        )

    def _install_artifact(self, platform: str, artifact_path: str, device_id: str | None = None) -> dict[str, object]:
        """Delegate to install_phase.install_artifact."""
        return install_artifact(
            install_app_script=self.install_app_script,
            platform=platform,
            artifact_path=artifact_path,
            install_device_id=device_id if device_id is not None else self.install_device_id,
            install_auto_install=self.install_auto_install,
            install_timeout=self.install_timeout,
            stream_output=self.stream_output,
        )

    def _build_project_with_retry(self, project_path: str, platform: str, device_id: str | None = None) -> dict[str, object]:
        """Build project with configurable retry on failure."""
        max_retries = 0
        if self.config is not None:
            max_retries = self.config.build_app.max_retries

        result = self._build_project(project_path, platform, device_id=device_id)
        if result["success"] or max_retries <= 0:
            return result

        for attempt in range(1, max_retries + 1):
            logger.warning(
                "Build failed, retrying (attempt %d/%d): %s",
                attempt, max_retries, result["message"],
            )
            result = self._build_project(project_path, platform, device_id=device_id)
            if result["success"]:
                logger.info("Build retry succeeded on attempt %d", attempt)
                break
        return result

    def _install_artifact_with_retry(
        self, platform: str, artifact_path: str, device_id: str | None = None,
    ) -> dict[str, object]:
        """Install artifact with configurable retry on failure."""
        max_retries = 0
        if self.config is not None:
            max_retries = self.config.install_app.max_retries

        result = self._install_artifact(platform, artifact_path, device_id=device_id)
        if result["success"] or max_retries <= 0:
            return result

        for attempt in range(1, max_retries + 1):
            logger.warning(
                "Install failed, retrying (attempt %d/%d): %s",
                attempt, max_retries, result["message"],
            )
            result = self._install_artifact(platform, artifact_path, device_id=device_id)
            if result["success"]:
                logger.info("Install retry succeeded on attempt %d", attempt)
                break
        return result

    def _extract_package_name_from_apk(
        self, artifact_path: str, platform: str
    ) -> str | None:
        """从已安装的 APK/IPA 中提取包名。

        Android: 使用 aapt dump badging 提取 package name。
        iOS: 使用 plutil 读取 Info.plist 提取 bundle identifier。
        """
        if platform in ("android", "expo_android"):
            try:
                result = subprocess.run(
                    ["aapt", "dump", "badging", artifact_path],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    import re
                    match = re.search(r"package:\s*name='([^']+)'", result.stdout)
                    if match:
                        return match.group(1)
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                logger.warning("aapt extract package name failed: %s", e)
        elif platform in ("ios", "expo_ios"):
            # iOS .ipa/.app - 尝试从已安装列表中查找
            try:
                result = subprocess.run(
                    ["xcrun", "simctl", "listapps", "booted"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and "CFBundleIdentifier" in result.stdout:
                    logger.debug("iOS app list available for package extraction")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        return None

    def _run_tests_on_installed_app(
        self,
        test_cases: list,
        platform: str,
        package_name: str | None,
        artifact_path: str,
        collect_device_logs: bool = True,
        report_dir: Path | None = None,
        requires_backend: bool = False,
        device_id: str | None = None,
    ) -> "ExecutionResult":
        """对已安装到设备的应用直接执行 E2E 测试（跳过 build/install）。"""
        # Verify ai-ui-test is available
        index_js = _resolve_ai_ui_test_entry(self.ai_ui_test_dir)
        if not index_js.exists():
            message = f"ai-ui-test not found at {index_js}"
            logger.error(message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, message),
                build_status="skipped",
                install_status="skipped",
                launch_status="unknown",
                artifact_path=artifact_path,
                package_name=package_name,
                error_message=message,
                error_details=[EvaluationError(origin="environment", stage="test", code="test_tool_unavailable", message=message)],
            )

        logger.info(
            "Executing %d tests on pre-installed app (%s, package=%s)",
            len(test_cases), platform, package_name,
        )

        # Start device log collection
        log_collector: DeviceLogCollector | None = None
        if collect_device_logs:
            log_collector = DeviceLogCollector()
            log_collector.start(platform, package_name=package_name, device_id=device_id)

        results: list = []
        max_parallel_tc = self._resolve_max_parallel_tc()

        tc_launch_idx = next(
            (i for i, tc in enumerate(test_cases) if tc.id == "TC_LAUNCH"),
            None,
        )

        if max_parallel_tc > 1 and len(test_cases) > 1 and tc_launch_idx is None:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            actual_workers = min(max_parallel_tc, len(test_cases))
            results = [None] * len(test_cases)
            with ThreadPoolExecutor(max_workers=actual_workers) as tc_pool:
                futures = {
                    tc_pool.submit(
                        self._run_single_test,
                        tc, platform, package_name,
                        report_dir=report_dir, device_id=device_id,
                    ): (i, tc)
                    for i, tc in enumerate(test_cases)
                }
                for fut in as_completed(futures):
                    idx, tc = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as exc:
                        logger.error("TC %s raised: %s", tc.id, exc)
                        result = make_failure_results([tc], f"TC raised: {exc}")[0]
                        result.error_details = [error_from_exception(exc, stage="test")]
                    results[idx] = result
        else:
            for i, tc in enumerate(test_cases):
                result = self._run_single_test(
                    tc, platform, package_name,
                    report_dir=report_dir, device_id=device_id,
                )
                results.append(result)
                # TC_LAUNCH 失败则短路其余
                if tc_launch_idx is not None and i == tc_launch_idx and not result.passed:
                    logger.warning("TC_LAUNCH failed, short-circuiting remaining tests")
                    remaining = test_cases[i + 1:]
                    if remaining:
                        results.extend(make_failure_results(remaining, "Skipped: TC_LAUNCH failed"))
                    break

        # Stop log collector and extract crash/ANR events
        crash_events: list[CrashEvent] = []
        anr_events: list[ANREvent] = []
        if log_collector:
            crash_events, anr_events = log_collector.stop()
            if crash_events:
                logger.warning(
                    "Detected %d crash event(s) during E2E execution",
                    len(crash_events),
                )
            if anr_events:
                logger.warning(
                    "Detected %d ANR event(s) during E2E execution",
                    len(anr_events),
                )
            # 保存原始日志文件
            if report_dir:
                log_file = report_dir / 'device.log'
                try:
                    log_collector.save_raw_logs(log_file)
                except Exception:
                    logger.warning("Failed to save raw logs to %s", log_file, exc_info=True)

        return ExecutionResult(
            test_results=results,
            error_details=collect_test_errors(results),
            build_status="skipped",
            install_status="skipped",
            launch_status=infer_launch_status(results) if results else "unknown",
            artifact_path=artifact_path,
            package_name=package_name,
            crash_events=crash_events,
            anr_events=anr_events,
        )

    def _resolve_package_name(
        self,
        platform: str,
        project_path: str,
        artifact_path: str,
    ) -> str | None:
        """Delegate to install_phase.resolve_package_name."""
        return resolve_package_name(platform, project_path, artifact_path)

    def _resolve_max_parallel_tc(self) -> int:
        """解析当前生效的 TC 并发数。

        优先级：self.config.ai_ui_test.max_parallel_tc → 默认 1。
        返回值保证 >= 1。
        """
        if self.config is not None:
            value = getattr(self.config.ai_ui_test, "max_parallel_tc", 1)
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return 1
        return 1

    def _run_single_test(
        self,
        tc: TestCase,
        platform: str,
        package_name: str | None = None,
        h5_url: str = "",
        report_dir: Path | None = None,
        requires_backend: bool = False,
        device_id: str | None = None,
    ) -> TestCaseResult:
        """Run a single test case with automatic retry on failure."""
        max_retries = 0
        if self.config is not None:
            max_retries = self.config.ai_ui_test.max_retries

        result = run_single_test(
            tc=tc,
            platform=platform,
            ai_ui_test_dir=self.ai_ui_test_dir,
            timeout=self.timeout,
            package_name=package_name,
            h5_url=h5_url,
            report_dir=report_dir,
            config=self.config,
            stream_output=self.stream_output,
            reports_cache_root=self._reports_cache_root,
            requires_backend=requires_backend,
            device_id=device_id if device_id is not None else self.install_device_id,
            show_browser=self.show_browser,
        )

        # Retry on failure (handles transient errors like timeout, device issues)
        if not result.passed and max_retries > 0:
            for attempt in range(1, max_retries + 1):
                logger.info(
                    "    Retrying %s (attempt %d/%d)",
                    tc.id, attempt, max_retries,
                )
                result = run_single_test(
                    tc=tc,
                    platform=platform,
                    ai_ui_test_dir=self.ai_ui_test_dir,
                    timeout=self.timeout,
                    package_name=package_name,
                    h5_url=h5_url,
                    report_dir=report_dir,
                    config=self.config,
                    stream_output=self.stream_output,
                    reports_cache_root=self._reports_cache_root,
                    requires_backend=requires_backend,
                    device_id=device_id if device_id is not None else self.install_device_id,
                    show_browser=self.show_browser,
                )
                if result.passed:
                    logger.info("    Retry succeeded for %s", tc.id)
                    break

        return result

    def _execute_h5_tests(
        self,
        test_cases: list[TestCase],
        h5_url: str,
        platform: str,
        report_dir: Path | None = None,
        requires_backend: bool = False,
    ) -> ExecutionResult:
        """Delegate to test_phase.execute_h5_tests."""
        return execute_h5_tests(
            test_cases=test_cases,
            h5_url=h5_url,
            platform=platform,
            ai_ui_test_dir=self.ai_ui_test_dir,
            timeout=self.timeout,
            config=self.config,
            stream_output=self.stream_output,
            reports_cache_root=self._reports_cache_root,
            report_dir=report_dir,
            requires_backend=requires_backend,
            show_browser=self.show_browser,
        )

    def _build_and_serve_h5(
        self,
        test_cases: list[TestCase],
        project_path: str,
        platform: str,
        report_dir: Path | None = None,
        requires_backend: bool = False,
    ) -> ExecutionResult:
        """Delegate to test_phase.build_and_serve_h5."""
        return build_and_serve_h5(
            test_cases=test_cases,
            project_path=project_path,
            platform=platform,
            build_timeout=self.build_timeout,
            stream_output=self.stream_output,
            ai_ui_test_dir=self.ai_ui_test_dir,
            timeout=self.timeout,
            config=self.config,
            reports_cache_root=self._reports_cache_root,
            report_dir=report_dir,
            requires_backend=requires_backend,
            show_browser=self.show_browser,
        )

    # ── Utility methods (kept for backward compatibility) ────────────

    def _make_failure_results(
        self,
        test_cases: list[TestCase],
        details: str,
    ) -> list[TestCaseResult]:
        return make_failure_results(test_cases, details)

    def _infer_launch_status(self, results: list[TestCaseResult]) -> str:
        return infer_launch_status(results)

    def _run_command(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int,
        prefix: str = "cmd",
    ) -> CommandResult:
        return run_command(
            cmd,
            cwd=cwd,
            timeout=timeout,
            stream_output=self.stream_output,
            prefix=prefix,
        )

    def _best_effort_error(self, command_result: CommandResult) -> str:
        return best_effort_error(command_result)

    # ── Legacy parse methods (kept for backward compatibility) ───────

    def _parse_results(
        self,
        test_cases: list[TestCase],
        raw_output: str,
    ) -> list[TestCaseResult]:
        return parse_results(test_cases, raw_output)

    def _try_parse_json_results(self, output: str) -> list[dict] | None:
        return try_parse_json_results(output)

    def _match_results(
        self,
        test_cases: list[TestCase],
        parsed: list[dict],
    ) -> list[TestCaseResult]:
        return match_results(test_cases, parsed)

    def _fallback_parse(
        self,
        test_cases: list[TestCase],
        raw_output: str,
    ) -> list[TestCaseResult]:
        return fallback_parse(test_cases, raw_output)

