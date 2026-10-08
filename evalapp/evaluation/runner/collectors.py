"""Result collection: gather test results, screenshots, logs, and process data."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...generators import GenerationResult
from ..results.models.errors import EvaluationError, error_from_legacy
from ..results.models import (
    DurationMetrics,
    E2EResult,
    FrameworkResultCollection,
    ProcessCollection,
    PromptResult,
)
from ...utils.logging import get_logger
from ...utils.project_discovery import find_project_root
from .state import ExecutionResult

logger = get_logger(__name__)

# Platforms that should not be included in package size statistics.
# - expo_web: artifact_path is often unset, causing fallback to the entire
#   project directory (including node_modules), producing hundreds of MB / GB.
# - miniprogram: no native artifact to measure.
PACKAGE_SIZE_EXCLUDED_PLATFORMS = {"expo_web", "miniprogram"}

# 映射“从 e2e_reports/ HTML 中提取截图”这一回退逻辑是否已警告过。
# 按项目统一截图来源为 workspace/{sample_id}/screenshots/，
# e2e_reports/ HTML 提取仅作为过渡期内的回退，首次触发时警告一次。
_E2E_SCREENSHOT_FALLBACK_WARNED: bool = False


def collect_process_data(generation_result: GenerationResult) -> ProcessCollection:
    """Collect process data from generation result.

    Records total duration from generation timing and token data from metadata.
    """
    total_ms = (
        int(generation_result.duration * 1000)
        if generation_result.duration
        else None
    )
    # Read token data from generation_result.metadata
    meta = generation_result.metadata or {}
    return ProcessCollection(
        collector_name="none",
        session_id=generation_result.session_id,
        project_id=generation_result.project_id,
        durations=DurationMetrics(total_ms=total_ms),
        token_input=meta.get("token_input") or None,
        token_output=meta.get("token_output") or None,
        token_total=meta.get("token_total") or None,
    )


def build_framework_result_data(
    *,
    generator_name: str,
    item_id: str,
    requirement: str,
    platform: str,
    generation_result: GenerationResult | None,
    process_data: ProcessCollection,
    execution_result: ExecutionResult | None,
) -> FrameworkResultCollection:
    """Build FrameworkResultCollection from generation and execution results."""
    test_results = execution_result.test_results if execution_result else []
    total_count = len(test_results)
    pass_count = sum(1 for result in test_results if result.passed)
    build_status = (
        execution_result.build_status if execution_result else ""
    )
    install_status = execution_result.install_status if execution_result else "unknown"
    launch_status = execution_result.launch_status if execution_result else "unknown"
    duration_build_ms = (
        execution_result.build_duration_ms if execution_result else None
    ) or process_data.durations.build_ms
    artifact_path = (
        execution_result.artifact_path if execution_result else ""
    ) or (generation_result.project_path if generation_result else "")

    result_data = FrameworkResultCollection(
        task_id=item_id,
        system_id=generator_name,
        requirement=requirement,
        platform=platform,
        generation_status="success"
        if generation_result and generation_result.success
        else "failed"
        if generation_result
        else "unknown",
        build_status=build_status or (
            "success" if generation_result and generation_result.success else "unknown"
        ),
        install_status=install_status,
        launch_status=launch_status or ("success" if test_results else "unknown"),
        duration_build_ms=duration_build_ms,
        duration_total_ms=result_total_duration_ms(generation_result, process_data),
        artifact_path=artifact_path,
        h5_url=generation_result.h5_url if generation_result else "",
        e2e_result=E2EResult(
            pass_count=pass_count,
            fail_count=total_count - pass_count,
            total_count=total_count,
            pass_rate=pass_count / total_count if total_count else 0.0,
            test_results=test_results,
        ),
    )

    if generation_result and not generation_result.success:
        result_data.launch_status = "failed"

    return result_data


def result_total_duration_ms(
    generation_result: GenerationResult | None,
    process_data: ProcessCollection,
) -> int | None:
    """Compute total duration in milliseconds from generation and process data."""
    if process_data.durations.total_ms is not None:
        return process_data.durations.total_ms
    if generation_result is None:
        return None
    return int(generation_result.duration * 1000)


def normalize_status(value: str) -> str:
    """Normalize a status string to a canonical form."""
    normalized = value.strip().lower()
    if not normalized:
        return ""
    if normalized == "completed":
        return "success"
    if normalized == "failed":
        return "failed"
    if normalized == "skipped":
        return "skipped"
    return normalized


def compute_package_size(artifact_path: str, platform: str) -> int:
    """Compute package size in bytes from artifact path.

    Args:
        artifact_path: Path to the artifact (file or directory).
        platform: Platform name (android/ios/miniprogram/expo_web/...).

    Returns:
        Package size in bytes, or 0 if artifact not found or platform excluded.
    """
    if platform in PACKAGE_SIZE_EXCLUDED_PLATFORMS:
        return 0

    if not artifact_path:
        return 0

    p = Path(artifact_path)
    if not p.exists():
        return 0

    try:
        if p.is_file():
            return p.stat().st_size
        elif p.is_dir():
            # iOS .app is a directory; sum all file sizes recursively
            return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())
    except (OSError, PermissionError) as e:
        logger.warning(
            "compute_package_size failed for %s (platform=%s): %s",
            artifact_path, platform, e,
        )

    return 0


def extract_package_name(project_path: str) -> str | None:
    """Extract Android applicationId from build.gradle.kts."""
    build_gradle = Path(project_path) / "app" / "build.gradle.kts"
    if not build_gradle.exists():
        build_gradle = Path(project_path) / "app" / "build.gradle"
    if not build_gradle.exists():
        return None
    try:
        content = build_gradle.read_text()
        match = re.search(r'applicationId\s*=\s*"([^"]+)"', content)
        if match:
            return match.group(1)
    except (OSError, UnicodeDecodeError) as e:
        logger.debug(
            "extract_package_name: failed to read %s: %s",
            build_gradle, e,
        )
    return None


def resolve_e2e_report_path(
    report_dir: Path | None,
    workspace_path: Path,
) -> str | None:
    """Resolve E2E report HTML path relative to workspace_path.

    Search priority:
    1. report.html at report_dir root
    2. .test_intermediates/ai-ui-test/*/report.html (newest)
    3. playwright-*.html under report_dir (recursive, latest by name)

    Returns relative path string, or None if not found.
    """
    if not report_dir or not report_dir.exists():
        return None

    # Priority 1: report.html at root
    report_html = report_dir / "report.html"
    if report_html.exists():
        return str(report_html.relative_to(workspace_path))

    # Priority 2: .test_intermediates/ai-ui-test/*/report.html
    test_intermediates_dir = report_dir / ".test_intermediates" / "ai-ui-test"
    if test_intermediates_dir.exists():
        html_files = list(test_intermediates_dir.glob("*/report.html"))
        if html_files:
            html_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            logger.info("Found E2E report in .test_intermediates: %s", html_files[0])
            return str(html_files[0].relative_to(workspace_path))

    # Priority 3: fallback to playwright-*.html under report_dir (recursive)
    # Midscene generates reports as playwright-YYYYMMDD_HHMMSS-HASH.html
    html_files = list(report_dir.rglob("playwright-*.html"))
    if html_files:
        # Sort by filename (timestamp-encoded) and pick the latest
        html_files.sort(key=lambda p: p.name)
        logger.info("Found E2E report (playwright): %s", html_files[-1])
        return str(html_files[-1].relative_to(workspace_path))

    return None


def _read_generation_h5_url(workspace_path: Path, item_id: str) -> str:
    """产物直评模式 h5_url 兜底：读 generation.json（磁盘布局见 _artifact_prep.py）。"""
    gen_path = workspace_path / item_id / "generation.json"
    if not gen_path.exists():
        return ""
    try:
        with open(gen_path, encoding="utf-8") as f:
            return str(json.load(f).get("h5_url", "") or "")
    except Exception as exc:
        logger.warning("读取 %s 的 generation.json h5_url 失败: %s", item_id, exc)
        return ""


def read_sample_report_data(
    workspace_path: Path,
    item_id: str,
    platform: str,
) -> dict:
    """Read duration, token, package size and h5_url data from sample_report.json.

    Returns dict with keys:
        duration_ms, duration_sec, token_input, token_output, token_total,
        package_size_bytes, h5_url
    """
    defaults = {
        "duration_ms": 0,
        "duration_sec": 0.0,
        "token_input": 0,
        "token_output": 0,
        "token_total": 0,
        "package_size_bytes": 0,
        "h5_url": "",
    }

    sample_report_path = workspace_path / item_id / "sample_report.json"
    if not sample_report_path.exists():
        # 产物直评模式无 sample_report.json，从 generation.json 兜底读 h5_url
        defaults["h5_url"] = _read_generation_h5_url(workspace_path, item_id)
        return defaults

    try:
        with open(sample_report_path, encoding="utf-8") as f:
            sr = json.load(f)

        # Duration: prefer platform-specific, fallback to sample-level
        duration_ms = 0
        platform_durations = sr.get("platform_durations", {})
        if isinstance(platform_durations, dict) and platform in platform_durations:
            platform_val = platform_durations[platform]
            if isinstance(platform_val, dict):
                duration_ms = int(platform_val.get("duration_ms", 0) or 0)
            elif isinstance(platform_val, (int, float)):
                duration_ms = int(platform_val)
        else:
            dur = sr.get("duration_ms") or 0
            if dur:
                duration_ms = int(dur)

        # Tokens: prefer platform-specific, fallback to sample-level
        token_input = token_output = token_total = 0
        platform_tokens = sr.get("platform_tokens", {})
        if isinstance(platform_tokens, dict) and platform in platform_tokens:
            pt = platform_tokens[platform]
            token_input = pt.get("token_input", 0) or 0
            token_output = pt.get("token_output", 0) or 0
            token_total = pt.get("token_total", 0) or 0
        else:
            token_input = sr.get("token_input", 0) or 0
            token_output = sr.get("token_output", 0) or 0
            token_total = sr.get("token_total", 0) or 0

        # Package size: prefer platform-specific (excluded platforms skip this)
        package_size_bytes = 0
        if platform not in PACKAGE_SIZE_EXCLUDED_PLATFORMS:
            platform_package_sizes = sr.get("platform_package_sizes", {})
            if isinstance(platform_package_sizes, dict) and platform in platform_package_sizes:
                package_size_bytes = int(platform_package_sizes[platform]) or 0

        # h5_url: prefer platform-specific, fallback to sample-level
        h5_url = ""
        platform_h5_urls = sr.get("platform_h5_urls", {})
        if isinstance(platform_h5_urls, dict) and platform in platform_h5_urls:
            h5_url = str(platform_h5_urls[platform] or "")
        else:
            h5_url = str(sr.get("h5_url", "") or "")
        if not h5_url:
            # 产物直评模式（--url）的 URL 写在 generation.json，见 _artifact_prep.py
            h5_url = _read_generation_h5_url(workspace_path, item_id)

        return {
            "duration_ms": duration_ms,
            "duration_sec": round(duration_ms / 1000, 2),
            "token_input": token_input,
            "token_output": token_output,
            "token_total": token_total,
            "package_size_bytes": package_size_bytes,
            "h5_url": h5_url,
        }
    except Exception as exc:
        logger.debug("Failed to read sample_report.json for %s: %s", item_id, exc)
        return defaults


def make_no_test_cases_result(
    *,
    item_id: str,
    sample,
    platform: str,
    requirement: str,
    generator_name: str,
    item_type: str = "sample",
    requires_backend: bool = False,
    build_result_data_func=None,
) -> PromptResult:
    """Create a PromptResult for the case where no test cases are found."""
    logger.warning(
        "No test cases found for %s/%s. Run the design command first.",
        item_id,
        platform,
    )
    result = PromptResult(
        prompt_id=item_id,
        sample_id=item_id if item_type == "sample" else "",
        sample_title=sample.title if sample else "",
        platform=platform,
        generator_name=generator_name,
        item_type=item_type,
        sample_complexity=sample.complexity if sample else "medium",
        sample_top_category=sample.top_category or (sample.app_type if sample else ""),
        requirement=requirement,
        generation_success=False,
        error_message="No test cases found",
        error_details=[EvaluationError(origin="evaluator", stage="test", code="no_test_cases", message="No test cases found")],
        requires_backend=requires_backend,
    )
    if build_result_data_func is not None:
        result.result_data = build_result_data_func(
            item_id=item_id,
            requirement=requirement,
            platform=platform,
            generation_result=None,
            process_data=result.process_data,
            execution_result=None,
        )
    return result


def prepare_existing_project_data(
    *,
    workspace_path: Path,
    item_id: str,
    platform: str,
    session_id: str,
    generator_name: str,
) -> tuple:
    """Prepare GenerationResult and ProcessCollection for an existing project.

    Reads sample_report.json for duration/token/package data.
    Automatically discovers the actual project root when the code is nested
    inside subdirectories (e.g. ``shell_project/``).

    多 Expo 模式下，expo_ios/expo_android 共享 generated_projects/expo/ 目录，
    需要从共享目录解析 project_path。

    Returns:
        (generation_result, process_data, package_size_bytes)
    """
    from ...workspace.paths import is_expo_platform, resolve_generated_project_dir

    sr_data = read_sample_report_data(workspace_path, item_id, platform)

    # 对所有 expo_* 平台统一走 resolve_generated_project_dir，
    # 利用其内置的三端 fallback 链（expo_web/expo_android/expo_ios 共享同一份
    # React Native 源码）。覆盖以下场景：
    #   1. 多 Expo 模式：使用共享的 generated_projects/expo/ 目录；
    #   2. 单 Expo 工作区被复用为其它 expo 平台评测（如 meta.json 是
    #      ["expo_web"]，但 exec_plan 跑 expo_android/expo_ios），自动
    #      fallback 到已生成的 expo_web/ 目录。
    if is_expo_platform(platform):
        base_project_dir = resolve_generated_project_dir(
            workspace_path, item_id, platform
        )
    else:
        base_project_dir = workspace_path / item_id / "generated_projects" / platform

    found_root = find_project_root(base_project_dir, platform)
    if found_root:
        project_path = str(found_root)
        if found_root != base_project_dir:
            logger.info(
                "Project root discovered for %s/%s: %s",
                item_id, platform, project_path,
            )
    else:
        project_path = str(base_project_dir)
        logger.warning(
            "No platform markers found for %s/%s under %s, using base path",
            item_id, platform, base_project_dir,
        )

    generation_result = GenerationResult(
        success=True,
        session_id=session_id,
        project_path=project_path,
        platform=platform,
        duration=sr_data["duration_sec"],
        generator_name=generator_name,
        h5_url=sr_data["h5_url"],
    )
    process_data = ProcessCollection(
        collector_name="none",
        session_id=session_id,
        durations=DurationMetrics(
            total_ms=sr_data["duration_ms"] if sr_data["duration_ms"] > 0 else None,
        ),
        token_input=sr_data["token_input"] or None,
        token_output=sr_data["token_output"] or None,
        token_total=sr_data["token_total"] or None,
    )
    return (
        generation_result,
        process_data,
        sr_data["package_size_bytes"],
    )


def _extract_and_save_screenshots(
    report_dir: Path,
    sample_id: str,
    platform: str,
    workspace_path: Path,
) -> None:
    """确保 workspace/{sample_id}/screenshots/ 目录存在有效截图。

    首选路径：workspace/{sample_id}/screenshots/（项目统一来源）。
    若该目录下已有截图文件则直接返回，不再从 HTML 抽取。

    .. deprecated::
        从 ``e2e_reports/`` 下的 HTML 报告中抽取截图的回退逻辑已废弃，
        后续版本计划移除。请确保上游测试框架（ai-ui-test/Midscene）直接将
        截图输出到 ``workspace/{sample_id}/screenshots/`` 。
    """
    global _E2E_SCREENSHOT_FALLBACK_WARNED

    screenshots_dir = workspace_path / sample_id / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    # 优先使用统一来源：screenshots/ 目录。
    # 若该目录下已存在任何图片文件，认为上游已直接产出，跳过回退抽取。
    if any(screenshots_dir.iterdir()):
        return

    if not report_dir or not report_dir.exists():
        return

    try:
        from ..results.comparison.screenshot_extractor import (
            _filter_screenshots,
            extract_all_screenshots,
            extract_sample_all_screenshots,
        )
    except ImportError:
        return

    # ---- 优先路径：通过 extract_sample_all_screenshots 获取带 TC 前缀的截图 ----
    e2e_reports_dir = workspace_path / sample_id / "e2e_reports"
    tc_prefixed_saved = False
    if e2e_reports_dir.exists():
        try:
            tc_screenshots = extract_sample_all_screenshots(
                e2e_reports_dir, sample_id, platform
            )
            # 检查是否成功获取到带 TC 前缀的截图
            if tc_screenshots and any(
                "TC" in shot.get("step_name", "") for shot in tc_screenshots
            ):
                saved_count = 0
                for shot in tc_screenshots:
                    step_name = shot["step_name"]
                    url = shot["url"]
                    try:
                        header, b64_data = url.split(",", 1)
                        fmt = header.split("/")[1].split(";")[0]
                        ext = "jpg" if fmt == "jpeg" else fmt
                        img_bytes = __import__("base64").b64decode(b64_data)

                        file_path = screenshots_dir / f"{step_name}.{ext}"
                        file_path.write_bytes(img_bytes)
                        saved_count += 1
                    except Exception:
                        continue
                if saved_count:
                    logger.info(
                        "Saved %d TC-prefixed screenshots for %s/%s to %s",
                        saved_count, sample_id, platform, screenshots_dir,
                    )
                    tc_prefixed_saved = True
        except Exception:
            pass

    if tc_prefixed_saved:
        return

    # ---- DEPRECATED FALLBACK: 从 e2e_reports/ HTML 中抽取截图 ----
    if not _E2E_SCREENSHOT_FALLBACK_WARNED:
        logger.warning(
            "[DEPRECATED] Falling back to extract screenshots from e2e_reports/ HTML "
            "(report_dir=%s). Screenshot source has been unified to "
            "workspace/{sample_id}/screenshots/. Please ensure the upstream test "
            "framework writes screenshots there directly. This fallback will be "
            "removed in a future release.",
            report_dir,
        )
        _E2E_SCREENSHOT_FALLBACK_WARNED = True

    all_screenshots = []

    # 搜索所有 E2E 报告 HTML 文件
    # 1. report_dir 根目录的 *.html
    for html_file in report_dir.glob("*.html"):
        shots = extract_all_screenshots(html_file)
        all_screenshots.extend(shots)

    # 2. .test_intermediates/ai-ui-test/*/report.html
    test_intermediates_dir = report_dir / ".test_intermediates" / "ai-ui-test"
    if test_intermediates_dir.exists():
        for html_file in test_intermediates_dir.glob("*/report.html"):
            shots = extract_all_screenshots(html_file)
            all_screenshots.extend(shots)

    # 3. playwright-*.html under report_dir (recursive)
    for html_file in sorted(report_dir.rglob("playwright-*.html"), key=lambda p: p.name):
        shots = extract_all_screenshots(html_file)
        all_screenshots.extend(shots)

    if not all_screenshots:
        return

    # 过滤无效截图
    valid_screenshots = _filter_screenshots(all_screenshots, platform)

    saved_count = 0
    for shot in valid_screenshots:
        step_name = shot["step_name"]
        url = shot["url"]

        try:
            header, b64_data = url.split(",", 1)
            fmt = header.split("/")[1].split(";")[0]
            ext = "jpg" if fmt == "jpeg" else fmt
            img_bytes = __import__("base64").b64decode(b64_data)

            file_path = screenshots_dir / f"{step_name}.{ext}"
            file_path.write_bytes(img_bytes)
            saved_count += 1
        except Exception:
            continue

    if saved_count:
        logger.info(
            "Saved %d screenshots for %s/%s to %s (via deprecated e2e_reports fallback)",
            saved_count, sample_id, platform, screenshots_dir,
        )


def finalize_prompt_result(
    *,
    result,
    execution_result: ExecutionResult,
    process_data: ProcessCollection,
    sample,
    test_cases: list,
    report_dir: Path | None,
    workspace_path: Path,
    platform: str,
    project_path: str,
    initial_package_size_bytes: int = 0,
) -> None:
    """Post-execution result finalization.

    Sets E2E report path, computes stability and top-level metrics.
    Mutates *result* in place.
    """
    from .validators import (
        compute_experience,
        compute_quality,
        compute_success_rate,
        compute_usability_metrics,
    )

    if process_data.error_type or process_data.error_message:
        result.error_details.append(error_from_legacy(process_data.error_type, process_data.error_message))

    # Set E2E report path
    e2e_path = resolve_e2e_report_path(report_dir, workspace_path)
    if e2e_path:
        result.e2e_report_path = e2e_path

    # Extract and save screenshots immediately after test execution
    _extract_and_save_screenshots(
        report_dir=report_dir or Path(),
        sample_id=result.sample_id,
        platform=platform,
        workspace_path=workspace_path,
    )

    # Compute usability (stability) metrics
    compute_usability_metrics(
        sample=sample,
        test_cases=test_cases,
        execution_result=execution_result,
        project_path=project_path,
        platform=platform,
    )

    # Sync stability metrics to result_data (fix white-screen data chain breakage)
    if execution_result.stability_metrics:
        result.result_data.stability_metrics = execution_result.stability_metrics

    # Compute package size: use initial value (e.g. from sample_report),
    # fallback to computing from artifact_path when initial is 0.
    package_size_bytes = initial_package_size_bytes
    if (
        package_size_bytes == 0
        and platform not in PACKAGE_SIZE_EXCLUDED_PLATFORMS
        and execution_result
        and execution_result.artifact_path
    ):
        package_size_bytes = compute_package_size(
            execution_result.artifact_path, platform,
        )

    # Compute top-level metrics
    result.success_rate = compute_success_rate(result)
    result.quality = compute_quality(result, execution_result)
    result.experience = compute_experience(
        process_data, package_size_bytes=package_size_bytes,
    )

    logger.info(
        "Result: %s/%s passed (%s)",
        result.pass_count,
        result.total_count,
        f"{result.pass_rate:.0%}",
    )

    logger.info(
        "Top-level metrics: success=%.1f quality=%.1f experience=%.1f",
        result.success_rate.composite_score if result.success_rate else 0,
        result.quality.composite_score if result.quality else 0,
        result.experience.composite_score if result.experience else 0,
    )
