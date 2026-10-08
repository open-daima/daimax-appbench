"""测试 TestExecutor 的 build/install/test 流程。

覆盖:
- execute_tests 调用 build -> install -> ui test 的完整流程
- build 失败时返回正确的 ExecutionResult
- install 失败时返回正确的 ExecutionResult
"""

from __future__ import annotations

import json
import subprocess
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evalapp.evaluation.runner.executor import TestExecutor
from evalapp.evaluation.runner.test_phase import run_single_test
from evalapp.evaluation.runner.state import ExecutionResult
from evalapp.benchset.testcases.models import TestCase
from evalapp.utils.process import StreamingResult


def _make_test_cases():
    """创建测试用例列表。"""
    return [
        TestCase(id="TC001", name="验证登录功能", description="测试用户登录流程", steps=["打开App", "输入账号密码", "点击登录"]),
        TestCase(id="TC002", name="验证注册功能", description="测试用户注册流程", steps=["打开App", "点击注册"]),
    ]


class TestExecutorBuildInstallTest:
    """测试 TestExecutor 的完整 build -> install -> test 流程。"""

    def test_execute_tests_runs_build_install_and_ui_flow(self, tmp_path):
        """完整流程: build -> install -> E2E test。"""
        # 创建 mock 的项目路径
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "build.gradle").write_text("apply plugin: 'com.android.application'")

        test_cases = _make_test_cases()

        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        # Mock build_project
        mock_build_result = {
            "success": True,
            "artifact_path": str(tmp_path / "app.apk"),
            "message": "",
            "duration_ms": 5000,
        }

        # Mock install_artifact
        mock_install_result = {
            "success": True,
            "message": "",
            "duration_ms": 3000,
        }

        # Mock ai-ui-test entry point exists
        ai_ui_entry = tmp_path / "ai-ui-test" / "dist" / "index.js"
        ai_ui_entry.parent.mkdir(parents=True, exist_ok=True)
        ai_ui_entry.write_text("// mock")

        with patch.object(executor, "_build_project_with_retry", return_value=mock_build_result):
            with patch.object(executor, "_install_artifact_with_retry", return_value=mock_install_result):
                with patch.object(executor, "_resolve_package_name", return_value="com.test.app"):
                    with patch.object(executor, "_run_single_test") as mock_run_test:
                        # Mock test execution results
                        from evalapp.evaluation.results.models import TestCaseResult
                        mock_run_test.side_effect = [
                            TestCaseResult(test_case_id="TC001", passed=True, status="PASS", details="OK"),
                            TestCaseResult(test_case_id="TC002", passed=False, status="FAIL", details="Button not found"),
                        ]

                        result = executor.execute_tests(
                            test_cases=test_cases,
                            project_path=str(project_path),
                            platform="android",
                            collect_device_logs=False,
                        )

        assert isinstance(result, ExecutionResult)
        assert result.build_status == "success"
        assert result.install_status == "success"
        assert len(result.test_results) == 2
        assert result.test_results[0].passed is True
        assert result.test_results[1].passed is False

    def test_execute_tests_build_failure(self, tmp_path):
        """build 失败应返回所有 TC 均 FAIL 的结果。"""
        project_path = tmp_path / "project"
        project_path.mkdir()

        test_cases = _make_test_cases()
        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        mock_build_result = {
            "success": False,
            "artifact_path": "",
            "message": "Compilation error in MainActivity.java",
            "duration_ms": 8000,
        }

        with patch.object(executor, "_build_project_with_retry", return_value=mock_build_result):
            result = executor.execute_tests(
                test_cases=test_cases,
                project_path=str(project_path),
                platform="android",
                collect_device_logs=False,
            )

        assert result.build_status == "failed"
        assert result.install_status == "skipped"
        assert result.launch_status == "failed"
        # 所有 TC 应标记为 FAIL
        assert len(result.test_results) == 2
        assert all(not tr.passed for tr in result.test_results)
        assert "Build failed" in result.test_results[0].details

    def test_execute_tests_install_failure(self, tmp_path):
        """install 失败应在 build 成功后中止。"""
        project_path = tmp_path / "project"
        project_path.mkdir()

        test_cases = _make_test_cases()
        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        mock_build_result = {
            "success": True,
            "artifact_path": str(tmp_path / "app.apk"),
            "message": "",
            "duration_ms": 5000,
        }
        mock_install_result = {
            "success": False,
            "message": "Device not found",
            "duration_ms": 2000,
        }

        with patch.object(executor, "_build_project_with_retry", return_value=mock_build_result):
            with patch.object(executor, "_install_artifact_with_retry", return_value=mock_install_result):
                with patch.object(executor, "_resolve_package_name", return_value="com.test.app"):
                    result = executor.execute_tests(
                        test_cases=test_cases,
                        project_path=str(project_path),
                        platform="android",
                        collect_device_logs=False,
                    )

        assert result.build_status == "success"
        assert result.install_status == "failed"
        assert result.launch_status == "failed"
        assert len(result.test_results) == 2
        assert all(not tr.passed for tr in result.test_results)

    def test_execute_tests_empty_test_cases(self, tmp_path):
        """空用例列表应返回默认空结果。"""
        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        result = executor.execute_tests(
            test_cases=[],
            project_path=str(tmp_path),
            platform="android",
            collect_device_logs=False,
        )

        assert isinstance(result, ExecutionResult)
        assert result.test_results == []


@pytest.mark.parametrize("stream_output", [False, True])
def test_timeout_recovers_report_and_page_diagnostics(tmp_path, stream_output):
    """普通与流式执行超时后都应回收报告和实时页面诊断。"""
    report_dir = tmp_path / "reports"
    run_dir = (
        report_dir
        / ".test_intermediates"
        / "ai-ui-test"
        / "web_TC020_unknown_2026-07-30T23-01-03-000"
    )
    run_dir.mkdir(parents=True)
    diagnostics = {
        "networkRequests": [],
        "jsErrors": [
            {
                "type": "pageerror",
                "name": "TypeError",
                "message": "boom",
                "timestamp": "2026-07-30T23:01:30.000Z",
            }
        ],
        "consoleMessages": [
            {
                "level": "error",
                "message": "render failed",
                "timestamp": "2026-07-30T23:01:31.000Z",
            }
        ],
        "captureNetwork": False,
    }
    (run_dir / "page_diagnostics.json").write_text(
        json.dumps(diagnostics), encoding="utf-8"
    )
    # 文件名内嵌时间戳必须为当前时间：回收逻辑会剔除文件名时间戳
    # 早于用例启动时间的陈旧报告（防止复用上一用例的残留 HTML）。
    report_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    source_report = report_dir / f"playwright-{report_stamp}-test.html"
    source_report.write_text("<html>partial report</html>", encoding="utf-8")
    reports_cache_root = tmp_path / "report-cache"
    test_case = TestCase(
        id="TC020",
        name="必填校验",
        description="验证鱼缸名称必填",
        steps=["提交空名称"],
    )

    if stream_output:
        timeout_patch = patch(
            "evalapp.utils.process.run_streaming",
            return_value=StreamingResult(
                returncode=-15,
                stdout="",
                stderr="Command timed out after 300s",
                duration=300.0,
                timed_out=True,
            ),
        )
    else:
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["node"], timeout=300),
            ("", ""),
        ]
        mock_proc.pid = 1
        mock_proc.returncode = -9
        timeout_patch = ExitStack()
        timeout_patch.enter_context(
            patch("evalapp.evaluation.runner.test_phase.subprocess.Popen", return_value=mock_proc)
        )
        timeout_patch.enter_context(
            patch("evalapp.evaluation.runner.test_phase._kill_process_group")
        )

    with timeout_patch:
        result = run_single_test(
            tc=test_case,
            platform="miniprogram",
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            timeout=300,
            h5_url="http://127.0.0.1:3000",
            report_dir=report_dir,
            stream_output=stream_output,
            reports_cache_root=reports_cache_root,
        )

    assert result.status == "FAIL"
    assert result.error_details[0].code == "timeout"
    assert result.error_details[0].stage == "test"
    assert result.error_details[0].origin == "unknown"
    assert "recovered partial artifacts" in result.details
    assert (
        Path(result.report_path).read_text(encoding="utf-8")
        == "<html>partial report</html>"
    )
    page_diagnostics = result.verifications["page_diagnostics"]
    assert page_diagnostics["summary"]["js_error_count"] == 1
    assert page_diagnostics["summary"]["console_error_count"] == 1
    assert page_diagnostics["js_errors"][0]["message"] == "boom"


# ---------------------------------------------------------------------------
# TC_LAUNCH white-screen short-circuit (修复三)
# ---------------------------------------------------------------------------


class TestWhiteScreenShortCircuit:
    """白屏短路：TC_LAUNCH passed=True 但 white_screen.detected=True 时跳过后续用例。

    兑现用户设计意图「如果白屏就不往下走」——扩展后的短路条件
    ``not result.passed or _result_has_white_screen(result)`` 在 passed=True
    但白屏确凿命中时同样触发短路。
    """

    def test_white_screen_skips_remaining_cases(self, tmp_path):
        """TC_LAUNCH 通过但检测到白屏 → 后续用例被 SKIPPED。"""
        from evalapp.evaluation.results.models import TestCaseResult

        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "build.gradle").write_text("// mock")

        test_cases = [
            TestCase(id="TC_LAUNCH", name="启动验证", description="app launch",
                     steps=["启动"]),
            TestCase(id="TC001", name="功能A", description="feature A",
                     steps=["操作A"]),
            TestCase(id="TC002", name="功能B", description="feature B",
                     steps=["操作B"]),
        ]

        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        mock_build_result = {
            "success": True,
            "artifact_path": str(tmp_path / "app.apk"),
            "message": "",
            "duration_ms": 100,
        }
        mock_install_result = {"success": True, "message": "", "duration_ms": 100}

        ai_ui_entry = tmp_path / "ai-ui-test" / "dist" / "index.js"
        ai_ui_entry.parent.mkdir(parents=True, exist_ok=True)
        ai_ui_entry.write_text("// mock")

        # TC_LAUNCH 通过但白屏 detected=True
        white_screen_result = TestCaseResult(
            test_case_id="TC_LAUNCH",
            passed=True,
            status="PASS",
            details="App launched but white screen detected",
            verifications={"white_screen": {"detected": True}},
        )

        with patch.object(executor, "_build_project_with_retry",
                          return_value=mock_build_result):
            with patch.object(executor, "_install_artifact_with_retry",
                              return_value=mock_install_result):
                with patch.object(executor, "_resolve_package_name",
                                  return_value="com.test.app"):
                    with patch.object(executor, "_run_single_test",
                                      return_value=white_screen_result) as mock_run:
                        result = executor.execute_tests(
                            test_cases=test_cases,
                            project_path=str(project_path),
                            platform="android",
                            collect_device_logs=False,
                        )

        # 短路生效：只执行了 TC_LAUNCH（1 次调用）
        assert mock_run.call_count == 1
        # 后续用例被跳过
        assert len(result.test_results) == 3
        assert result.test_results[0].passed is True  # TC_LAUNCH
        assert result.test_results[1].status == "SKIPPED"  # TC001
        assert result.test_results[2].status == "SKIPPED"  # TC002
        assert all(not tr.passed for tr in result.test_results[1:])

    def test_no_white_screen_no_short_circuit(self, tmp_path):
        """对照：TC_LAUNCH 通过且无白屏 → 后续用例正常执行。"""
        from evalapp.evaluation.results.models import TestCaseResult

        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "build.gradle").write_text("// mock")

        test_cases = [
            TestCase(id="TC_LAUNCH", name="启动验证", description="app launch",
                     steps=["启动"]),
            TestCase(id="TC001", name="功能A", description="feature A",
                     steps=["操作A"]),
        ]

        executor = TestExecutor(
            ai_ui_test_dir=tmp_path / "ai-ui-test",
            build_app_script=tmp_path / "build_app.py",
            install_app_script=tmp_path / "install_app.py",
        )

        mock_build_result = {
            "success": True,
            "artifact_path": str(tmp_path / "app.apk"),
            "message": "",
            "duration_ms": 100,
        }
        mock_install_result = {"success": True, "message": "", "duration_ms": 100}

        ai_ui_entry = tmp_path / "ai-ui-test" / "dist" / "index.js"
        ai_ui_entry.parent.mkdir(parents=True, exist_ok=True)
        ai_ui_entry.write_text("// mock")

        # TC_LAUNCH 通过且无白屏
        launch_ok = TestCaseResult(
            test_case_id="TC_LAUNCH", passed=True, status="PASS", details="OK",
        )
        tc1_ok = TestCaseResult(
            test_case_id="TC001", passed=True, status="PASS", details="OK",
        )

        with patch.object(executor, "_build_project_with_retry",
                          return_value=mock_build_result):
            with patch.object(executor, "_install_artifact_with_retry",
                              return_value=mock_install_result):
                with patch.object(executor, "_resolve_package_name",
                                  return_value="com.test.app"):
                    with patch.object(executor, "_run_single_test",
                                      side_effect=[launch_ok, tc1_ok]) as mock_run:
                        result = executor.execute_tests(
                            test_cases=test_cases,
                            project_path=str(project_path),
                            platform="android",
                            collect_device_logs=False,
                        )

        # 无短路：两个用例都执行了
        assert mock_run.call_count == 2
        assert len(result.test_results) == 2
        assert all(tr.passed for tr in result.test_results)
