"""当前工作区的严格只读导出；单次读取、来源隔离、尽力检测并发变化。"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from ..evaluation.results.export import (
    Consistency, EvaluationObservation, Execution, ExecutionCounts, ExecutionStatus,
    ExecutionSummary, ExportItem, ExportSource, ExportWarning,
    InterruptionEvidence, Provenance, ReportedScores, ResultExport, snapshot_from_dict,
)
from .report_aggregator import _content_timestamp, _get_excluded_workspace_dirs, _parse_iso_ts, select_fresh_scores

RESULT_FILES = ("sample_scores.json", "scores.json", "evaluation.json")
MANIFEST = Path("execution_manifest.json")
STATUSES = get_args(ExecutionStatus)
IDENTITY = ("sample_id", "prompt_id", "platform", "generator_name")
TERMINAL = {"completed", "failed", "skipped"}


def _stat(path: Path, *, follow=True):
    try:
        return path.stat(follow_symlinks=follow)
    except FileNotFoundError:
        return None


def _fingerprint(value):
    if value is None:
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _entries(path: Path):
    try:
        with os.scandir(path) as entries:
            return sorted((Path(e.path) for e in entries), key=lambda p: p.name)
    except FileNotFoundError:
        return []


def _directory(path: Path):
    value = _stat(path)
    return value is not None and stat.S_ISDIR(value.st_mode)


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


class _Reader:
    """仅 FileNotFoundError 视为缺失；权限、stat 和扫描故障一律传递。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.warnings: list[ExportWarning] = []
        self.changed: set[Path] = set()
        self.cache: dict[Path, object] = {}
        self.read_stats: dict[Path, object] = {}
        self.bad: set[Path] = set()
        self.excluded = _get_excluded_workspace_dirs()
        self.initial, self.run_dirs = self.discover()

    def warn(self, code, path, pointer="", sample_id=None, platform=None):
        messages = {
            "invalid_json": "来源不是有效的 UTF-8 JSON。",
            "invalid_structure": "来源包含不支持的结构或字段类型。",
            "identity_conflict": "身份记录冲突；来源观察未强制合并。",
            "unknown_execution_status": "执行状态缺失或无法识别，保留 unknown。",
            "source_changed": "读取期间检测到来源变化；本次不是事务快照。",
            "source_mismatch": "来源时间不同或无法证明对应同一次评测，视图独立保留。",
            "missing_snapshot": "没有可用的完整原生评测快照。",
        }
        self.warnings.append(ExportWarning(
            code=code, message=messages[code], sample_id=sample_id, platform=platform,
            file=path.as_posix() if path is not None else None, pointer=pointer,
        ))

    def discover(self):
        files = {}

        def record(relative, *, follow=True):
            value = _stat(self.workspace / relative, follow=follow)
            if value is not None:
                files[relative] = value

        record(MANIFEST)
        for entry in _entries(self.workspace):
            if entry.name in self.excluded or entry.name.startswith(".") or not _directory(entry):
                continue
            for name in RESULT_FILES:
                record(Path(entry.name) / name)
        runs = self.workspace / "runs"
        candidates = []
        for entry in _entries(runs):
            if re.fullmatch(r"\d{8}_\d{6}", entry.name) and _directory(entry):
                candidates.append(entry.relative_to(self.workspace))
        candidates.sort(reverse=True)
        latest = runs / "latest"
        record(Path("runs/latest"), follow=False)
        if _directory(latest):
            resolved = latest.resolve()
            if resolved.is_relative_to(self.workspace):
                relative = resolved.relative_to(self.workspace)
                candidates = [relative] + [p for p in candidates if p != relative]
        for run in candidates:
            for name in ("phase", "command.json", "result_summary.json"):
                record(run / name)
        return files, candidates

    def read(self, path: Path, *, text=False):
        if path in self.cache:
            return self.cache[path]
        absolute = self.workspace / path
        before = _stat(absolute)
        if _fingerprint(before) != _fingerprint(self.initial.get(path)):
            self.changed.add(path)
        self.read_stats[path] = before
        try:
            content = absolute.read_bytes()
        except FileNotFoundError:
            content = None
            if before is not None:
                self.changed.add(path)
        after = _stat(absolute)
        if _fingerprint(before) != _fingerprint(after):
            self.changed.add(path)
        value = None
        if content is not None:
            try:
                decoded = content.decode("utf-8")
                value = decoded if text else json.loads(decoded, parse_constant=self._invalid_constant)
            except (UnicodeError, ValueError):
                self.warn("invalid_structure" if text else "invalid_json", path)
                self.bad.add(path)
            if not text and path not in self.bad and not isinstance(value, dict):
                self.warn("invalid_structure", path)
                self.bad.add(path)
                value = None
        self.cache[path] = value
        return value

    @staticmethod
    def _invalid_constant(value):
        raise ValueError("非标准 JSON 数值")

    def provenance(self, path: Path, pointer="", block=None, root=None):
        block, root = block or {}, root or {}
        timestamp = block.get("updated_at")
        if timestamp is None:
            timestamp = root.get("updated_at")
        if timestamp is not None and not isinstance(timestamp, str):
            self.warn("invalid_structure", path, pointer + "/updated_at")
        value = self.read_stats.get(path)
        return Provenance(
            file=path.as_posix(), pointer=pointer,
            updated_at=timestamp if isinstance(timestamp, str) else None,
            mtime_ns=value.st_mtime_ns if value else None,
        )

    def freshness(self, path, raw):
        timestamp = _content_timestamp(raw)
        value = self.read_stats.get(path)
        return timestamp if timestamp is not None else (value.st_mtime if value else 0.0)

    def finish(self):
        final, _ = self.discover()
        for path in self.initial.keys() | final.keys():
            if path.parts[0] == "runs" and path.name in ("command.json", "result_summary.json") and path not in self.cache and path in self.initial and path in final:
                continue
            if _fingerprint(self.initial.get(path)) != _fingerprint(final.get(path)):
                self.changed.add(path)
        for path in sorted(self.changed):
            self.warn("source_changed", path)
        return bool(self.changed)


def _field(raw, key, expected, reader, path, pointer):
    value = raw.get(key)
    valid = value is None or (isinstance(value, expected) and not (expected is int and isinstance(value, bool)))
    if expected is list and isinstance(value, list):
        valid = all(isinstance(item, dict) for item in value)
    if not valid:
        reader.warn("invalid_structure", path, pointer + "/" + _pointer(key))
        return None
    return value


def _identity(layers, reader, path, pointer, platform=None):
    identity = {}
    conflict = False
    for key in IDENTITY:
        values = [_field(layer, key, str, reader, path, pointer) for layer in layers]
        if key == "platform" and platform is not None:
            values.append(platform)
        known = [v for v in values if v is not None]
        identity[key] = known[0] if known else None
        conflict |= len(set(known)) > 1
    if conflict:
        reader.warn("identity_conflict", path, pointer, identity["sample_id"], identity["platform"])
    return identity, conflict


def _platforms(raw, reader, path):
    if raw is None:
        return {}
    platforms = raw.get("platforms")
    if not isinstance(platforms, dict):
        reader.warn("invalid_structure", path, "/platforms")
        return {}
    valid = {}
    for platform, block in platforms.items():
        if not isinstance(block, dict):
            reader.warn("invalid_structure", path, "/platforms/" + _pointer(platform))
        else:
            valid[platform] = block
    return valid


@dataclass
class _Row:
    directory: str
    pointer: str
    item: ExportItem
    conflict: bool = False


def _sample_rows(reader, directory):
    paths = {name: Path(directory) / name for name in RESULT_FILES}
    raw = {name: reader.read(path) for name, path in paths.items()}
    platforms = {name: _platforms(raw[name], reader, paths[name]) for name in RESULT_FILES}
    # 结构错误的平台不能进入旧转换器；纯仲裁规则和整文件粒度保持不变。
    clean = {name: ({**raw[name], "platforms": platforms[name]} if raw[name] is not None else None) for name in RESULT_FILES}
    selected, source = select_fresh_scores(
        clean["scores.json"], clean["sample_scores.json"],
        scores_freshness=reader.freshness(paths["scores.json"], raw["scores.json"]),
        sample_freshness=reader.freshness(paths["sample_scores.json"], raw["sample_scores.json"]),
    )
    observations = []
    for name in RESULT_FILES:
        path, root = paths[name], raw[name]
        for platform, block in platforms[name].items():
            pointer = "/platforms/" + _pointer(platform)
            if name == "sample_scores.json":
                pr = block.get("prompt_result")
                if pr is not None and not isinstance(pr, dict):
                    reader.warn("invalid_structure", path, pointer + "/prompt_result")
                    pr = None
                identity, conflict = _identity([pr or {}, block, root], reader, path, pointer, platform)
                item = ExportItem(**identity)
                if pr is not None:
                    native = {key: _field(pr, key, expected, reader, path, pointer + "/prompt_result") for key, expected in (
                        ("generation_success", bool), ("success_rate", dict), ("quality", dict), ("experience", dict),
                        ("test_results", list), ("error_message", str), ("error_details", list),
                        ("project_path", str), ("e2e_report_path", str),
                    )}
                    rd = _field(pr, "result_data", dict, reader, path, pointer + "/prompt_result") or {}
                    native["result_data"] = {key: _field(rd, key, str, reader, path, pointer + "/prompt_result/result_data") for key in ("artifact_path", "h5_url")}
                    item.evaluation_snapshot = snapshot_from_dict(native, reader.provenance(path, pointer + "/prompt_result", block, root))
            else:
                identity, conflict = _identity([block, root], reader, path, pointer, platform)
                item = ExportItem(**identity)
            if name == "evaluation.json":
                item.evaluation_observation = EvaluationObservation(
                    **{key: _field(block, key, expected, reader, path, pointer) for key, expected in (
                        ("test_results", list), ("error_message", str), ("error_details", list),
                    )}, provenance=reader.provenance(path, pointer, block, root),
                )
            if name == source + ".json" and selected is not None:
                if name == "sample_scores.json" and conflict and item.evaluation_snapshot is not None:
                    observations.append(_Row(directory, path.as_posix() + pointer + "/prompt_result", item, True))
                    identity, _ = _identity([block, root], reader, path, pointer, platform)
                    item = ExportItem(**identity)
                scores = selected.get("platforms", {}).get(platform)
                recorded = source != "sample_scores" or isinstance(block.get("scores"), dict)
                if source == "sample_scores" and "scores" in block and not recorded:
                    reader.warn("invalid_structure", path, pointer + "/scores")
                if isinstance(scores, dict) and recorded:
                    score_pointer = pointer + ("/scores" if source == "sample_scores" else "")
                    item.reported_scores = ReportedScores(data=scores, provenance=reader.provenance(path, score_pointer, block, root))
            if any((item.evaluation_snapshot, item.reported_scores, item.evaluation_observation)):
                observations.append(_Row(directory, path.as_posix() + pointer, item, conflict))
    return _merge_rows(observations, reader)


def _merge_rows(rows, reader):
    """仅在目录、平台和所有已知身份兼容时合并不同视图。"""
    groups = defaultdict(list)
    for row in rows:
        groups[row.item.platform].append(row)
    merged = []
    for group in groups.values():
        conflict = any(row.conflict for row in group)
        for key in IDENTITY:
            conflict |= len({getattr(row.item, key) for row in group if getattr(row.item, key) is not None}) > 1
        if conflict:
            for row in group:
                row.conflict = True
                reader.warn("identity_conflict", Path(row.pointer.split("/platforms/")[0]), "", row.item.sample_id, row.item.platform)
            merged.extend(group)
            continue
        target = group[0]
        for row in group[1:]:
            for key in IDENTITY:
                if getattr(target.item, key) is None:
                    setattr(target.item, key, getattr(row.item, key))
            for view in ("evaluation_snapshot", "reported_scores", "evaluation_observation"):
                if getattr(row.item, view) is not None:
                    setattr(target.item, view, getattr(row.item, view))
        merged.append(target)
    return merged


def _manifest(reader):
    raw = reader.read(MANIFEST)
    summary = ExecutionSummary()
    if raw is None:
        return [], summary, MANIFEST in reader.bad
    provenance = reader.provenance(MANIFEST, "", raw)
    summary.provenance = provenance
    items = raw.get("items")
    if not isinstance(items, list):
        reader.warn("invalid_structure", MANIFEST, "/items")
        return [], summary, True
    rows, complete, unknown = [], True, False
    for index, block in enumerate(items):
        pointer = f"/items/{index}"
        if not isinstance(block, dict):
            reader.warn("invalid_structure", MANIFEST, pointer)
            complete = False
            continue
        identity, conflict = _identity([block], reader, MANIFEST, pointer)
        if not identity["sample_id"] or not identity["platform"]:
            complete = False
            reader.warn("invalid_structure", MANIFEST, pointer)
            continue
        phases = block.get("phases")
        if not isinstance(phases, dict):
            phases = {}
        statuses = {"overall": block.get("overall_status")}
        for phase in ("generate", "evaluate"):
            value = phases.get(phase)
            statuses[phase] = value.get("status") if isinstance(value, dict) else None
        for key, value in statuses.items():
            if value not in STATUSES:
                statuses[key] = "unknown"
            if statuses[key] == "unknown":
                unknown = True
                reader.warn("unknown_execution_status", MANIFEST, pointer, identity["sample_id"], identity["platform"])
        item = ExportItem(**identity, execution=Execution(**statuses, provenance=reader.provenance(MANIFEST, pointer, block, raw)))
        rows.append(_Row(identity["sample_id"] or "", pointer, item, conflict))
    keys = Counter((r.item.sample_id, r.item.platform) for r in rows)
    for row in rows:
        if keys[row.item.sample_id, row.item.platform] > 1:
            row.conflict = True
            complete = False
            reader.warn("identity_conflict", MANIFEST, row.pointer, row.item.sample_id, row.item.platform)
    if complete:
        counts = Counter(r.item.execution.overall for r in rows)
        summary.counts = ExecutionCounts(**{s: counts[s] for s in STATUSES})
        summary.total = len(rows)
    unfinished = any(getattr(r.item.execution, key) in ("pending", "running") for r in rows for key in ("generate", "evaluate", "overall"))
    if unfinished:
        summary.interruption.evidence.append(InterruptionEvidence(reason="manifest_unfinished", provenance=provenance))
    elif complete and rows and not unknown and all(r.item.execution.overall in TERMINAL for r in rows):
        summary.interruption.evidence.append(InterruptionEvidence(reason="manifest_terminal", provenance=provenance))
    return rows, summary, not complete or unknown


def _run_evidence(reader):
    evidence, uncertain = [], False
    for directory in reader.run_dirs:
        phase_path = directory / "phase"
        phase = reader.read(phase_path, text=True)
        if phase_path in reader.bad:
            uncertain = True
            break
        if phase is None or not phase.strip():
            uncertain = True
            break
        if phase.strip() == "report":
            continue
        path = directory / "command.json"
        raw = reader.read(path)
        summary_path = directory / "result_summary.json"
        result = reader.read(summary_path)
        if raw is None or "finished_at" not in raw:
            if raw is not None:
                reader.warn("invalid_structure", path, "/finished_at")
            uncertain = True
            break
        finished = raw["finished_at"]
        if finished is None:
            evidence.append(InterruptionEvidence(reason="run_unfinished", provenance=reader.provenance(path, "/finished_at", raw)))
        elif _parse_iso_ts(finished) is None:
            reader.warn("invalid_structure", path, "/finished_at")
            uncertain = True
        elif summary_path in reader.bad:
            uncertain = True
        elif result is None:
            evidence.append(InterruptionEvidence(reason="run_unfinished", provenance=reader.provenance(path, "/finished_at", raw)))
        else:
            evidence.extend([
                InterruptionEvidence(reason="run_finished", provenance=reader.provenance(path, "/finished_at", raw)),
                InterruptionEvidence(reason="run_finished", provenance=reader.provenance(summary_path, "", result)),
            ])
        break
    return evidence, uncertain


def export_workspace(workspace: Path) -> ResultExport:
    """导出当前文件观察与 manifest 计划项；不创建任何源端文件。"""
    workspace = Path(workspace).resolve(strict=True)
    if not _directory(workspace):
        raise ValueError("输入必须是工作区目录")
    reader = _Reader(workspace)
    directories = sorted({p.parent.as_posix() for p in reader.initial if p.name in RESULT_FILES and len(p.parts) == 2})
    rows = [row for directory in directories for row in _sample_rows(reader, directory)]
    locations = defaultdict(set)
    for row in rows:
        if row.item.sample_id is not None and row.item.platform is not None:
            locations[row.item.sample_id, row.item.platform].add(row.directory)
    for row in rows:
        if len(locations[row.item.sample_id, row.item.platform]) > 1:
            row.conflict = True
            source = next(v.provenance for v in (
                row.item.evaluation_snapshot, row.item.reported_scores, row.item.evaluation_observation,
            ) if v is not None)
            reader.warn("identity_conflict", Path(source.file), source.pointer, row.item.sample_id, row.item.platform)
    planned, summary, uncertain = _manifest(reader)
    for row in planned:
        matches = [r for r in rows if (r.item.sample_id == row.item.sample_id or r.directory == row.item.sample_id) and r.item.platform == row.item.platform]
        identity_conflict = any(
            getattr(r.item, key) is not None and getattr(row.item, key) is not None
            and getattr(r.item, key) != getattr(row.item, key)
            for r in matches for key in IDENTITY
        )
        if matches and not identity_conflict and not row.conflict and not any(r.conflict for r in matches) and len(matches) == 1:
            matches[0].item.execution = row.item.execution
            for key in IDENTITY:
                if getattr(matches[0].item, key) is None:
                    setattr(matches[0].item, key, getattr(row.item, key))
        else:
            rows.append(row)
            if matches:
                uncertain = True
                summary.total = summary.counts = None
                reader.warn("identity_conflict", MANIFEST, row.pointer, row.item.sample_id, row.item.platform)
    extra, run_uncertain = _run_evidence(reader)
    summary.interruption.evidence.extend(extra)
    evidence = summary.interruption.evidence
    # 冲突时不能继续保留“全部终结”的断言证据。
    if summary.total is None:
        evidence = [e for e in evidence if e.reason != "manifest_terminal"]
        summary.interruption.evidence = evidence
    positive = any(e.reason.endswith("unfinished") for e in evidence)
    summary.interruption.suspected = True if positive else (None if uncertain or run_uncertain or not evidence else False)
    if not rows:
        raise ValueError("没有可识别的当前格式结果或计划项；旧 run_data/results 请通过 export_run 转换")
    rows.sort(key=lambda r: (r.directory, r.item.platform or "", r.pointer))
    for row in rows:
        item = row.item
        views = [getattr(item, name) for name in ("evaluation_snapshot", "reported_scores", "evaluation_observation")]
        sources = [v.provenance for v in views if v is not None]
        if item.evaluation_snapshot is None:
            source = sources[0] if sources else item.execution.provenance
            reader.warn("missing_snapshot", Path(source.file) if source and source.file else None, source.pointer if source else "", item.sample_id, item.platform)
        if len(sources) > 1 and (any(p.updated_at is None for p in sources) or len({p.updated_at for p in sources}) > 1):
            reader.warn("source_mismatch", Path(sources[-1].file), sources[-1].pointer, item.sample_id, item.platform)
    changed = reader.finish()
    return ResultExport(
        source=ExportSource(kind="workspace", run_id=None, timestamp=None, native_summary=None,
                            consistency=Consistency(mode="best_effort", change_detected=changed)),
        items=[r.item for r in rows], execution_summary=summary, warnings=reader.warnings,
    )
