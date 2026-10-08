"""固定 v1 包装契约；来源的原生记录不经模型默认值补齐。"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from evalapp.evaluation.results.export import ResultExport, export_run
from evalapp.evaluation.results.models import EvalRun, PromptResult, TestCaseResult as CaseResult
from evalapp.evaluation.metrics.models import SuccessRateMetrics, QualityMetrics, ExperienceMetrics

TOP = {"schema_version", "exported_at", "source", "items", "execution_summary", "warnings"}
ITEM = {"sample_id", "prompt_id", "platform", "generator_name", "evaluation_snapshot", "reported_scores", "evaluation_observation", "execution"}
STATES = {"pending", "running", "completed", "failed", "skipped", "unknown"}


def validate_contract(payload):
    assert set(payload) == TOP
    assert payload["schema_version"] == "1.0"
    assert datetime.fromisoformat(payload["exported_at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert set(payload["source"]) == {"kind", "run_id", "timestamp", "native_summary", "consistency"}
    for item in payload["items"]:
        assert set(item) == ITEM
        assert set(item["execution"]) == {"generate", "evaluate", "overall", "provenance"}
        for view in ("evaluation_snapshot", "reported_scores", "evaluation_observation"):
            if item[view] is not None:
                assert set(item[view]["provenance"]) == {"file", "pointer", "updated_at", "mtime_ns"}
    assert ResultExport.model_validate_json(json.dumps(payload)).model_dump(mode="json") == payload


def put(workspace, relative, value):
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def plan_item(sid="s", platform="h5", status="completed"):
    return {"sample_id": sid, "platform": platform, "overall_status": status,
            "phases": {"generate": {"status": "completed"}, "evaluate": {"status": status}}}


def workspace_fixture(workspace):
    raw = {"sample_id": "s", "prompt_id": "p", "platform": "h5", "generator_name": "fixture",
           "generation_success": False, "success_rate": {"composite_score": 0}, "quality": None,
           "experience": {"composite_score": 12, "detail": None},
           "test_results": [{"test_case_id": "same", "passed": False, "status": "SKIPPED"},
                            {"test_case_id": "same", "passed": True}], "error_details": [],
           "process_data": {"raw": {"private": "never exported"}},
           "result_data": {"artifact_path": "artifact.apk", "e2e_result": {"test_results": [{"test_case_id": "self-report"}]}}}
    put(workspace, "s/sample_scores.json", {"sample_id": "s", "platforms": {"h5": {
        "updated_at": "2025-01-01T00:00:00", "prompt_result": raw, "scores": {"quality": 0}}}})
    put(workspace, "s/scores.json", {"sample_id": "s", "updated_at": "2025-02-01T00:00:00", "platforms": {"h5": {"quality_score": 90}}})
    put(workspace, "s/evaluation.json", {"sample_id": "s", "platforms": {"h5": {
        "updated_at": "2025-03-01T00:00:00", "test_results": [{"test_case_id": "same", "passed": True}]}}})
    put(workspace, "execution_manifest.json", {"items": [plan_item(), plan_item(platform="ios", status="running")]})
    return raw


def test_memory_contract_and_no_side_effects():
    pr = PromptResult(sample_id="", prompt_id="p", platform="h5", generator_name="fixture", generation_success=False,
                      success_rate=SuccessRateMetrics(composite_score=0), quality=QualityMetrics(composite_score=0),
                      experience=ExperienceMetrics(composite_score=10),
                      test_results=[CaseResult(test_case_id="same", passed=False, status="SKIPPED")])
    run = EvalRun(timestamp="2025-01-01T00:00:00", prompt_results=[pr, pr.model_copy(deep=True)])
    before = run.model_dump(mode="json")
    with patch.object(EvalRun, "compute_summary", side_effect=AssertionError("不能评分")), patch.object(Path, "read_bytes", side_effect=AssertionError("不能读文件")):
        result = export_run(run)
    payload = result.model_dump(mode="json")
    validate_contract(payload)
    assert payload["source"]["native_summary"] == before["summary"]
    assert payload["source"]["timestamp"] == run.timestamp
    assert payload["items"][0]["sample_id"] == ""
    assert len(payload["items"]) == 2
    assert payload["execution_summary"]["total"] is None
    assert payload["execution_summary"]["interruption"]["suspected"] is None
    assert run.model_dump(mode="json") == before
    result.items[0].evaluation_snapshot.quality["composite_score"] = 99
    assert pr.quality.composite_score == 0


def test_schema_enums_and_count_invariants():
    schema = ResultExport.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    assert set(schema["required"]) == TOP
    for definition in schema["$defs"].values():
        if "properties" in definition:
            assert set(definition["required"]) == set(definition["properties"])
    assert set(schema["$defs"]["ExecutionCounts"]["properties"]) == STATES
    payload = export_run(EvalRun()).model_dump(mode="json")
    payload["execution_summary"]["total"] = 0
    with pytest.raises(ValidationError):
        ResultExport.model_validate_json(json.dumps(payload))


def test_workspace_views_and_manifest(tmp_path):
    from evalapp.services.result_export import export_workspace
    raw = workspace_fixture(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    payload = export_workspace(tmp_path).model_dump(mode="json")
    validate_contract(payload)
    item = next(i for i in payload["items"] if i["platform"] == "h5")
    snapshot = item["evaluation_snapshot"]
    assert snapshot["quality"] is None
    assert snapshot["success_rate"] == raw["success_rate"]
    assert snapshot["test_results"] == raw["test_results"]
    assert snapshot["error_message"] is None
    assert snapshot["error_details"] == []
    assert "process_data" not in snapshot
    assert item["reported_scores"]["data"] == {"quality_score": 90}
    assert item["evaluation_observation"]["test_results"] == [{"test_case_id": "same", "passed": True}]
    assert item["execution"]["overall"] == "completed"
    assert payload["execution_summary"]["counts"]["running"] == 1
    assert payload["execution_summary"]["total"] == 2
    assert payload["execution_summary"]["interruption"]["suspected"] is True
    assert any(w["code"] == "source_mismatch" for w in payload["warnings"])
    assert payload["source"]["consistency"] == {"mode": "best_effort", "change_detected": False}
    assert payload["source"]["run_id"] is None
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("manifest,expected_count,suspected", [
    (None, None, None), ({"items": []}, 0, None), ({"items": [plan_item()]}, 1, False),
    ({"items": [plan_item(status="pending")]}, 1, True),
    ({"items": [plan_item(status="skipped")]}, 1, False),
    ({"items": [plan_item(status="failed")]}, 1, False),
    ({"items": [plan_item(status="unrecognized")]}, 1, None),
    ({"items": [{"sample_id": "s", "platform": "h5"}]}, 1, None),
    ({"items": [plan_item(), plan_item()]}, None, None),
    ({"items": [plan_item(), {"sample_id": "s"}]}, None, None),
    ({"items": [False]}, None, None), ({"items": {}}, None, None),
])
def test_manifest_unknown_and_denominator(tmp_path, manifest, expected_count, suspected):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/scores.json", {"sample_id": "s", "platforms": {"h5": {"quality_score": 0}}})
    put(tmp_path, "extra/scores.json", {"sample_id": "extra", "platforms": {"h5": {"quality_score": 100}}})
    if manifest is not None:
        put(tmp_path, "execution_manifest.json", manifest)
    result = export_workspace(tmp_path)
    validate_contract(result.model_dump(mode="json"))
    summary = result.execution_summary
    assert summary.total == expected_count
    assert summary.interruption.suspected is suspected
    assert any(i.sample_id == "extra" for i in result.items)
    assert all(i.evaluation_snapshot is None for i in result.items)
    if expected_count is not None:
        assert sum(summary.counts.model_dump().values()) == expected_count


@pytest.mark.parametrize("value,warning", [(b"bad", "invalid_json"), (b"\xff", "invalid_json"), (b"[]", "invalid_structure"), (b"null", "invalid_structure"), (b'{"a":NaN}', "invalid_json")])
@pytest.mark.parametrize("relative", ["s/sample_scores.json", "s/evaluation.json", "execution_manifest.json"])
def test_corrupt_sources_are_warnings(tmp_path, value, warning, relative):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/scores.json", {"platforms": {"h5": {"quality_score": 0}}})
    path = tmp_path / relative
    path.write_bytes(value)
    result = export_workspace(tmp_path)
    assert any(w.code == warning and w.file == relative for w in result.warnings)
    assert result.execution_summary.total is None
    assert result.execution_summary.interruption.suspected is None
    assert "bad" not in " ".join(w.message for w in result.warnings)


@pytest.mark.parametrize("site", ["read", "stat", "scan"])
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_io_errors_propagate_and_cli_preserves_output(tmp_path, site, error_type):
    from evalapp.services.result_export import export_workspace
    from evalapp.commands.exporting import export_cmd
    workspace = tmp_path / "ws"
    workspace_fixture(workspace)
    target = tmp_path / "export.json"
    target.write_text("existing", encoding="utf-8")
    def fail(*args, **kwargs):
        raise error_type("sensitive text must not be echoed")
    method = {"read": "read_bytes", "stat": "stat"}.get(site)
    context = patch.object(Path, method, fail) if method else patch("evalapp.services.result_export.os.scandir", fail)
    with context:
        with pytest.raises(error_type):
            export_workspace(workspace)
        response = CliRunner().invoke(export_cmd, ["--workspace", str(workspace), "--output", str(target), "--overwrite"])
    assert response.exit_code != 0
    assert "sensitive text" not in response.output
    assert target.read_text() == "existing"


@pytest.mark.parametrize("action", ["replace", "add", "delete", "manifest", "run"])
def test_concurrent_change_and_read_once(tmp_path, monkeypatch, action):
    from evalapp.services.result_export import export_workspace
    workspace_fixture(tmp_path)
    reads = {}
    original = Path.read_bytes
    victim = tmp_path / "s/sample_scores.json"
    def read(path):
        reads[path] = reads.get(path, 0) + 1
        content = original(path)
        if path == victim:
            if action == "replace":
                replacement = put(tmp_path, "replacement.json", {"sample_id": "s", "platforms": {"h5": {"prompt_result": {"quality": {"composite_score": 999}}}}})
                replacement.replace(victim)
            elif action == "add":
                put(tmp_path, "new/scores.json", {"platforms": {"ios": {"quality_score": 1}}})
            elif action == "delete":
                victim.unlink()
            elif action == "manifest":
                put(tmp_path, "execution_manifest.json", {"items": [plan_item(status="running")]})
            else:
                put(tmp_path, "runs/20250101_000000/command.json", {"finished_at": None})
                (tmp_path / "runs/20250101_000000/phase").write_text("evaluate")
        return content
    monkeypatch.setattr(Path, "read_bytes", read)
    result = export_workspace(tmp_path)
    assert result.source.consistency.change_detected is True
    assert any(w.code == "source_changed" for w in result.warnings)
    assert all(count == 1 for count in reads.values())
    assert next(i for i in result.items if i.platform == "h5").evaluation_snapshot.quality is None
    assert not any(i.platform == "ios" and i.reported_scores for i in result.items)


@pytest.mark.parametrize("finished,summary,expected", [(None, True, True), ("2025-01-01T00:00:00", True, False), ("2025-01-01T00:00:00", False, True), ("invalid", True, None), ("absent", False, None)])
def test_run_interruption_evidence(tmp_path, finished, summary, expected):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/scores.json", {"platforms": {"h5": {}}})
    command = {} if finished == "absent" else {"finished_at": finished}
    path = put(tmp_path, "runs/20250101_000000/command.json", command)
    (path.parent / "phase").write_text("evaluate")
    if summary:
        put(tmp_path, "runs/20250101_000000/result_summary.json", {})
    later = put(tmp_path, "runs/20250201_000000/command.json", {"finished_at": None})
    (later.parent / "phase").write_text("report")
    (tmp_path / "runs/latest").symlink_to(later.parent.name)
    result = export_workspace(tmp_path)
    assert result.execution_summary.interruption.suspected is expected
    assert all("20250101" in e.provenance.file for e in result.execution_summary.interruption.evidence)
    if finished in ("invalid", "absent"):
        assert any(w.code == "invalid_structure" and w.file == "runs/20250101_000000/command.json"
                   and w.pointer == "/finished_at" for w in result.warnings)


def test_corrupt_run_overrides_terminal_manifest(tmp_path):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "execution_manifest.json", {"items": [plan_item()]})
    path = put(tmp_path, "runs/20250101_000000/command.json", {})
    (path.parent / "phase").write_text("evaluate")
    path.write_text("corrupt")
    result = export_workspace(tmp_path)
    assert result.execution_summary.interruption.suspected is None


@pytest.mark.parametrize("scores_ts,snapshot_ts,expected", [
    ("2025-01-01T00:00:00", "2025-01-01T00:00:00", 90),
    ("2025-01-01T00:00:00", "2025-02-01T00:00:00", 10),
    (None, None, 90),
])
def test_freshness_matches_report(tmp_path, scores_ts, snapshot_ts, expected):
    import os
    from evalapp.services.result_export import export_workspace
    from evalapp.services.report_aggregator import _resolve_fresh_scores
    a = put(tmp_path, "s/scores.json", {"updated_at": scores_ts, "platforms": {"h5": {"quality_score": 90}}})
    b = put(tmp_path, "s/sample_scores.json", {"updated_at": snapshot_ts, "platforms": {"h5": {"scores": {"quality": 10}}}})
    os.utime(a, (100, 100))
    os.utime(b, (100, 100))
    report, _ = _resolve_fresh_scores(a.parent)
    result = export_workspace(tmp_path)
    assert result.items[0].reported_scores.data == report["platforms"]["h5"]
    assert result.items[0].reported_scores.data["quality_score"] == expected


def test_sample_scores_only_and_json_pointer(tmp_path):
    from evalapp.services.result_export import export_workspace
    from evalapp.services.report_aggregator import _resolve_fresh_scores
    put(tmp_path, "s/sample_scores.json", {"platforms": {"a~/b": {"scores": {"quality": 0}, "prompt_result": {"test_results": []}}}})
    result = export_workspace(tmp_path)
    item = result.items[0]
    assert item.sample_id is None and item.prompt_id is None
    assert item.evaluation_snapshot.generation_success is None
    assert item.evaluation_snapshot.test_results == []
    assert item.evaluation_snapshot.provenance.pointer == "/platforms/a~0~1b/prompt_result"
    assert item.reported_scores.provenance.pointer == "/platforms/a~0~1b/scores"
    assert _resolve_fresh_scores(tmp_path / "s", allow_sample_scores_fallback=False) == (None, "")


def test_identity_conflicts_preserve_observations(tmp_path):
    from evalapp.services.result_export import export_workspace
    workspace_fixture(tmp_path)
    put(tmp_path, "s/evaluation.json", {"sample_id": "other", "platforms": {"h5": {"test_results": []}}})
    result = export_workspace(tmp_path)
    assert len([i for i in result.items if i.platform == "h5"]) == 4
    assert any(i.sample_id == "other" for i in result.items)
    assert any(w.code == "identity_conflict" for w in result.warnings)
    assert result.execution_summary.total is None


@pytest.mark.parametrize("status", [None, "completed", "running"])
def test_cross_directory_identity_conflict_preserves_sources(tmp_path, status):
    from evalapp.services.result_export import export_workspace
    for directory, score in (("a", 10), ("b", 90)):
        put(tmp_path, f"{directory}/scores.json", {"sample_id": "s", "platforms": {"h5": {"quality_score": score}}})
    if status is not None:
        put(tmp_path, "execution_manifest.json", {"items": [plan_item(status=status)]})
    result = export_workspace(tmp_path)
    validate_contract(result.model_dump(mode="json"))
    observed = [item for item in result.items if item.reported_scores is not None]
    assert [(item.reported_scores.provenance.file, item.reported_scores.data["quality_score"])
            for item in observed] == [("a/scores.json", 10), ("b/scores.json", 90)]
    assert all(item.sample_id == "s" and item.platform == "h5" and item.execution.overall == "unknown"
               for item in observed)
    assert {w.file for w in result.warnings if w.code == "identity_conflict"} >= {"a/scores.json", "b/scores.json"}
    assert result.execution_summary.total is None and result.execution_summary.counts is None
    assert result.execution_summary.interruption.suspected is (True if status == "running" else None)
    assert not any(e.reason == "manifest_terminal" for e in result.execution_summary.interruption.evidence)
    assert len(result.items) == (2 if status is None else 3)
    if status is not None:
        assert result.items[-1].execution.overall == status
        assert result.items[-1].execution.provenance.file == "execution_manifest.json"


def test_missing_scalars_and_invalid_fields(tmp_path):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/sample_scores.json", {"platforms": {"h5": {"prompt_result": {
        "sample_id": "", "prompt_id": "p", "generation_success": "false", "quality": [],
        "test_results": [False], "error_details": "bad", "result_data": 1,
    }}, "invalid": False}})
    result = export_workspace(tmp_path)
    assert result.items[0].sample_id == ""
    snapshot = result.items[0].evaluation_snapshot
    assert snapshot.generation_success is None and snapshot.quality is None and snapshot.test_results is None
    assert snapshot.error_details is None
    assert any(w.code == "invalid_structure" for w in result.warnings)


def test_cli_output_protection_and_read_only(tmp_path, monkeypatch):
    from evalapp.cli import main
    from evalapp.evaluation.execution_manifest import ExecutionManifest
    workspace = tmp_path / "ws"
    workspace_fixture(workspace)
    original = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    output = tmp_path / "out.json"
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    def forbidden(*args, **kwargs):
        raise AssertionError("导出不能调用运行、评分或写入服务")
    monkeypatch.setattr(ExecutionManifest, "load", forbidden)
    monkeypatch.setattr(ExecutionManifest, "save", forbidden)
    monkeypatch.setattr(EvalRun, "compute_summary", forbidden)
    for target in (workspace / "out.json", alias / "out.json"):
        response = CliRunner().invoke(main, ["export", "--workspace", str(workspace), "--output", str(target), "--overwrite"])
        assert response.exit_code != 0
    response = CliRunner().invoke(main, ["export", "--workspace", str(workspace), "--output", str(output)])
    assert response.exit_code == 0, response.output
    validate_contract(json.loads(output.read_text()))
    assert "脱敏" in response.output
    old = output.read_bytes()
    assert CliRunner().invoke(main, ["export", "--workspace", str(workspace), "--output", str(output)]).exit_code != 0
    assert output.read_bytes() == old
    assert CliRunner().invoke(main, ["export", "--workspace", str(workspace), "--output", str(output), "--overwrite"]).exit_code == 0
    assert original == {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}


def test_cli_no_results_and_atomic_failure(tmp_path):
    from evalapp.commands.exporting import export_cmd, _atomic_output
    from evalapp.services.result_export import export_workspace
    workspace = tmp_path / "empty"
    workspace.mkdir()
    output = tmp_path / "out.json"
    with pytest.raises(ValueError):
        export_workspace(workspace)
    assert CliRunner().invoke(export_cmd, ["--workspace", str(workspace), "--output", str(output)]).exit_code != 0
    assert not output.exists()
    output.write_text("old")
    with patch("evalapp.commands.exporting.os.replace", side_effect=OSError("fail")):
        with pytest.raises(OSError):
            _atomic_output(output, "new", True)
    assert output.read_text() == "old"
    assert not list(tmp_path.glob(".out.json.*"))
    with pytest.raises(FileExistsError):
        _atomic_output(output, "new", False)
    assert output.read_text() == "old"


def test_nested_identity_conflict_separates_snapshot_and_scores(tmp_path):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/sample_scores.json", {"sample_id": "s", "platforms": {"h5": {
        "scores": {"quality": 10}, "prompt_result": {"sample_id": "other", "quality": {"composite_score": 0}},
    }}})
    result = export_workspace(tmp_path)
    assert len(result.items) == 2
    assert any(i.sample_id == "other" and i.evaluation_snapshot and not i.reported_scores for i in result.items)
    assert any(i.sample_id == "s" and i.reported_scores and not i.evaluation_snapshot for i in result.items)


def test_whole_file_selection_preserves_platform_provenance(tmp_path):
    from evalapp.services.result_export import export_workspace
    put(tmp_path, "s/scores.json", {"updated_at": "2025-02-01T00:00:00", "platforms": {"h5": {"quality_score": 90}, "ios": {"quality_score": 90}}})
    put(tmp_path, "s/sample_scores.json", {"platforms": {
        "h5": {"updated_at": "2025-01-01T00:00:00", "scores": {"quality": 10}},
        "ios": {"updated_at": "2025-03-01T00:00:00", "scores": {"quality": 20}},
    }})
    result = export_workspace(tmp_path)
    assert [i.reported_scores.data["quality_score"] for i in result.items] == [10, 20]
    assert result.items[0].reported_scores.provenance.updated_at == "2025-01-01T00:00:00"


def test_same_case_ids_across_samples_and_platforms(tmp_path):
    from evalapp.services.result_export import export_workspace
    for sample in ("b", "a"):
        put(tmp_path, f"{sample}/sample_scores.json", {"sample_id": sample, "platforms": {
            p: {"prompt_result": {"test_results": [{"test_case_id": "same", "passed": False}]}} for p in ("ios", "h5")
        }})
    result = export_workspace(tmp_path)
    assert [(i.sample_id, i.platform) for i in result.items] == [(s, p) for s in ("a", "b") for p in ("h5", "ios")]
    assert all(i.evaluation_snapshot.test_results == [{"test_case_id": "same", "passed": False}] for i in result.items)
    assert all(i.reported_scores is None for i in result.items)


def test_schema_shared_fixture_for_memory_workspace_and_cli(tmp_path):
    from evalapp.services.result_export import export_workspace
    from evalapp.commands.exporting import export_cmd
    pr = PromptResult(sample_id="s", prompt_id="p", platform="web", generator_name="fixture", generation_success=True,
                      quality=QualityMetrics(composite_score=0), test_results=[CaseResult(test_case_id="same", passed=False, status="SKIPPED")])
    workspace = tmp_path / "ws"
    put(workspace, "s/sample_scores.json", {"sample_id": "s", "platforms": {"web": {"prompt_result": pr.model_dump(mode="json")}}})
    output = tmp_path / "export.json"
    assert CliRunner().invoke(export_cmd, ["--workspace", str(workspace), "--output", str(output)]).exit_code == 0
    payloads = [export_run(EvalRun(prompt_results=[pr])).model_dump(mode="json"), export_workspace(workspace).model_dump(mode="json"), json.loads(output.read_text())]
    for payload in payloads:
        validate_contract(payload)
    snapshots = [{k: v for k, v in p["items"][0]["evaluation_snapshot"].items() if k != "provenance"} for p in payloads]
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_source_missing_during_read_and_source_exclusions(tmp_path, monkeypatch):
    from evalapp.services.result_export import export_workspace
    workspace_fixture(tmp_path)
    put(tmp_path, "report/scores.json", {"platforms": {"hidden": {}}})
    original = Path.read_bytes
    def read(path):
        if path.name == "evaluation.json":
            raise FileNotFoundError()
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    result = export_workspace(tmp_path)
    assert all(i.evaluation_observation is None for i in result.items)
    assert not any(i.platform == "hidden" for i in result.items)


def test_output_file_symlink_into_input_is_rejected(tmp_path):
    from evalapp.commands.exporting import export_cmd
    workspace = tmp_path / "ws"
    workspace_fixture(workspace)
    link = tmp_path / "link.json"
    source = workspace / "s/scores.json"
    before = source.read_bytes()
    link.symlink_to(source)
    response = CliRunner().invoke(export_cmd, ["--workspace", str(workspace), "--output", str(link), "--overwrite"])
    assert response.exit_code != 0
    assert source.read_bytes() == before
