"""Shared state, dataclasses and constants for the runner package."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..metrics.models import ANREvent, CrashEvent, StabilityMetrics
from ..results.models import EvaluationError, TestCaseResult
from ...utils.logging import get_logger
from ...utils.paths import get_project_root
from ...utils.process import run_streaming

logger = get_logger(__name__)

# 项目内置工具路径（仓库根目录 tools/）
_BUILTIN_AI_UI_TEST_DIR = get_project_root() / "tools" / "ai-ui-test"
_BUILTIN_BUILD_APP_SCRIPT = (
    get_project_root() / "tools" / "build_app" / "scripts" / "build_app.py"
)
_BUILTIN_INSTALL_APP_SCRIPT = (
    get_project_root() / "tools" / "install_app" / "scripts" / "install_app.py"
)
_DEFAULT_AI_UI_TEST_DIR = _BUILTIN_AI_UI_TEST_DIR
_DEFAULT_BUILD_APP_SCRIPT = _BUILTIN_BUILD_APP_SCRIPT
_DEFAULT_INSTALL_APP_SCRIPT = _BUILTIN_INSTALL_APP_SCRIPT
_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

_DEFAULT_REPORTS_CACHE_ROOT = (
    Path.home() / ".cache" / "evalapp" / "e2e_reports"
)
# NOTE: This is a temporary cache for report snapshots during execution.
# Final reports are exported to the workspace directory's e2e_reports/
# subdirectory by ResultStore.export_e2e_reports(). The cache ensures
# isolation between concurrent test runs.

# Folders that are shared/aggregate report roots: when report.html lives
# under one of these we only snapshot that single file (plus well-known
# sibling artefacts) instead of the whole directory, otherwise older
# reports from previous runs would be dragged into the snapshot.
_SHARED_REPORT_DIRS = {"report", "reports", "midscene_run"}
_SIBLING_ARTEFACT_NAMES = {
    "device.log",
    "performance.json",
    "execution_chain.md",
    "maestro.yaml",
}
_REPORT_FILE_EXTS = {".html", ".htm"}


def _is_shared_report_dir(folder: Path) -> bool:
    """Heuristic: True when *folder* is a shared report aggregation dir."""
    try:
        if folder.name.lower() in _SHARED_REPORT_DIRS:
            return True
        html_count = sum(
            1
            for child in folder.iterdir()
            if child.is_file() and child.suffix.lower() in _REPORT_FILE_EXTS
        )
        return html_count > 1
    except OSError:
        return True


def _sanitise_token(value: str, fallback: str = "x") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def _resolve_ai_ui_test_entry(base_dir: Path) -> Path:
    """Return the ai-ui-test CLI entry-point JS file.

    Newer versions use ``dist/command/ai-ui-test.js``;
    older versions use ``dist/index.js``.
    """
    new_entry = base_dir / "dist" / "command" / "ai-ui-test.js"
    if new_entry.exists():
        return new_entry
    return base_dir / "dist" / "index.js"


@dataclass
class CommandResult:
    """Structured subprocess result for skill script invocations."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass
class ExecutionResult:
    """End-to-end execution outcome for one project on one platform."""

    test_results: list[TestCaseResult] = field(default_factory=list)
    build_status: str = "unknown"
    install_status: str = "unknown"
    launch_status: str = "unknown"
    artifact_path: str = ""
    package_name: str | None = None
    build_duration_ms: int | None = None
    error_message: str = ""
    crash_events: list[CrashEvent] = field(default_factory=list)
    anr_events: list[ANREvent] = field(default_factory=list)
    e2e_report_path: str = ""  # E2E测试报告路径

    # 白屏检测（已在 evaluator.py 中实现）
    white_screen_count: int = 0
    white_screen_evidence: list[str] = field(default_factory=list)

    # 稳定性指标（由 compute_usability_metrics 计算后赋值）
    stability_metrics: "StabilityMetrics | None" = None
    error_details: list[EvaluationError] = field(default_factory=list)


def run_command(
    cmd: list[str],
    cwd: str,
    timeout: int,
    stream_output: bool = False,
    prefix: str = "cmd",
) -> CommandResult:
    """Run a command via run_streaming and return a structured result."""
    result = run_streaming(
        cmd,
        cwd=cwd,
        timeout=timeout,
        stream=stream_output,
        prefix=prefix,
    )
    return CommandResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=int(result.duration * 1000),
    )


def best_effort_error(command_result: CommandResult) -> str:
    """Extract the most useful error message from a command result."""
    stderr = command_result.stderr.strip()
    stdout = command_result.stdout.strip()
    if stderr:
        return stderr[-500:]
    if stdout:
        return stdout[-500:]
    return "command failed without output"
