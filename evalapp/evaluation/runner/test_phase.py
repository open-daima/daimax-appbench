"""Test phase: execute E2E tests via ai-ui-test."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .state import (
    ExecutionResult,
    _is_shared_report_dir,
    _REPORT_FILE_EXTS,
    _resolve_ai_ui_test_entry,
    _SIBLING_ARTEFACT_NAMES,
    _sanitise_token,
    best_effort_error,
    run_command,
)
from .build_phase import find_h5_serve_root, rewrite_cdn_to_local
from ...config import Config
from ..results.models import TestCaseResult
from ..results.models.errors import EvaluationError, error_from_exception, error_from_legacy
from ...benchset.testcases.models import TestCase
from ...utils.logging import get_logger
from ...utils.process import _kill_process_group

logger = get_logger(__name__)


# ── Shared helpers ──────────────────────────────────────────────────


def make_failure_results(
    test_cases: list[TestCase],
    details: str,
) -> list[TestCaseResult]:
    """Create failure results for all test cases with the given details."""
    return [
        TestCaseResult(
            test_case_id=tc.id,
            passed=False,
            status="FAIL",
            details=details,
        )
        for tc in test_cases
    ]


def _result_has_white_screen(result: TestCaseResult) -> bool:
    """Return True only when a white screen is *definitively* detected.

    The sole trusted signal is ``verifications.white_screen.detected``.
    Missing / non-dict / falsy values are treated as "no white screen"
    to honour the zero-false-positive red line: a missing field must
    never be mistaken for a detected white screen, otherwise downstream
    success-rate zeroing would trigger spurious repair loops.
    """
    verifications = getattr(result, "verifications", None)
    if not isinstance(verifications, dict) or not verifications:
        return False
    ws = verifications.get("white_screen")
    if not isinstance(ws, dict) or not ws:
        return False
    detected = ws.get("detected", False)
    return detected is True


def infer_launch_status(results: list[TestCaseResult]) -> str:
    """Infer the app launch status from test results.

    Design intent: ``TC_LAUNCH`` failing (app failed to launch) *or*
    ``TC_LAUNCH`` passing while a white screen is definitively detected
    (launched but blank) both count as a launch failure, which drives
    the first-run success rate to 0.
    """
    if not results:
        return "unknown"

    # 优先检查 TC_LAUNCH 用例（专门的启动测试）
    launch_test = next((r for r in results if r.test_case_id == "TC_LAUNCH"), None)
    if launch_test:
        # TC_LAUNCH 失败（超时/异常/断言失败）→ 启动失败
        if not launch_test.passed:
            return "failed"
        # TC_LAUNCH 通过但白屏检测确凿命中 → 视为启动失败（启动后白屏）
        if _result_has_white_screen(launch_test):
            return "failed"
        return "success"

    # 如果没有 TC_LAUNCH 用例，只要有用例通过就说明启动成功
    if any(result.passed for result in results):
        return "success"

    # 所有用例都失败，说明启动有问题
    return "failed"


def collect_test_errors(results: list[TestCaseResult]) -> list[EvaluationError]:
    """汇集明确的运行异常；普通断言失败不归责执行器。"""
    errors = [error for result in results for error in result.error_details]
    launch = next((r for r in results if r.test_case_id == "TC_LAUNCH"), None)
    if launch and (not launch.passed or _result_has_white_screen(launch)):
        errors.append(EvaluationError(stage="launch", code="launch_failed", message=launch.details))
    return errors


# ── ai-ui-test tool management ──────────────────────────────────────


def ensure_ai_ui_test_ready(ai_ui_test_dir: Path, config: Config | None = None) -> None:
    """Ensure the built-in ai-ui-test tool is initialized (npm install + build).

    On first use, automatically installs dependencies and compiles TypeScript.
    Subsequent calls are skipped unless source is newer than dist.

    Args:
        ai_ui_test_dir: ai-ui-test 工具目录。
        config: 可选配置，提供 npm 安装/编译的超时。
    """
    dist_dir = ai_ui_test_dir / "dist"
    node_modules = ai_ui_test_dir / "node_modules"

    # Check if already initialized and no rebuild needed
    if dist_dir.exists() and node_modules.exists():
        src_dir = ai_ui_test_dir / "src"
        needs_rebuild = False
        if src_dir.exists():
            dist_mtime = max(
                (f.stat().st_mtime for f in dist_dir.rglob("*") if f.is_file()),
                default=0,
            )
            src_mtime = max(
                (f.stat().st_mtime for f in src_dir.rglob("*") if f.is_file()),
                default=0,
            )
            if src_mtime > dist_mtime:
                needs_rebuild = True
                logger.info("ai-ui-test 源码已更新，需要重新编译")
        if not needs_rebuild:
            logger.debug("ai-ui-test已初始化: %s", ai_ui_test_dir)
            ensure_playwright_browsers(ai_ui_test_dir)
            return

    logger.info("首次使用内置ai-ui-test，正在初始化...")
    init_ai_ui_test(ai_ui_test_dir, config=config)
    ensure_playwright_browsers(ai_ui_test_dir)


def ensure_playwright_browsers(ai_ui_test_dir: Path) -> None:
    """确保 Playwright 所需的 chromium / chromium_headless_shell 已下载。

    背景：ai-ui-test 通过 Midscene + Playwright 启动 headless Chromium 执行测试。
    package.json 的 postinstall 仅在 npm install 时触发，一旦 Playwright 升级导致
    新版浏览器 build_id 不在缓存中，运行时会抛 "Executable doesn't exist at
    .../chrome-headless-shell"。本函数在每次启动 ai-ui-test 前做轻量自愈。

    自愈策略：
      1. 调用 `npx playwright install --dry-run chromium` 检查缺失项；
      2. 缺失时运行 `npx playwright install chromium`；
      3. 网络环境带自签证书时（SELF_SIGNED_CERT_IN_CHAIN）回退到
         `NODE_TLS_REJECT_UNAUTHORIZED=0` 重试一次。
    """
    tool_dir = str(ai_ui_test_dir)
    try:
        check = subprocess.run(
            ["npx", "--no-install", "playwright", "install", "--dry-run", "chromium"],
            cwd=tool_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Playwright 浏览器自检失败（跳过自愈）：%s", exc)
        return

    output = (check.stdout or "") + "\n" + (check.stderr or "")
    # dry-run 输出形如 "browser: chromium ... install location: <path>"；
    # 已安装时 path 存在；缺失时 Playwright 会标注 "<not installed>" 或类似字样。
    needs_install = (
        check.returncode != 0
        or "<not installed>" in output
        or "not installed" in output.lower()
    )
    if not needs_install:
        logger.debug("Playwright chromium 浏览器已就绪")
        return

    logger.info("检测到 Playwright 浏览器缺失，开始自动安装 chromium ...")
    install_cmd = ["npx", "--no-install", "playwright", "install", "chromium"]
    install_env = {**os.environ}
    try:
        result = subprocess.run(
            install_cmd,
            cwd=tool_dir,
            capture_output=True,
            text=True,
            timeout=600,
            env=install_env,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Playwright 浏览器安装超时：%s", exc)
        return

    if result.returncode == 0:
        logger.info("Playwright chromium 安装完成")
        return

    err = (result.stderr or result.stdout or "")[:1000]
    if "SELF_SIGNED_CERT_IN_CHAIN" in err or "self-signed certificate" in err:
        logger.warning(
            "Playwright 下载遇到自签证书拦截，回退 NODE_TLS_REJECT_UNAUTHORIZED=0 重试",
        )
        install_env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        try:
            result = subprocess.run(
                install_cmd,
                cwd=tool_dir,
                capture_output=True,
                text=True,
                timeout=600,
                env=install_env,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("Playwright 浏览器安装重试超时：%s", exc)
            return
        if result.returncode == 0:
            logger.info("Playwright chromium 安装完成（已绕过 TLS 校验）")
            return
        err = (result.stderr or result.stdout or "")[:1000]

    logger.error(
        "Playwright chromium 自动安装失败 (exit=%s)：%s\n请手工执行："
        "cd %s && npx playwright install chromium",
        result.returncode, err, tool_dir,
    )


def init_ai_ui_test(ai_ui_test_dir: Path, config: Config | None = None) -> None:
    """Execute npm install and npm run build to initialize ai-ui-test.

    超时参数从 :class:`Config.ai_ui_test` 中读取，避免在代码中硬编码。
    在 ``config`` 为 None 的场景下使用 :class:`AIUITestConfig` 的默认值。
    """
    tool_dir = str(ai_ui_test_dir)

    # 从配置中读取超时值（避免硬编码）
    if config is not None:
        npm_install_timeout = config.ai_ui_test.npm_install_timeout
        npm_build_timeout = config.ai_ui_test.npm_build_timeout
    else:
        # 同步 :class:`AIUITestConfig` 的默认值
        from ...config import AIUITestConfig
        _defaults = AIUITestConfig()
        npm_install_timeout = _defaults.npm_install_timeout
        npm_build_timeout = _defaults.npm_build_timeout

    # npm install
    # Ensure npm registry is reachable before installing dependencies
    from ...utils.npm_registry import ensure_npm_registry_reachable
    ensure_npm_registry_reachable(tool_dir)

    logger.info("安装依赖: cd %s && npm install", tool_dir)
    result = subprocess.run(
        ["npm", "install"],
        cwd=tool_dir,
        capture_output=True,
        text=True,
        timeout=npm_install_timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"npm install 失败 (exit {result.returncode}): {result.stderr[:500]}"
        )
    logger.info("依赖安装完成")

    # npm run build
    logger.info("编译TypeScript: npm run build")
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=tool_dir,
        capture_output=True,
        text=True,
        timeout=npm_build_timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"npm run build 失败 (exit {result.returncode}): {result.stderr[:500]}"
        )
    logger.info("ai-ui-test初始化完成")


# ── Single test execution ───────────────────────────────────────────


def run_single_test(
    *,
    tc: TestCase,
    platform: str,
    ai_ui_test_dir: Path,
    timeout: int,
    package_name: str | None = None,
    h5_url: str = "",
    report_dir: Path | None = None,
    config: Config | None = None,
    stream_output: bool = False,
    reports_cache_root: Path,
    requires_backend: bool = False,
    device_id: str | None = None,
    show_browser: bool = False,
) -> TestCaseResult:
    """Run a single test case via ai-ui-test CLI.

    Args:
        tc: Test case definition.
        platform: Target platform (android/ios/web/...).
        ai_ui_test_dir: Path to the ai-ui-test tool directory.
        timeout: Test execution timeout in seconds.
        package_name: Optional app package name for native platforms.
        h5_url: Optional web URL; when set, forces ``--platform web``
            and passes ``--url`` to ai-ui-test.
        report_dir: Per-item isolated directory used as the subprocess
            cwd so Midscene writes ``.test_intermediates`` there.
        config: Optional Config for model environment variables.
        stream_output: Whether to stream test output in real-time.
        reports_cache_root: Per-executor cache root for report snapshots.
        requires_backend: Whether the sample requires real backend services;
            when True, ``--verify-real-backend`` is added to ai-ui-test.
        device_id: Optional target device ID; when set, passes
            ``--device-id`` to ai-ui-test.
        show_browser: When True, run browser in headed mode (visible);
            when False (default), append ``--headless`` for web platform tests.
    """
    steps_text = build_steps_text(tc, platform=platform, requires_backend=requires_backend)
    assertion_text = tc.expected_result or tc.description
    if not requires_backend:
        assertion_text += (
            "（判断标准："
            "数据为模拟数据或占位符不影响判定）"
        )

    cmd = [
        "node",
        str(_resolve_ai_ui_test_entry(ai_ui_test_dir)),
        steps_text,
        assertion_text,
        "--case-id",
        tc.id,
        "--platform",
        "web" if h5_url else platform,
    ]

    if h5_url:
        cmd.extend(["--url", h5_url])
        # 小程序H5测试使用移动端视口，让页面以手机尺寸渲染
        cmd.extend(["--viewport", "390x844"])
        # 仅当样本标记 requires_backend=true 时启用 real_backend 验证
        if requires_backend:
            cmd.extend(["--verify-real-backend"])
    elif package_name:
        cmd.extend(["--package", package_name])

    if device_id:
        cmd.extend(["--device-id", device_id])

    # H5/web 评测：根据 show_browser 决定是否无头模式
    # show_browser=True  → 有头模式（浏览器可见），不加 --headless
    # show_browser=False → 无头模式（默认），追加 --headless
    if h5_url and not show_browser:
        cmd.append("--headless")

    # Use report_dir as cwd so Midscene writes .test_intermediates there;
    # fall back to ai_ui_test_dir if not specified.
    cwd = str(report_dir) if report_dir else str(ai_ui_test_dir)
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)

    env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    if config is not None:
        env["MIDSCENE_REPLANNING_CYCLE_LIMIT"] = str(config.ai_ui_test.replan_limit)
        # 从配置文件传递模型环境变量（优先于系统环境变量）
        if config.models.e2e.api_key:
            env["MIDSCENE_MODEL_API_KEY"] = config.models.e2e.api_key
        if config.models.e2e.base_url:
            env["MIDSCENE_MODEL_BASE_URL"] = config.models.e2e.base_url
        if config.models.e2e.name:
            env["MIDSCENE_MODEL_NAME"] = config.models.e2e.name
        if config.models.e2e.family:
            env["MIDSCENE_MODEL_FAMILY"] = config.models.e2e.family
        # 可选 Midscene 运行参数：仅在配置存在时透传给 TypeScript 子进程
        run_dir = getattr(config.ai_ui_test, "run_dir", None)
        if run_dir:
            env["MIDSCENE_RUN_DIR"] = str(run_dir)
        log_level = getattr(config.ai_ui_test, "log_level", None)
        if log_level:
            env["MIDSCENE_LOG_LEVEL"] = str(log_level)
    start = time.time()
    try:
        if stream_output:
            from ...utils.process import run_streaming
            result = run_streaming(
                cmd,
                cwd=cwd,
                env=env,
                timeout=timeout,
                stream=True,
                prefix=f"test:{tc.id}",
            )
            duration = result.duration
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
            if result.timed_out:
                raise subprocess.TimeoutExpired(
                    cmd=cmd,
                    timeout=timeout,
                    output=stdout,
                    stderr=stderr,
                )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
                start_new_session=True,  # 新进程组，超时可 killpg 杀干净（含 chromium）
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 与 run_streaming 路径一致：超时杀整个进程组，
                # 避免 chromium 等孙子进程变孤儿堆积、拖垮后续用例
                _kill_process_group(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                raise subprocess.TimeoutExpired(
                    cmd=cmd, timeout=timeout, output=stdout, stderr=stderr
                )
            duration = time.time() - start
            stdout = stdout or ""
            stderr = stderr or ""
            returncode = proc.returncode
        result = parse_ai_ui_test_output(stdout + "\n" + stderr)

        if result is not None:
            passed = result.get("success", False)
            # 优先使用 reason（AI 提取的可读失败原因），其次 fallback 到 error（技术性错误信息）
            ai_reason = result.get("reason", "")
            error_msg = result.get("error", "")
            error_type = result.get("errorType", "")
            if ai_reason:
                # reason 已是可读的失败原因，直接使用
                reason = ai_reason
            else:
                # 没有 reason 时，使用 error 并附加错误类型前缀
                reason = error_msg
                if error_type:
                    reason = f"[{error_type}] {reason}".strip()
            raw_report_path = str(result.get("reportPath") or "").strip()
            snapshot_path, generated_at = snapshot_report(
                raw_report_path=raw_report_path,
                test_case_id=tc.id,
                platform=platform,
                started_at=start,
                reports_cache_root=reports_cache_root,
            )
            # 提取 verifications（white_screen, real_backend 等）
            verifications = result.get("verifications", None)
            return TestCaseResult(
                test_case_id=tc.id,
                passed=passed,
                status="PASS" if passed else "FAIL",
                details=reason,
                duration=duration,
                report_path=snapshot_path,
                report_started_at=start,
                report_generated_at=generated_at,
                verifications=verifications,
                error_details=[error_from_legacy(error_type, reason, stage="test")] if error_type else [],
            )

        return TestCaseResult(
            test_case_id=tc.id,
            passed=returncode == 0,
            status="PASS" if returncode == 0 else "FAIL",
            details=stdout[-200:] if stdout else stderr[-200:],
            duration=duration,
        )

    except subprocess.TimeoutExpired as exc:
        raw_report_path, verifications = recover_timeout_artifacts(
            report_dir=report_dir,
            test_case_id=tc.id,
            started_at=start,
        )
        snapshot_path, generated_at = snapshot_report(
            raw_report_path=raw_report_path,
            test_case_id=tc.id,
            platform=platform,
            started_at=start,
            reports_cache_root=reports_cache_root,
        )
        details = f"Test timed out after {timeout}s"
        if snapshot_path or verifications:
            details += "; recovered partial artifacts"
        return TestCaseResult(
            test_case_id=tc.id,
            passed=False,
            status="FAIL",
            details=details,
            duration=time.time() - start,
            report_path=snapshot_path,
            report_started_at=start,
            report_generated_at=generated_at,
            verifications=verifications,
            error_details=[error_from_exception(exc, stage="test")],
        )
    except Exception as exc:  # pragma: no cover - defensive path
        return TestCaseResult(
            test_case_id=tc.id,
            passed=False,
            status="FAIL",
            details=f"Error: {exc}",
            duration=time.time() - start,
            error_details=[error_from_exception(exc, stage="test")],
        )


def build_steps_text(tc: TestCase, platform: str = "", requires_backend: bool = False) -> str:
    """Build a natural language steps description for ai-ui-test."""
    guidance_parts = [
        "【执行说明】这是AI生成的应用，请以验证目标为准灵活执行。",
        "(1) 按钮/导航的文案和位置可能与参考操作不同，寻找语义相近的元素；",
    ]
    if not requires_backend:
        guidance_parts.append(
            "(2) 数据为占位符或模拟数据时，只要UI结构存在即视为功能可用。"
        )
    guidance = "".join(guidance_parts) + "\n"
    goal = f"验证目标：{tc.description}"
    if tc.steps:
        hints = [step.split(" -> 预期:")[0].strip() for step in tc.steps]
        return guidance + goal + "\n参考操作路径（仅供参考，实际界面可能不同）：" + "，".join(hints)
    return guidance + goal


def parse_ai_ui_test_output(output: str) -> dict | None:
    """Parse the JSON result from ai-ui-test CLI output."""
    for line in output.split("\n"):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "success" in data:
            return data
    return None


# Midscene names shared-dir reports like
# ``playwright-2026-08-04_23-40-08-xxxx.html``: the embedded timestamp
# records when the report was generated.
_REPORT_NAME_TS_RE = re.compile(r"playwright-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


def _parse_report_filename_timestamp(name: str) -> float | None:
    """Parse the timestamp embedded in a playwright report filename.

    Returns epoch seconds (local time, matching ``time.time()``) or
    ``None`` when the name embeds no parsable timestamp. Callers must
    skip the filename-based stale check on ``None`` (conservative:
    never block on a guess, honouring the zero-false-positive line).
    """
    match = _REPORT_NAME_TS_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").timestamp()
    except ValueError:
        return None


def _filename_predates_start(name: str, started_at: float) -> bool:
    """True when the filename timestamp is >5s older than *started_at*.

    Catches the ``mtime == started_at`` edge case where a stale file in
    the shared report dir was touched exactly at case start, so the
    mtime tolerance alone cannot flag it as stale.
    """
    name_ts = _parse_report_filename_timestamp(name)
    return name_ts is not None and name_ts + 5.0 < started_at


def recover_timeout_artifacts(
    *,
    report_dir: Path | None,
    test_case_id: str,
    started_at: float,
) -> tuple[str, dict | None]:
    """Recover the report and live page diagnostics after a test timeout."""
    if not report_dir or not report_dir.exists():
        return "", None

    case_token = _sanitise_token(test_case_id, "tc")
    intermediates = report_dir / ".test_intermediates" / "ai-ui-test"
    run_dirs: list[Path] = []
    if intermediates.exists():
        run_dirs = [
            path for path in intermediates.iterdir()
            if path.is_dir() and case_token in path.name
        ]
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    diagnostics = None
    for run_dir in run_dirs:
        diagnostics_path = run_dir / "page_diagnostics.json"
        if not diagnostics_path.exists():
            continue
        try:
            if diagnostics_path.stat().st_mtime + 5.0 < started_at:
                continue
            capture = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            diagnostics = {"page_diagnostics": _summarise_page_diagnostics(capture)}
            break
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue

    for run_dir in run_dirs:
        report_path = run_dir / "report.html"
        if report_path.exists() and report_path.stat().st_mtime + 5.0 >= started_at:
            return str(report_path), diagnostics

    report_candidates = [
        path for path in report_dir.rglob("playwright-*.html")
        if path.stat().st_mtime + 5.0 >= started_at
    ]
    # Prefer candidates whose filename embeds this case's token: they
    # provably belong to this case. Otherwise drop candidates whose
    # filename timestamp predates the case start (mtime alone cannot
    # be trusted in the shared midscene_run/report/ directory), and
    # never fall back to a stale file.
    preferred = [
        path for path in report_candidates if case_token in path.name
    ]
    if preferred:
        report_candidates = preferred
    else:
        report_candidates = [
            path for path in report_candidates
            if not _filename_predates_start(path.name, started_at)
        ]
    if report_candidates:
        report_candidates.sort(
            key=lambda path: abs(path.stat().st_mtime - started_at)
        )
        return str(report_candidates[0]), diagnostics
    return "", diagnostics


def _summarise_page_diagnostics(capture: dict) -> dict:
    """Convert a live diagnostics capture into the public verification shape."""
    network_enabled = capture.get("captureNetwork") is True
    requests = capture.get("networkRequests") or []
    js_errors = capture.get("jsErrors") or []
    console_messages = capture.get("consoleMessages") or []
    network_errors = [
        item for item in requests
        if network_enabled and (item.get("failed") or item.get("status") is None)
    ]
    http_errors = [
        item for item in requests
        if network_enabled and not item.get("failed")
        and isinstance(item.get("status"), int) and item["status"] >= 400
    ]
    console_errors = [
        item for item in console_messages if item.get("level") == "error"
    ]
    console_warnings = [
        item for item in console_messages
        if item.get("level") in {"warn", "warning"}
    ]
    reasons = []
    if network_errors:
        reasons.append(f"网络失败 {len(network_errors)} 个")
    if http_errors:
        reasons.append(f"HTTP错误 {len(http_errors)} 个")
    if js_errors:
        reasons.append(f"JS运行时错误 {len(js_errors)} 个")
    if console_errors:
        reasons.append(f"console.error {len(console_errors)} 条")
    if console_warnings:
        reasons.append(f"console.warn {len(console_warnings)} 条")
    issue_count = len(network_errors) + len(http_errors) + len(js_errors) + len(console_errors)
    return {
        "pass": issue_count == 0,
        "method": "playwright_page_diagnostics_timeout_recovery",
        "reason": "；".join(reasons) or "超时前未检测到JS运行时错误或console.error",
        "summary": {
            "network_monitor_enabled": network_enabled,
            "total_requests": len(requests),
            "network_error_count": len(network_errors),
            "http_error_count": len(http_errors),
            "js_error_count": len(js_errors),
            "console_error_count": len(console_errors),
            "console_warn_count": len(console_warnings),
        },
        "network_errors": network_errors[:20],
        "http_errors": http_errors[:20],
        "js_errors": js_errors[:20],
        "console_errors": (console_errors + console_warnings)[:20],
        "requests": requests[:50],
    }


def snapshot_report(
    *,
    raw_report_path: str,
    test_case_id: str,
    platform: str,
    started_at: float,
    reports_cache_root: Path,
) -> tuple[str, float]:
    """Copy the ai-ui-test report to a private per-case snapshot dir.

    Why: the report path returned by ai-ui-test may live inside a
    shared folder such as ``midscene_run/report/`` that accumulates
    historical HTML files from previous runs. By taking a snapshot
    *immediately* after the CLI exits, we pin the report that truly
    belongs to this case and decouple later export logic from any
    concurrent or subsequent runs mutating that shared folder.

    Returns:
        (snapshot_report_path, report_mtime_seconds). Both are empty
        / 0.0 when the source path is missing on disk.
    """
    if not raw_report_path:
        return "", 0.0
    src = Path(raw_report_path).expanduser()
    if not src.exists():
        logger.warning(
            "ai-ui-test reportPath missing on disk, skip snapshot: %s",
            raw_report_path,
        )
        return "", 0.0

    try:
        mtime = src.stat().st_mtime
    except OSError:
        mtime = 0.0

    # Block stale pointers: a file predating this case's start almost
    # certainly means ai-ui-test returned a report left over from a
    # previous case in the shared report dir. Treat it the same as
    # "no report" instead of snapshotting the wrong case's report.
    # Use 5s tolerance to accommodate NFS/Docker clock skew.
    if mtime and mtime + 5.0 < started_at:
        logger.warning(
            "report file predates test start (mtime=%.0f < started_at=%.0f, delta=%.1fs), skip snapshot: %s",
            mtime, started_at, started_at - mtime, src,
        )
        return "", 0.0

    # mtime can equal started_at exactly when the shared dir entry was
    # touched at case start, so additionally check the timestamp
    # embedded in playwright report filenames. Unparsable names skip
    # this check (conservative, zero false positives).
    if _filename_predates_start(src.name, started_at):
        logger.warning(
            "report filename timestamp predates test start (started_at=%.0f), skip snapshot: %s",
            started_at, src,
        )
        return "", 0.0

    tc_token = _sanitise_token(test_case_id, "tc")
    plat_token = _sanitise_token(platform, "unknown")
    ts_suffix = datetime.fromtimestamp(started_at).strftime("%Y%m%d_%H%M%S")
    case_dir = (
        reports_cache_root
        / f"{tc_token}_{plat_token}_{ts_suffix}"
    )
    # Ensure unique directory in case of rapid retries within the
    # same second.
    idx = 0
    final_dir = case_dir
    while final_dir.exists():
        idx += 1
        final_dir = Path(f"{case_dir}_{idx}")
    try:
        final_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Unable to create snapshot dir %s: %s", final_dir, exc,
        )
        return str(src), mtime

    # Copy the HTML itself first.
    try:
        dest_html = final_dir / (src.name if src.suffix else "report.html")
        shutil.copy2(src, dest_html)
    except OSError as exc:
        logger.warning(
            "Failed to snapshot report %s -> %s: %s",
            src, final_dir, exc,
        )
        return str(src), mtime

    # If parent looks like a private per-case bundle, copy the
    # well-known sibling artefacts too. In shared directories we
    # deliberately skip siblings (they belong to other runs).
    parent = src.parent
    if parent.is_dir() and not _is_shared_report_dir(parent):
        for sibling in parent.iterdir():
            if sibling == src or not sibling.is_file():
                continue
            # Skip other HTML reports to avoid mixing cases.
            if sibling.suffix.lower() in _REPORT_FILE_EXTS:
                continue
            try:
                shutil.copy2(sibling, final_dir / sibling.name)
            except OSError:
                continue
    else:
        # Still try to pull known artefacts that share the same
        # timestamp prefix as the report (best-effort).
        report_prefix = src.stem
        if parent.is_dir():
            for sibling in parent.iterdir():
                if not sibling.is_file() or sibling == src:
                    continue
                if sibling.name in _SIBLING_ARTEFACT_NAMES or \
                        sibling.stem.startswith(report_prefix):
                    try:
                        shutil.copy2(sibling, final_dir / sibling.name)
                    except OSError:
                        continue

    logger.info(
        "Snapshotted e2e report %s -> %s", src, dest_html,
    )
    return str(dest_html), mtime


# ── H5 test execution ───────────────────────────────────────────────


def execute_h5_tests(
    *,
    test_cases: list[TestCase],
    h5_url: str,
    platform: str,
    ai_ui_test_dir: Path,
    timeout: int,
    config: Config | None,
    stream_output: bool,
    reports_cache_root: Path,
    report_dir: Path | None = None,
    requires_backend: bool = False,
    show_browser: bool = False,
) -> ExecutionResult:
    """Execute E2E tests against an H5 URL (miniprogram web preview).

    Skips build and install phases since the app is already deployed
    as a web page at the given URL.
    """
    logger.info(
        "Executing %d H5 tests against %s",
        len(test_cases),
        h5_url,
    )

    # Verify ai-ui-test is available
    index_js = _resolve_ai_ui_test_entry(ai_ui_test_dir)
    if not index_js.exists():
        message = f"ai-ui-test not found at {index_js}"
        logger.error(message)
        return ExecutionResult(
            test_results=make_failure_results(test_cases, message),
            build_status="skipped",
            install_status="skipped",
            launch_status="unknown",
            error_message=message,
            error_details=[EvaluationError(origin="environment", stage="test", code="test_tool_unavailable", message=message)],
        )

    results: list[TestCaseResult] = []
    for i, tc in enumerate(test_cases):
        logger.info("  [%s/%s] %s: %s", i + 1, len(test_cases), tc.id, tc.name)
        result = run_single_test(
            tc=tc,
            platform=platform,
            ai_ui_test_dir=ai_ui_test_dir,
            timeout=timeout,
            h5_url=h5_url,
            report_dir=report_dir,
            config=config,
            stream_output=stream_output,
            reports_cache_root=reports_cache_root,
            requires_backend=requires_backend,
            show_browser=show_browser,
        )
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

    passed = sum(1 for r in results if r.passed)
    logger.info("H5 test execution complete: %s/%s passed", passed, len(results))

    return ExecutionResult(
        test_results=results,
        build_status="skipped",
        install_status="skipped",
        launch_status=infer_launch_status(results),
        error_details=collect_test_errors(results),
    )


def get_free_port() -> int:
    """Allocate a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _kill_processes_on_port(port: int) -> None:
    """Kill any process listening on the given port (best-effort).

    Used to clean up leftover Expo Metro bundler processes that may
    survive across sequential sample evaluations and cause port 8081
    conflicts for subsequent builds.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                pid_str = pid_str.strip()
                if not pid_str:
                    continue
                try:
                    os.kill(int(pid_str), signal.SIGKILL)
                    logger.info("Killed process %s on port %d", pid_str, port)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


def _wait_for_port(port: int, timeout: float | None = None, interval: float = 0.3,
                   config: Config | None = None) -> bool:
    """Wait until a TCP port is accepting connections on localhost.

    Args:
        port: The TCP port to check.
        timeout: Maximum wait time in seconds. 如为 None 则从 ``config``
            读取 ``ai_ui_test.port_wait_timeout``，仍为 None 时使用
            :class:`AIUITestConfig` 默认值（避免代码内硬编码）。
        interval: Time between retries in seconds.
        config: 可选配置，仅在 ``timeout`` 未显式传入时起作用。

    Returns:
        True if port became reachable within timeout, False otherwise.
    """
    if timeout is None:
        if config is not None:
            timeout = config.ai_ui_test.port_wait_timeout
        else:
            from ...config import AIUITestConfig
            timeout = AIUITestConfig().port_wait_timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(("127.0.0.1", port))
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(interval)
    return False


def build_and_serve_h5(
    *,
    test_cases: list[TestCase],
    project_path: str,
    platform: str,
    build_timeout: int,
    stream_output: bool,
    ai_ui_test_dir: Path,
    timeout: int,
    config: Config | None,
    reports_cache_root: Path,
    report_dir: Path | None = None,
    requires_backend: bool = False,
    show_browser: bool = False,
) -> ExecutionResult:
    """Build the H5 target, serve it locally, run E2E tests, then tear down."""
    # Defensive: clean up any leftover Expo Metro bundler on port 8081
    # so that subsequent expo_web / expo_android / expo_ios samples do
    # not encounter "Port 8081 is being used by another process".
    _kill_processes_on_port(8081)

    shell_project = Path(project_path)

    def _pkg_has_build_web(pkg: Path) -> bool:
        """True if *pkg* is a readable package.json with a build:web script.

        Robust against malformed files: corrupt JSON, non-object top level,
        ``scripts`` being null or a non-dict (e.g. a string, where ``in``
        would silently do substring matching) all yield False.
        """
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        scripts = data.get("scripts")
        return isinstance(scripts, dict) and "build:web" in scripts

    # Generator H5: the actual Vite project may live in a shell_project/
    # subdirectory under the generated project root.  If package.json is
    # absent at the root but present inside shell_project/, redirect.
    #
    # expo_web 兜底防御（capability/ 干扰目录问题，2026-09-17）：生成器可能在
    # 项目根旁生成仅含 test 脚本的辅助目录（如 capability/）。若根目录
    # package.json 存在但缺少 build:web 脚本，同样重定向到 shell_project/，
    # 避免后续 `npm run build:web` 报 "Missing script"。
    root_pkg = shell_project / "package.json"
    root_pkg_unusable = not root_pkg.is_file() or (
        platform == "expo_web" and not _pkg_has_build_web(root_pkg)
    )
    if root_pkg_unusable:
        _subdir = shell_project / "shell_project"
        _subdir_pkg = _subdir / "package.json"
        if _subdir_pkg.is_file() and (
            platform != "expo_web" or _pkg_has_build_web(_subdir_pkg)
        ):
            logger.info(
                "package.json %s at project root, using "
                "shell_project/ subdirectory: %s",
                "missing" if not root_pkg.is_file() else "lacks build:web script",
                _subdir,
            )
            shell_project = _subdir

    # expo_web uses `expo export --platform web` which outputs to dist/,
    # h5 (Vite) uses `npm run build` which outputs to dist/,
    # while miniprogram uses `npm run build:h5` which outputs to dist-h5/.
    dist_dir_name = "dist" if platform in ("expo_web", "h5") else "dist-h5"
    dist_h5 = shell_project / dist_dir_name

    # Check if the project is already a static HTML app (e.g. Claude-
    # generated pure HTML/CSS/JS project with index.html at root).
    # In that case, skip the npm build entirely and serve from the
    # project directory directly.
    # NOTE: Exclude Vite/Node projects (which also have index.html but
    # require a build step) by checking that package.json is absent.
    project_root_index = shell_project / "index.html"
    if project_root_index.is_file() and not dist_h5.exists() and not (shell_project / "package.json").is_file():
        logger.info(
            "index.html found at project root, serving as static site "
            "(no npm build needed)"
        )
        serve_root = shell_project
    else:
        # Step 1: Build if dist directory doesn't exist or has no index.html.
        #
        # miniprogram 特殊处理：生成器在生成阶段已经 build 过 dist-h5，但
        # 它走的是发布构建（taro build --type h5 <OSS_URL>），publicPath 被
        # 烧成 OSS 绝对地址，本地 npx serve 无法访问异步 chunk / 图片。
        # 我们无条件删旧产物并重新执行 `npm run build:h5`（不附加 URL 参数），
        # 让 config/index.ts 的 publicPath 退回到 '/'，所有资源走相对路径。
        if platform == "miniprogram" and dist_h5.exists():
            logger.info(
                "Removing generator-built %s/ (with remote publicPath) before "
                "local rebuild",
                dist_dir_name,
            )
            shutil.rmtree(dist_h5, ignore_errors=True)

        # 复用判断：对 h5 / expo_web 平台，若 dist/ 已存在且包含 index.html
        # （生成器生成阶段已完成构建），则直接复用该产物，仅做后续的
        # CDN URL 改写 + serve，避免重复执行 npm run build。
        # miniprogram 例外：Taro 的 publicPath 在构建时写死，必须重建，
        # 故其 dist-h5/ 已在上方被删除，此处始终触发构建。
        existing_serve_root = find_h5_serve_root(dist_h5) if dist_h5.exists() else None
        needs_build = (
            platform == "miniprogram"
            or not dist_h5.exists()
            or existing_serve_root is None
        )
        if needs_build:
            if platform == "miniprogram":
                logger.info(
                    "Rebuilding %s/ for miniprogram (Taro publicPath must be "
                    "reset for local serve)",
                    dist_dir_name,
                )
            elif not dist_h5.exists():
                logger.info(
                    "No existing %s/ found, running fresh build", dist_dir_name
                )
            else:
                logger.info(
                    "Existing %s/ has no index.html, rebuilding", dist_dir_name
                )
            if platform == "expo_web":
                build_script = "build:web"
                build_label = "web"
            elif platform == "h5":
                build_script = "build"
                build_label = "h5"
            else:
                build_script = "build:h5"
                build_label = "miniprogram-h5"
            logger.info("Building %s target in %s ...", build_label.upper(), shell_project)
            build_cmd_result = run_command(
                ["npm", "run", build_script],
                cwd=str(shell_project),
                timeout=build_timeout,
                stream_output=stream_output,
                prefix=f"{build_label}-build",
            )
            if build_cmd_result.returncode != 0:
                message = best_effort_error(build_cmd_result)
                logger.error("H5 build failed: %s", message)
                return ExecutionResult(
                    test_results=make_failure_results(
                        test_cases, f"H5 build failed: {message}"
                    ),
                    build_status="failed",
                    install_status="skipped",
                    launch_status="failed",
                    build_duration_ms=build_cmd_result.duration_ms,
                    error_message=message,
                    error_details=[EvaluationError(stage="build", code="build_failed", message=message)],
                )
            if not dist_h5.exists():
                message = f"Build succeeded but {dist_dir_name}/ directory not found"
                logger.error(message)
                return ExecutionResult(
                    test_results=make_failure_results(test_cases, message),
                    build_status="failed",
                    install_status="skipped",
                    launch_status="failed",
                    build_duration_ms=build_cmd_result.duration_ms,
                    error_message=message,
                    error_details=[EvaluationError(stage="build", code="build_failed", message=message)],
                )
        else:
            logger.info(
                "Reusing existing generator build output at %s/ (index.html "
                "present), skipping npm run build",
                dist_dir_name,
            )

        # Step 2: Resolve the actual serve root (where index.html lives)
        serve_root = find_h5_serve_root(dist_h5)
        if serve_root is None:
            message = (
                f"No index.html found under {dist_h5}/ – "
                "H5 build output may be incomplete"
            )
            logger.error(message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, message),
                build_status="success",
                install_status="skipped",
                launch_status="failed",
                error_message=message,
                error_details=[EvaluationError(stage="launch", code="launch_failed", message=message)],
            )

    logger.info("Serving H5 from %s", serve_root)

    # Step 2b: Rewrite CDN URLs to relative paths (needed when
    # --skip-publish was used and assets were not uploaded to CDN).
    #
    # miniprogram 平台已在 Step 1 强制 rebuild，publicPath = '/'，产物中
    # 不会再出现 OSS 绝对地址，无需 rewrite；其他平台（expo_web / h5）
    # 仍按原逻辑兜底。
    if platform != "miniprogram":
        rewrite_cdn_to_local(serve_root)

    # Step 3: Start a local static server
    serve_proc: subprocess.Popen | None = None
    port = get_free_port()
    h5_url = f"http://localhost:{port}"
    try:
        serve_proc = subprocess.Popen(
            ["npx", "serve", str(serve_root), "-l", str(port),
             "--no-clipboard", "--single"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # new process group so we can kill the whole tree later
            preexec_fn=os.setsid,
        )
        # Wait for the server to become reachable (port readiness check)
        # 超时从 config.ai_ui_test.port_wait_timeout 读取，避免硬编码。
        port_wait_timeout = (
            config.ai_ui_test.port_wait_timeout if config is not None else None
        )
        if not _wait_for_port(port, timeout=port_wait_timeout, config=config):
            # Port not reachable within timeout
            if serve_proc.poll() is None:
                # Process still alive, log warning and proceed
                logger.warning(
                    "Port %d not reachable after %ss, proceeding anyway",
                    port,
                    port_wait_timeout if port_wait_timeout is not None else "default",
                )
            # else: fall through to poll check below
        if serve_proc.poll() is not None:
            try:
                stderr_out = (serve_proc.stderr.read() if serve_proc.stderr else "") or ""
            except (ValueError, IOError):
                stderr_out = ""
            stderr_out = stderr_out.strip()
            message = f"serve exited immediately: {stderr_out}"
            logger.error(message)
            return ExecutionResult(
                test_results=make_failure_results(test_cases, message),
                build_status="success",
                install_status="skipped",
                launch_status="failed",
                error_message=message,
                error_details=[EvaluationError(stage="launch", code="launch_failed", message=message)],
            )
        logger.info("H5 server started at %s (pid %s)", h5_url, serve_proc.pid)

        # Step 4: Run tests
        return execute_h5_tests(
            test_cases=test_cases,
            h5_url=h5_url,
            platform=platform,
            ai_ui_test_dir=ai_ui_test_dir,
            timeout=timeout,
            config=config,
            stream_output=stream_output,
            reports_cache_root=reports_cache_root,
            report_dir=report_dir,
            requires_backend=requires_backend,
            show_browser=show_browser,
        )
    finally:
        if serve_proc is not None and serve_proc.poll() is None:
            logger.info("Stopping H5 server (pid %s)", serve_proc.pid)
            # 超时从 config.ai_ui_test.serve_shutdown_timeout 读取，避免硬编码。
            if config is not None:
                shutdown_timeout = config.ai_ui_test.serve_shutdown_timeout
            else:
                from ...config import AIUITestConfig
                shutdown_timeout = AIUITestConfig().serve_shutdown_timeout
            try:
                os.killpg(os.getpgid(serve_proc.pid), signal.SIGTERM)
                serve_proc.wait(timeout=shutdown_timeout)
            except Exception:
                try:
                    os.killpg(os.getpgid(serve_proc.pid), signal.SIGKILL)
                except Exception:  # pragma: no cover
                    pass


# ── Legacy parse methods ────────────────────────────────────────────
# Keep for backward compatibility with existing tests.


def parse_results(
    test_cases: list[TestCase],
    raw_output: str,
) -> list[TestCaseResult]:
    """Parse test results from raw output (legacy compatibility)."""
    parsed = try_parse_json_results(raw_output)
    if parsed:
        return match_results(test_cases, parsed)
    return fallback_parse(test_cases, raw_output)


def try_parse_json_results(output: str) -> list[dict] | None:
    """Try to parse a JSON array of results from the output.

    [性能优化] 使用 str.find() 定位括号位置替代逐字符扫描，
    对于大输出字符串（数KB~数十KB）显著减少 Python 循环开销。
    """
    # 快速路径：如果没有 '[' 直接返回
    if '[' not in output:
        return None

    # 从后往前搜索，因为 JSON 结果通常在输出末尾
    search_start = 0
    while True:
        start = output.find('[', search_start)
        if start < 0:
            return None

        # 用计数器找匹配的 ']'
        depth = 0
        i = start
        length = len(output)
        while i < length:
            ch = output[i]
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(output[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, list) and data:
                        return data
                    break
            # 跳过字符串内容以避免误匹配括号
            elif ch == '"':
                i += 1
                while i < length and output[i] != '"':
                    if output[i] == '\\':
                        i += 1  # 跳过转义字符
                    i += 1
            i += 1

        search_start = start + 1


def match_results(
    test_cases: list[TestCase],
    parsed: list[dict],
) -> list[TestCaseResult]:
    """Match parsed results to test cases by ID."""
    results: list[TestCaseResult] = []
    parsed_by_id = {str(r.get("test_case_id", r.get("id", ""))): r for r in parsed}

    for tc in test_cases:
        parsed_result = parsed_by_id.get(tc.id)
        if parsed_result:
            status = parsed_result.get("status", "FAIL").upper()
            results.append(
                TestCaseResult(
                    test_case_id=tc.id,
                    passed=status == "PASS",
                    status=status,
                    details=parsed_result.get("details", ""),
                )
            )
        else:
            results.append(
                TestCaseResult(
                    test_case_id=tc.id,
                    passed=False,
                    status="FAIL",
                    details="No result found for this test case",
                )
            )
    return results


def fallback_parse(
    test_cases: list[TestCase],
    raw_output: str,
) -> list[TestCaseResult]:
    """Fallback parsing of test results from raw output."""
    output_lower = raw_output.lower()
    results: list[TestCaseResult] = []

    for tc in test_cases:
        tc_id_lower = tc.id.lower()
        tc_name_lower = tc.name.lower()

        passed = False
        details = ""

        for identifier in [tc_id_lower, tc_name_lower]:
            idx = output_lower.find(identifier)
            if idx >= 0:
                line_end = output_lower.find("\n", idx)
                if line_end < 0:
                    line_end = len(output_lower)
                context = output_lower[idx:line_end]
                if "pass" in context and "fail" not in context:
                    passed = True
                    details = "Matched PASS in output"
                elif "fail" in context:
                    passed = False
                    details = "Matched FAIL in output"
                break

        results.append(
            TestCaseResult(
                test_case_id=tc.id,
                passed=passed,
                status="PASS" if passed else "FAIL",
                details=details or "Result parsed from raw output",
            )
        )

    return results
