"""v1 本地交换契约：不评分、不读写文件，也不展开私有过程数据。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .models import EvalRun

ExecutionStatus = Literal["pending", "running", "completed", "failed", "skipped", "unknown"]
WarningCode = Literal[
    "missing_snapshot", "invalid_json", "invalid_structure", "identity_conflict",
    "source_mismatch", "unknown_execution_status", "source_changed",
]


class ContractModel(BaseModel):
    """包装层严格校验；消费者可忽略未来新增字段。"""

    model_config = ConfigDict(
        strict=True,
        json_schema_extra=lambda schema: schema.update(required=list(schema.get("properties", {}))),
    )


class Provenance(ContractModel):
    file: str | None
    pointer: str
    updated_at: str | None
    mtime_ns: int | None


class Consistency(ContractModel):
    mode: Literal["in_memory", "best_effort"]
    change_detected: bool | None


class ExportSource(ContractModel):
    kind: Literal["eval_run", "workspace"]
    run_id: str | None
    timestamp: str | None
    native_summary: dict[str, Any] | None
    consistency: Consistency


class Evidence(ContractModel):
    project_path: str | None
    e2e_report_path: str | None
    artifact_path: str | None
    h5_url: str | None


class EvaluationSnapshot(ContractModel):
    generation_success: bool | None
    success_rate: dict[str, Any] | None
    quality: dict[str, Any] | None
    experience: dict[str, Any] | None
    test_results: list[dict[str, Any]] | None
    error_message: str | None
    error_details: list[dict[str, Any]] | None
    evidence: Evidence
    provenance: Provenance


class ReportedScores(ContractModel):
    data: dict[str, Any]
    provenance: Provenance


class EvaluationObservation(ContractModel):
    test_results: list[dict[str, Any]] | None
    error_message: str | None
    error_details: list[dict[str, Any]] | None
    provenance: Provenance


class Execution(ContractModel):
    generate: ExecutionStatus = "unknown"
    evaluate: ExecutionStatus = "unknown"
    overall: ExecutionStatus = "unknown"
    provenance: Provenance | None = None


class ExportItem(ContractModel):
    sample_id: str | None
    prompt_id: str | None
    platform: str | None
    generator_name: str | None
    evaluation_snapshot: EvaluationSnapshot | None = None
    reported_scores: ReportedScores | None = None
    evaluation_observation: EvaluationObservation | None = None
    execution: Execution = Field(default_factory=Execution)


class ExportWarning(ContractModel):
    code: WarningCode
    message: str
    sample_id: str | None = None
    platform: str | None = None
    file: str | None = None
    pointer: str | None = None


class InterruptionEvidence(ContractModel):
    reason: Literal["manifest_unfinished", "manifest_terminal", "run_unfinished", "run_finished"]
    provenance: Provenance


class Interruption(ContractModel):
    suspected: bool | None = None
    evidence: list[InterruptionEvidence] = Field(default_factory=list)


class ExecutionCounts(ContractModel):
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    unknown: int = Field(ge=0)


class ExecutionSummary(ContractModel):
    unit: Literal["sample_platform"] = "sample_platform"
    total: int | None = Field(default=None, ge=0)
    counts: ExecutionCounts | None = None
    provenance: Provenance | None = None
    interruption: Interruption = Field(default_factory=Interruption)

    @model_validator(mode="after")
    def validate_counts(self) -> "ExecutionSummary":
        if (self.total is None) != (self.counts is None):
            raise ValueError("total 与 counts 必须同时已知或未知")
        if self.counts is not None and self.total != sum(self.counts.model_dump().values()):
            raise ValueError("total 必须等于六种执行状态的计数之和")
        return self


class ResultExport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    exported_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: ExportSource
    items: list[ExportItem]
    execution_summary: ExecutionSummary = Field(default_factory=ExecutionSummary)
    warnings: list[ExportWarning] = Field(default_factory=list)


def snapshot_from_dict(raw: dict, provenance: Provenance) -> EvaluationSnapshot:
    """只复制已记录字段；调用者负责验证字典类型，不补齐原生模型默认值。"""
    rd = raw.get("result_data") or {}
    return EvaluationSnapshot(
        **{key: deepcopy(raw.get(key)) for key in (
            "generation_success", "success_rate", "quality", "experience",
            "test_results", "error_message", "error_details",
        )},
        evidence=Evidence(
            project_path=raw.get("project_path"), e2e_report_path=raw.get("e2e_report_path"),
            artifact_path=rd.get("artifact_path"), h5_url=rd.get("h5_url"),
        ),
        provenance=provenance,
    )


def export_run(run: EvalRun) -> ResultExport:
    """转换当前内存对象，不读取工作区或重新计算 summary。"""
    items = []
    for index, result in enumerate(run.prompt_results):
        fields = {
            key: True for key in (
                "sample_id", "prompt_id", "platform", "generator_name",
                "generation_success", "success_rate", "quality", "experience",
                "test_results", "error_message", "error_details", "project_path", "e2e_report_path",
            )
        }
        fields["result_data"] = {"artifact_path", "h5_url"}
        raw = result.model_dump(mode="json", include=fields)
        items.append(ExportItem(
            **{key: raw.get(key) for key in ("sample_id", "prompt_id", "platform", "generator_name")},
            evaluation_snapshot=snapshot_from_dict(raw, Provenance(
                file=None, pointer=f"/prompt_results/{index}", updated_at=None, mtime_ns=None,
            )),
        ))
    return ResultExport(
        source=ExportSource(
            kind="eval_run", run_id=run.run_id, timestamp=run.timestamp,
            native_summary=deepcopy(run.summary.model_dump(mode="json")),
            consistency=Consistency(mode="in_memory", change_detected=None),
        ),
        items=items,
    )
