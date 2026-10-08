"""结构化错误只增加观测信息，不改变原有执行与评分。"""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from evalapp.evaluation.results.models import EvalRun, EvaluationError, PromptResult, TestCaseResult as CaseResult
from evalapp.evaluation.results.models.errors import error_from_exception, error_from_legacy
from evalapp.evaluation.runner.collectors import make_no_test_cases_result
from evalapp.evaluation.runner.evaluator import Evaluator
from evalapp.evaluation.runner.executor import TestExecutor as Executor
from evalapp.evaluation.runner.state import ExecutionResult
from evalapp.evaluation.runner.test_phase import collect_test_errors, execute_h5_tests
from evalapp.services.evaluation import EvaluationService
from evalapp.benchset.samples.models import EvalSample


def result(**kw):
    return PromptResult(prompt_id="s", sample_id="s", platform="h5", generator_name="test", generation_success=False, **kw)


def test_old_models_and_independent_defaults():
    a, b = result(), result()
    a.error_details.append(EvaluationError(code="test"))
    assert b.error_details == []
    assert PromptResult.model_validate(b.model_dump()).error_details == []
    assert ExecutionResult().error_details == []
    assert a.generation_success is False
    assert a.pass_rate == b.pass_rate == 0


@pytest.mark.parametrize("raw,origin", [("collector_error", "evaluator"), ("eval_script", "evaluator"), ("infra", "environment"), ("emulator", "environment"), ("other", "unknown")])
def test_explicit_legacy_mapping(raw, origin):
    assert error_from_legacy(raw, "timeout network generator").origin == origin
    assert error_from_legacy("other", "timeout").code == "unknown"


def test_timeout_evidence_and_assertions():
    assert error_from_exception(TimeoutError()).code == "timeout"
    assert error_from_exception(subprocess.TimeoutExpired("tool", 1)).code == "timeout"
    assert error_from_exception(ValueError("timeout")).code == "evaluation_exception"
    assert collect_test_errors([CaseResult(test_case_id="case", passed=False)]) == []
    errors = collect_test_errors([CaseResult(test_case_id="TC_LAUNCH", passed=False)])
    assert errors[0].code == "launch_failed"
    assert errors[0].origin == "unknown"


def test_no_cases_and_gate_keep_existing_policy(tmp_path):
    sample = EvalSample(sample_id="s", title="sample", requirement="test")
    pr = make_no_test_cases_result(item_id="s", sample=sample, platform="h5", requirement="test", generator_name="test")
    assert pr.generation_success is False
    assert pr.error_details[0].origin == "evaluator"
    evaluator = Evaluator(Mock(name="generator"), Mock(), Mock(), workspace_path=tmp_path)
    evaluator.generator.name = "test"
    gate = evaluator._make_gate_skipped_result(sample, "h5", "not ready")
    assert gate.error_details[0].code == "generation_not_ready"
    assert gate.error_details[0].origin == "unknown"
    assert [gate.success_rate.composite_score, gate.quality.composite_score, gate.experience.composite_score] == [0, 0, 0]


def test_missing_tool_is_environment(tmp_path):
    r = execute_h5_tests(test_cases=[], h5_url="http://localhost", platform="h5", ai_ui_test_dir=tmp_path, timeout=1, config=None, stream_output=False, reports_cache_root=tmp_path)
    assert r.error_details[0].origin == "environment"
    assert r.error_details[0].code == "test_tool_unavailable"


@pytest.mark.parametrize("failed_stage", ["build", "install"])
def test_executor_explicit_stage(tmp_path, failed_stage):
    executor = Executor(ai_ui_test_dir=tmp_path, build_app_script=tmp_path / "build", install_app_script=tmp_path / "install")
    build = {"success": failed_stage != "build", "artifact_path": "app", "message": "failed", "duration_ms": 1}
    with patch.object(executor, "_build_project_with_retry", return_value=build), patch.object(executor, "_install_artifact_with_retry", return_value={"success": False, "message": "failed"}), patch.object(executor, "_resolve_package_name", return_value="app"):
        r = executor.execute_tests([SimpleNamespace(id="case")], str(tmp_path), "android", collect_device_logs=False)
    assert r.error_details[0].stage == failed_stage
    assert r.error_details[0].origin == "unknown"
    assert r.test_results[0].passed is False


def test_persistence_and_partial_retest(tmp_path):
    service = EvaluationService(tmp_path)
    error = EvaluationError(stage="test", code="timeout")
    pr = result(error_details=[error], test_results=[CaseResult(test_case_id="a", passed=False, status="SKIPPED", duration=2, error_details=[error]), CaseResult(test_case_id="b", passed=True)])
    service.persist_sample(pr)
    raw = json.loads((tmp_path / "s/evaluation.json").read_text())["platforms"]["h5"]
    assert raw["error_details"][0]["code"] == "timeout"
    assert raw["test_results"][0]["status"] == "SKIPPED"
    assert raw["test_results"][0]["duration"] == 2
    service.persist_evaluation_results(EvalRun(prompt_results=[pr]))
    later = result(test_results=[CaseResult(test_case_id="b", passed=False, status="FAIL")])
    service._persist_evaluation_incremental([("s", "h5", later)], ["b"])
    merged = json.loads((tmp_path / "s/evaluation.json").read_text())["platforms"]["h5"]
    assert merged["test_results"][0] == raw["test_results"][0]
    assert merged["test_results"][1]["passed"] is False
    assert not (tmp_path / "s/sample_scores.json").exists()
    service._persist_evaluation_full([("s", "h5", pr)])
    assert json.loads((tmp_path / "s/evaluation.json").read_text())["platforms"]["h5"]["error_details"] == raw["error_details"]


def test_partial_retest_keeps_top_level_tool_error_and_clears_launch(tmp_path):
    service = EvaluationService(tmp_path)
    old = result(error_details=[EvaluationError(stage="launch", code="launch_failed")],
                 test_results=[CaseResult(test_case_id="TC_LAUNCH", passed=False)])
    service.persist_sample(old)
    missing = EvaluationError(origin="environment", stage="test", code="test_tool_unavailable")
    new = result(error_details=[missing], test_results=[CaseResult(test_case_id="TC_LAUNCH", passed=True)])
    service._persist_evaluation_incremental([("s", "h5", new)], ["TC_LAUNCH"])
    raw = json.loads((tmp_path / "s/evaluation.json").read_text())["platforms"]["h5"]
    assert raw["error_details"] == [missing.model_dump()]


@pytest.mark.parametrize("workers", [1, 2])
def test_serial_parallel_error_propagation(tmp_path, workers):
    generator = Mock()
    generator.name = "fixture"
    evaluator = Evaluator(generator, Mock(), Mock(), workspace_path=tmp_path)
    sample = EvalSample(sample_id="s", title="sample", requirement="test")
    pr = result(error_details=[EvaluationError(stage="test", code="timeout")])
    with patch.object(evaluator, "_persist_sample_scores") as persist:
        run = evaluator._execute_tasks([(sample, "h5", None, None)], Mock(return_value=pr), [sample], max_workers=workers)
    assert run.prompt_results[0].error_details == pr.error_details
    assert persist.call_args.args[2].error_details == pr.error_details


def test_serial_reraises_parallel_retains_unknown_exception(tmp_path):
    generator = Mock()
    generator.name = "fixture"
    evaluator = Evaluator(generator, Mock(), Mock(), workspace_path=tmp_path)
    sample = EvalSample(sample_id="s", title="sample", requirement="test")
    tasks = [(sample, "h5", None, None)]
    with patch.object(evaluator, "_persist_sample_scores"):
        with pytest.raises(ValueError):
            evaluator._execute_tasks(tasks, Mock(side_effect=ValueError("timeout")), [sample], max_workers=1)
        run = evaluator._execute_tasks(tasks, Mock(side_effect=ValueError("timeout")), [sample], max_workers=2)
    assert run.prompt_results[0].error_details[0].origin == "unknown"
    assert run.prompt_results[0].error_details[0].code == "evaluation_exception"


@pytest.mark.parametrize("generation_success", [True, False])
def test_generator_and_execution_error_transfer(tmp_path, generation_success):
    from evalapp.generators import GenerationResult
    from evalapp.evaluation.results.models import ProcessCollection
    from evalapp.evaluation.results.models.execution import compute_failure_rate_metrics
    generator = Mock()
    generator.name = "fixture"
    generator.generate.return_value = GenerationResult(success=generation_success, project_path=str(tmp_path), error="failure")
    executor, store = Mock(), Mock()
    store.load.return_value = [SimpleNamespace(id="a")]
    executor.execute_tests.return_value = ExecutionResult(error_details=[EvaluationError(stage="install", code="install_failed")])
    evaluator = Evaluator(generator, executor, store, workspace_path=tmp_path)
    sample = EvalSample(sample_id="s", title="sample", requirement="test")
    with patch.object(evaluator, "_collect_process_data", return_value=ProcessCollection()), patch("evalapp.evaluation.runner.evaluator.finalize_prompt_result"):
        pr = evaluator._evaluate_item("s", "test", "general", ["h5"], "h5", "sample", sample=sample)
    assert pr.error_details[0].code == ("install_failed" if generation_success else "generation_failed")
    assert pr.error_details[0].origin == ("unknown" if generation_success else "generator")
    previous = pr.model_copy(update={"error_details": []})
    assert compute_failure_rate_metrics([pr]) == compute_failure_rate_metrics([previous])
    assert pr.success_rate == previous.success_rate and pr.pass_rate == previous.pass_rate
