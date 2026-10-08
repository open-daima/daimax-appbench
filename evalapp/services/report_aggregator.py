"""报告聚合器 - 从各样本 sample_report.json 动态聚合生成报告数据。

作为报告生成的唯一入口，扫描工作区下所有样本数据，
计算聚合指标，输出与现有 API 返回的 report_data 结构兼容的数据。

数据源优先级：
  1. {sample_id}/scores.json — 各平台评分（核心指标来源）
  2. {sample_id}/generation.json — token、duration 等生成数据
  3. {sample_id}/sample_report.json — 历史兼容的聚合数据

当工作区不存在任何有效的样本数据文件时，返回 None，
让调用方回退到读取 report_data.json。

新鲜度仲裁（scores.json vs sample_scores.json）：
  ``sample_scores.json`` 由评测器在每个样本×平台完成时实时写入，
  ``scores.json`` 则由批次收尾的 ``persist_all`` 与后续报告阶段写入。评测进程
  被中断时 ``persist_all`` 不会执行，``scores.json`` 会停留在上一轮的旧数据
  （甚至被报告阶段基于旧数据重写一次），掩盖新鲜的实时结果、造成假 0 分。
  因此两者同时存在时按新鲜度取较新者（见 :func:`_resolve_fresh_scores`）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from ..utils.currency import extract_cost_usd
from ..utils.logging import get_logger

logger = get_logger(__name__)

# 非样本目录黑名单 - 扫描时跳过这些目录名
_NON_SAMPLE_DIRS = frozenset({
    "runs",
    "report",
    "archive",
    "analyzer",
    "e2e_reports",
    "stability_logs",
    "__pycache__",
    ".venv",
    "node_modules",
    "dist",
    ".DS_Store",
})


def _get_excluded_workspace_dirs() -> frozenset[str]:
    """Return the combined set of non-sample dirs (static + config-driven)."""
    try:
        from ..config import get_config
        extra = get_config().excluded_workspace_dirs or []
    except Exception:
        extra = []
    if not extra:
        return _NON_SAMPLE_DIRS
    return _NON_SAMPLE_DIRS | frozenset(extra)


# ===================== 新鲜度仲裁（scores.json vs sample_scores.json） =====================


def _parse_iso_ts(value) -> float | None:
    """将 ISO 时间字符串解析为 epoch 秒；无法解析时返回 None。

    naive 时间串按本地时区解释（与 ``write_sample_scores`` /
    ``EvaluationService.persist_scores`` 写入 ``updated_at`` 的约定一致），
    因此与文件 mtime 处于同一时间基准，可直接比较。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _content_timestamp(data) -> float | None:
    """提取 JSON 内容里的时间戳（epoch 秒）。

    优先顶层 ``updated_at``；缺失时取各平台 ``updated_at`` 的最大值
    （多平台分批完成时，文件整体的新鲜度等于最新一个平台）。
    """
    if not isinstance(data, dict):
        return None
    ts = _parse_iso_ts(data.get("updated_at"))
    if ts is not None:
        return ts
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        return None
    stamps = [
        _parse_iso_ts(p.get("updated_at"))
        for p in platforms.values()
        if isinstance(p, dict)
    ]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def _freshness(path: Path, data) -> float:
    """文件新鲜度（epoch 秒）：优先内容时间戳，缺失时回退到 mtime。

    同一完成事件写入的两文件内容时间戳严格相等（见
    ``evaluator._persist_sample_scores``），配合 :func:`_resolve_fresh_scores`
    的严格大于比较，相等时一律选信息更完整的 scores.json，避免跨秒边界
    非确定性地选中降级转换结构（丢 backend_completeness 等字段）。

    注意：这里返回**同一量纲的 float**（内容时间戳或 mtime 二选一），
    不可改为 ``(是否有内容时间戳, 值)`` 之类的 tuple 键——历史批次的
    scores.json 无 ``updated_at`` 而 sample_scores.json 有时，tuple 首元素
    差异会使 sample_scores 恒定胜出，破坏以 scores.json 为准的聚合。
    两侧均无内容时间戳时才回退到 mtime 比较。
    """
    ts = _content_timestamp(data)
    if ts is not None:
        return ts
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _resolve_fresh_scores(
    sample_dir: Path,
    *,
    allow_sample_scores_fallback: bool = True,
) -> tuple[dict | None, str]:
    """按新鲜度解析样本的评分数据。

    仲裁规则：

    * 两者都存在 → 仅当 ``sample_scores.json`` 的时间戳**严格大于**
      ``scores.json`` 时才选前者；相等（同一完成事件写入，两文件时间戳
      严格相等）或无法判定时一律取 ``scores.json``，因为它是信息更完整
      的产物（含 stability / aesthetics / backend 等字段，而
      ``sample_scores.json`` 转换后只有汇总分值）。
    * 仅 ``sample_scores.json`` 存在 → 走既有回退路径（转换后返回）。
      历史 miniprogram 批次只有这个文件，必须继续工作。
    * 仅 ``scores.json`` 存在 → 直接返回（绝大多数批次的正常路径）。

    Args:
        sample_dir: 样本目录。
        allow_sample_scores_fallback: ``scores.json`` 缺失/损坏时是否允许回退到
            ``sample_scores.json``。``get_sample_report`` 传 True（保留既有回退
            语义）；``aggregate`` 传 False，以保持“无 scores.json 则跳过该样本”
            的原有行为不变（避免只有 sample_scores.json 的历史批次被新增
            纳入聚合，影响其他平台/批次的正常聚合结果）。

    Returns:
        ``(scores_raw, source)``；source 为 ``"scores"`` / ``"sample_scores"`` /
        ``""``（无可用数据）。scores_raw 为 None 时表示无有效数据。
    """
    scores_path = sample_dir / "scores.json"
    ss_path = sample_dir / "sample_scores.json"

    scores_raw = _load_json_safe(scores_path)
    if not isinstance(scores_raw, dict):
        scores_raw = None

    if not ss_path.exists():
        # 无 sample_scores.json：完全保持既有行为
        return scores_raw, ("scores" if scores_raw is not None else "")

    ss_raw = _load_json_safe(ss_path)
    if not isinstance(ss_raw, dict):
        return scores_raw, ("scores" if scores_raw is not None else "")

    selected, source = select_fresh_scores(
        scores_raw, ss_raw,
        scores_freshness=_freshness(scores_path, scores_raw),
        sample_freshness=_freshness(ss_path, ss_raw),
        allow_sample_scores_fallback=allow_sample_scores_fallback,
    )
    if source == "sample_scores" and scores_raw is not None:
        logger.info(
            "样本评分新鲜度仲裁：%s 的 sample_scores.json 比 scores.json 新，采用前者",
            sample_dir.name,
        )
    return selected, source


def select_fresh_scores(
    scores_raw: dict | None,
    ss_raw: dict | None,
    *,
    scores_freshness: float,
    sample_freshness: float,
    allow_sample_scores_fallback: bool = True,
) -> tuple[dict | None, str]:
    """纯来源仲裁；读取与时间获取由调用方按各自容错策略完成。"""
    if not isinstance(ss_raw, dict):
        return scores_raw, ("scores" if scores_raw is not None else "")
    converted = _convert_sample_scores_to_scores(ss_raw)

    if scores_raw is None:
        # scores.json 缺失/损坏：仅在调用方允许时保留既有回退路径
        if allow_sample_scores_fallback and converted is not None:
            return converted, "sample_scores"
        return scores_raw, ("scores" if scores_raw is not None else "")

    if converted is None:
        return scores_raw, "scores"

    # 两者都存在且均可用 → 仅当 sample_scores 严格更新时采用；
    # 时间戳相等（同事件）或无法判定 → 一律选信息更完整的 scores.json
    if sample_freshness > scores_freshness:
        return converted, "sample_scores"
    return scores_raw, "scores"


def _convert_sample_scores_to_scores(sample_scores_raw: dict) -> dict | None:
    """将 sample_scores.json 格式转换为 scores.json 兼容格式，以便复用现有的报告构建逻辑。"""
    platforms_raw = sample_scores_raw.get("platforms")
    if not platforms_raw or not isinstance(platforms_raw, dict):
        return None

    converted_platforms = {}
    for plat_name, plat_data in platforms_raw.items():
        if not isinstance(plat_data, dict):
            continue
        scores = plat_data.get("scores", {})
        if not isinstance(scores, dict):
            scores = {}

        # 映射 sample_scores 的 scores 子结构到 scores.json 的扁平格式
        converted_platforms[plat_name] = {
            "success_rate_score": scores.get("success_rate"),
            "quality_score": scores.get("quality"),
            "experience_score": scores.get("experience"),
            "generation_success": scores.get("generation_success"),
            "pass_count": scores.get("pass_count"),
            "total_count": scores.get("total_count"),
            "pass_rate": scores.get("pass_rate"),
        }

    if not converted_platforms:
        return None

    return {
        "sample_id": sample_scores_raw.get("sample_id", ""),
        "platforms": converted_platforms,
    }


class ReportAggregator:
    """报告聚合器 - 从工作区样本数据动态聚合生成报告。

    用法::

        aggregator = ReportAggregator(workspace_dir)
        report_data = aggregator.aggregate()
        if report_data is None:
            # 回退到读取 report_data.json
            ...
    """

    def __init__(self, workspace_dir: Path) -> None:
        """初始化聚合器。

        Args:
            workspace_dir: 工作区绝对路径
        """
        self.workspace_dir = Path(workspace_dir)

    def aggregate(self) -> dict | None:
        """扫描所有样本数据文件，返回完整的报告数据结构。

        Returns:
            与 report_data 兼容的字典；如果工作区无有效样本数据则返回 None。
        """
        workspace = self.workspace_dir
        if not workspace.is_dir():
            logger.warning("工作区目录不存在: %s", workspace)
            return None

        # === 1. 扫描样本目录 ===
        sample_dirs = _scan_sample_dirs(workspace)
        if not sample_dirs:
            logger.info("工作区中未找到有效样本目录: %s", workspace)
            return None

        # === 2. 读取工作区元数据 ===
        meta_info = _read_workspace_meta(workspace)

        # === 3. 逐样本读取 scores.json 并聚合 ===
        sample_results: list[dict] = []
        excluded_samples: list[dict] = []
        # 按平台收集各维度分数列表
        platform_scores: dict[str, _PlatformScoreCollector] = {}
        # 全局各维度分数收集器
        global_collector = _ScoreCollector()
        # 去重计数（同一 sample_id 只计一次）
        unique_sample_ids: set[str] = set()

        for sample_id, sample_dir in sorted(sample_dirs.items()):
            # 读取 scores.json；与 sample_scores.json 同时存在时按新鲜度取较新者，
            # 避免评测中断后遗留的旧 scores.json 掩盖新鲜的逐样本实时结果（假 0 分）。
            # allow_sample_scores_fallback=False：保持“无 scores.json 则跳过该样本”
            # 的原有语义，不改变历史批次（如只有 sample_scores.json 的 miniprogram）
            # 的聚合范围。
            scores_raw, _scores_source = _resolve_fresh_scores(
                sample_dir, allow_sample_scores_fallback=False,
            )
            if scores_raw is None or not isinstance(scores_raw, dict):
                logger.warning("样本 '%s' 的 scores.json 无效，跳过", sample_id)
                continue

            platforms_data = scores_raw.get("platforms", {})
            if not isinstance(platforms_data, dict) or not platforms_data:
                logger.warning("样本 '%s' 的 scores.json 缺少 platforms 数据", sample_id)
                continue

            # 读取 generation.json（补充 token/duration）
            gen_data = _load_json_safe(sample_dir / "generation.json")
            # 读取 sample_report.json（兼容旧格式）
            sr_data = _load_json_safe(sample_dir / "sample_report.json")
            # 读取 evaluation.json（补充稳定性指标和测试用例）
            eval_data = _load_json_safe(sample_dir / "evaluation.json")

            # 判断是否应排除该样本
            generation_success = _check_generation_success(scores_raw, gen_data)
            is_deliverable = _check_deliverable(scores_raw, gen_data)

            if not generation_success or not is_deliverable:
                # 仅当样本没有有效平台数据（不会产出 sample_results 条目）时，
                # 才加入 excluded_samples；否则样本会通过 sample_results 以 0 分
                # 参与均值计算和前端渲染，不需要在 excluded_samples 中重复出现。
                has_valid_platform_entries = any(
                    isinstance(v, dict) for v in platforms_data.values()
                )
                if not has_valid_platform_entries:
                    excluded_entry = _build_excluded_entry(
                        sample_id, scores_raw, gen_data, sr_data,
                        generation_success=generation_success,
                        is_deliverable=is_deliverable,
                    )
                    excluded_samples.append(excluded_entry)

            unique_sample_ids.add(sample_id)

            # 展开为扁平化的样本-平台条目
            for plat_name, plat_scores in platforms_data.items():
                if not isinstance(plat_scores, dict):
                    continue

                flat_entry = _build_sample_entry(
                    sample_id, plat_name, plat_scores,
                    gen_data=gen_data, sr_data=sr_data, eval_data=eval_data,
                    workspace_dir=workspace,
                )
                sample_results.append(flat_entry)

                # 收集各维度分数
                if plat_name not in platform_scores:
                    platform_scores[plat_name] = _PlatformScoreCollector()
                platform_scores[plat_name].collect(plat_scores, flat_entry)
                global_collector.collect(plat_scores, flat_entry)

        if not sample_results and not excluded_samples:
            logger.warning("工作区中无有效评分数据: %s", workspace)
            return None

        # === 4. 计算聚合指标 ===
        sample_count = len(unique_sample_ids)
        global_means = global_collector.compute_means()
        per_platform_means: dict[str, dict] = {}
        for plat_name, collector in platform_scores.items():
            per_platform_means[plat_name] = collector.compute_means()
            per_platform_means[plat_name]["sample_count"] = collector.sample_count

        # === 5. 构建 meta ===
        platforms_list = meta_info.get("platforms", [])
        platform_str = ",".join(platforms_list) if platforms_list else (
            ",".join(sorted(platform_scores.keys())) if platform_scores else "unknown"
        )
        run_id = f"agg_{hashlib.md5(str(workspace).encode()).hexdigest()[:12]}"

        meta = {
            "run_id": run_id,
            "eval_version": meta_info.get("eval_version", ""),
            "dataset_version": meta_info.get("dataset", ""),
            "generator": meta_info.get("generator", ""),
            "generator_branch": meta_info.get("generator_branch", ""),
            "platform": platform_str,
            "sample_count": sample_count,
            "workspace_name": meta_info.get("workspace_name", workspace.name),
            "start_time": meta_info.get("created_at", ""),
            "end_time": "",
            "aggregated_from_raw": True,
        }

        # === 6. 构建输出结构 ===
        result = {
            "meta": meta,
            "summary": {
                "total_prompts": sample_count,
            },
            "top_level_summary": {
                "sample_count": sample_count,
                "mean_success_rate": global_means.get("mean_success_rate", 0),
                "mean_quality": global_means.get("mean_quality", 0),
                "mean_experience": global_means.get("mean_experience", 0),
                "mean_stability_score": global_means.get("mean_stability_score", 0),
                "mean_duration_ms": global_means.get("mean_duration_ms", 0),
                "mean_token_total": global_means.get("mean_token_total", 0),
                "mean_cost_usd": global_means.get("mean_cost_usd"),
                "mean_aesthetics_score": global_means.get("mean_aesthetics_score"),
                "e2e_pass": global_means.get("e2e_pass", 0),
                "e2e_count": global_means.get("e2e_count", 0),
                "e2e_pass_rate": global_means.get("e2e_pass_rate"),
                "per_platform": per_platform_means,
            },
            "sample_results": sample_results,
            "excluded_samples": excluded_samples,
        }

        # === 7. 跨平台一致性对比 ===
        from .cross_platform import enrich_report_data_with_cross_platform
        enrich_report_data_with_cross_platform(result, workspace)

        logger.info(
            "聚合完成: %d 样本, %d 条目, success_rate=%.1f, quality=%.1f, "
            "experience=%.1f, stability=%.1f",
            sample_count, len(sample_results),
            global_means.get("mean_success_rate", 0),
            global_means.get("mean_quality", 0),
            global_means.get("mean_experience", 0),
            global_means.get("mean_stability_score", 0),
        )

        return result

    def get_sample_report(self, sample_id: str) -> dict | None:
        """获取单个样本的报告数据。

        优先读取 scores.json（与 sample_scores.json 同时存在时按新鲜度取较新者），
        然后从 generation.json 和 sample_report.json 补充数据。

        Args:
            sample_id: 样本 ID

        Returns:
            样本报告字典，如果样本不存在或数据损坏则返回 None
        """
        sample_dir = self.workspace_dir / sample_id
        if not sample_dir.is_dir():
            return None

        # 新鲜度仲裁 + 既有回退链：scores.json → sample_scores.json 转换
        scores_raw, _scores_source = _resolve_fresh_scores(sample_dir)

        if scores_raw is None or not isinstance(scores_raw, dict):
            # 最终回退到 sample_report.json
            sr_data = _load_json_safe(sample_dir / "sample_report.json")
            if sr_data is not None and isinstance(sr_data, dict):
                return sr_data
            return None

        platforms_data = scores_raw.get("platforms", {})
        if not isinstance(platforms_data, dict):
            return None

        gen_data = _load_json_safe(sample_dir / "generation.json")
        sr_data = _load_json_safe(sample_dir / "sample_report.json")
        eval_data = _load_json_safe(sample_dir / "evaluation.json")

        results: list[dict] = []
        for plat_name, plat_scores in platforms_data.items():
            if not isinstance(plat_scores, dict):
                continue
            entry = _build_sample_entry(
                sample_id, plat_name, plat_scores,
                gen_data=gen_data, sr_data=sr_data, eval_data=eval_data,
                workspace_dir=self.workspace_dir,
            )
            results.append(entry)

        return {
            "sample_id": sample_id,
            "platforms": results,
        }


    def _convert_sample_scores_to_scores(self, sample_scores_raw: dict) -> dict | None:
        """将 sample_scores.json 转换为 scores.json 兼容格式（委派给模块级实现）。

        保留为实例方法以兼容既有调用方；实现已上提为模块级函数，
        供 :func:`_resolve_fresh_scores` 复用。
        """
        return _convert_sample_scores_to_scores(sample_scores_raw)


class _ScoreCollector:
    """收集各维度的分数值，用于计算均值。"""

    def __init__(self) -> None:
        self.success_rate: list[float] = []
        self.quality: list[float] = []
        self.experience: list[float] = []
        self.stability_score: list[float] = []
        self.duration_ms: list[float] = []
        self.token_total: list[float] = []
        self.token_input: list[float] = []
        self.token_output: list[float] = []
        self.cost_usd: list[float] = []
        self.aesthetics_score: list[float] = []
        self.usecase_completeness: list[float] = []
        # 后端完整度（0-100 口径，仅 requires_backend 且评分非空的样本参与）
        self.backend_completeness: list[float] = []
        # E2E 用例通过/总数（功能完整度 e2e_pass_rate 的分子/分母）
        self.e2e_pass: int = 0
        self.e2e_count: int = 0

    def collect(self, plat_scores: dict, flat_entry: dict) -> None:
        """从平台评分数据中收集各维度分数。

        注意：quality_score、stability_score 从 flat_entry 读取而非 plat_scores，
        因为 flat_entry 经过启动失败归零等后处理，是最终权威值。
        """
        _append_valid(self.success_rate, flat_entry, "success_rate_score")
        _append_valid(self.quality, flat_entry, "quality_score")
        _append_valid(self.experience, flat_entry, "experience_score")
        _append_valid(self.stability_score, flat_entry, "stability_score")
        # duration_ms 和 token_total 来自 flat_entry（已从 generation.json 补全）
        _append_valid(self.duration_ms, flat_entry, "duration_ms")
        _append_valid(self.token_total, flat_entry, "token_total")
        _append_valid(self.token_input, flat_entry, "token_input")
        _append_valid(self.token_output, flat_entry, "token_output")
        # cost_usd: 过滤 null 和 0（数据仅有 cost_cny 人民币时按汇率折算为美元）
        cost = extract_cost_usd(flat_entry)
        if cost is not None and cost > 0:
            self.cost_usd.append(cost)
        # aesthetics_score: 过滤 null（但保留 0，因为 0 是有效评分）
        aes = flat_entry.get("aesthetics_score")
        if aes is not None and isinstance(aes, (int, float)):
            self.aesthetics_score.append(float(aes))
        # usecase_completeness: 从 flat_entry 的 functionality_score 字段收集
        # （该字段由 _build_sample_entry 从 prompt_result.quality.usecase_completeness 设置）
        _append_valid(self.usecase_completeness, flat_entry, "functionality_score")
        # backend_completeness: 从样本原始评分 plat_scores 收集（scores.json 的
        # platforms[plat].backend_completeness，0-100 口径；requires_backend=false
        # 的样本该值为 null，自然不参与均值，与旧管道 ExecutionSummary 口径一致）
        _append_valid(self.backend_completeness, plat_scores, "backend_completeness")
        # e2e 用例计数：e2e_test_cases 已由各 backfill 归一化 passed 字段（含 manual_override）
        test_cases = flat_entry.get("e2e_test_cases")
        if isinstance(test_cases, list):
            self.e2e_count += len(test_cases)
            self.e2e_pass += sum(
                1 for tc in test_cases if isinstance(tc, dict) and tc.get("passed")
            )

    def compute_means(self) -> dict:
        """计算各维度的均值。"""
        result: dict = {}
        result["mean_success_rate"] = _safe_mean(self.success_rate)
        result["mean_quality"] = _safe_mean(self.quality)
        result["mean_experience"] = _safe_mean(self.experience)
        result["mean_stability_score"] = _safe_mean(self.stability_score)
        result["mean_duration_ms"] = _safe_mean(self.duration_ms, precision=1)
        result["mean_token_total"] = _safe_mean(self.token_total, precision=0)
        result["mean_token_input"] = _safe_mean(self.token_input, precision=0)
        result["mean_token_output"] = _safe_mean(self.token_output, precision=0)
        # cost_usd 和 aesthetics_score 允许为 None
        result["mean_cost_usd"] = _safe_mean_or_none(self.cost_usd, precision=6)
        result["mean_aesthetics_score"] = _safe_mean_or_none(self.aesthetics_score)
        result["mean_usecase_completeness"] = _safe_mean(self.usecase_completeness)
        # 后端完整度：无任何后端样本时为 None（前端展示 '-'），避免误导性的 0
        result["mean_backend_completeness"] = _safe_mean_or_none(self.backend_completeness)
        # 功能完整度：无 E2E 数据时为 None（前端展示 '-'），避免误导性的 0
        result["e2e_pass"] = self.e2e_pass
        result["e2e_count"] = self.e2e_count
        result["e2e_pass_rate"] = (
            round(self.e2e_pass / self.e2e_count * 100, 1) if self.e2e_count > 0 else None
        )
        return result


class _PlatformScoreCollector(_ScoreCollector):
    """按平台收集分数，额外跟踪 sample_count。"""

    def __init__(self) -> None:
        super().__init__()
        self.sample_count: int = 0

    def collect(self, plat_scores: dict, flat_entry: dict) -> None:
        super().collect(plat_scores, flat_entry)
        self.sample_count += 1


# ====================== 模块级函数式接口 ======================


def aggregate_report(workspace_dir: Path) -> dict | None:
    """主入口：聚合工作区报告。

    Args:
        workspace_dir: 工作区绝对路径

    Returns:
        与 report_data 兼容的字典；如果工作区无有效样本数据则返回 None
    """
    aggregator = ReportAggregator(workspace_dir)
    return aggregator.aggregate()


def load_single_sample_report(workspace_dir: Path, sample_id: str) -> dict | None:
    """加载单个样本报告。

    Args:
        workspace_dir: 工作区绝对路径
        sample_id: 样本 ID

    Returns:
        样本报告字典，如果不存在则返回 None
    """
    aggregator = ReportAggregator(workspace_dir)
    return aggregator.get_sample_report(sample_id)


# ====================== 内部辅助函数 ======================


def _scan_sample_dirs(workspace: Path) -> dict[str, Path]:
    """扫描工作区子目录，识别包含 scores.json 或 sample_report.json 的有效样本目录。

    Returns:
        {sample_id: sample_dir_path} 字典
    """
    sample_dirs: dict[str, Path] = {}
    excluded = _get_excluded_workspace_dirs()
    for entry in workspace.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name in excluded:
            continue
        # 识别包含 scores.json 的目录（优先）或 sample_report.json（兼容）
        if (entry / "scores.json").exists() or (entry / "sample_report.json").exists():
            sample_dirs[name] = entry
    return sample_dirs


def _read_workspace_meta(workspace: Path) -> dict:
    """读取工作区的 meta.json 元数据。

    如果 meta.json 不存在或读取失败，返回合理默认值。
    """
    meta_path = workspace / "meta.json"
    if not meta_path.exists():
        return _default_meta(workspace)

    meta_raw = _load_json_safe(meta_path)
    if meta_raw is None or not isinstance(meta_raw, dict):
        return _default_meta(workspace)

    return {
        "workspace_name": meta_raw.get("workspace_name", workspace.name),
        "generator": meta_raw.get("generator", ""),
        "generator_branch": meta_raw.get("generator_branch", ""),
        "platforms": meta_raw.get("platforms", []),
        "dataset": meta_raw.get("dataset", ""),
        "created_at": meta_raw.get("created_at", ""),
        "eval_version": meta_raw.get("eval_version", ""),
    }


def _default_meta(workspace: Path) -> dict:
    """返回默认的 meta 信息。"""
    return {
        "workspace_name": workspace.name,
        "generator": "",
        "generator_branch": "",
        "platforms": [],
        "dataset": "",
        "created_at": "",
        "eval_version": "",
    }


def _load_json_safe(path: Path) -> dict | list | None:
    """安全地加载 JSON 文件，损坏时记录 warning 并返回 None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("JSON 解析失败 %s: %s", path, exc)
        return None
    except OSError as exc:
        logger.debug("文件读取失败 %s: %s", path, exc)
        return None


def _append_valid(target: list[float], data: dict, key: str) -> None:
    """从 dict 中提取有效数值追加到列表，跳过 None 和非数值。"""
    value = data.get(key)
    if value is not None and isinstance(value, (int, float)):
        target.append(float(value))


def _safe_mean(values: list[float], *, precision: int = 1) -> float:
    """安全计算均值，空列表返回 0.0。"""
    if not values:
        return 0.0
    result = sum(values) / len(values)
    return round(result, precision)


def _safe_mean_or_none(values: list[float], *, precision: int = 1) -> float | None:
    """安全计算均值，空列表返回 None。"""
    if not values:
        return None
    result = sum(values) / len(values)
    return round(result, precision)


def _check_generation_success(
    scores_raw: dict,
    gen_data: dict | None,
) -> bool:
    """检查样本的生成是否成功。

    优先从 scores.json 的 success_rate_score 判断（>0 即为成功），
    其次从 generation.json 的 success 字段判断。
    """
    # 从各平台的 success_rate_score 判断：只要有一个平台 >0 就算成功
    platforms = scores_raw.get("platforms", {})
    if isinstance(platforms, dict):
        for _plat, plat_data in platforms.items():
            if isinstance(plat_data, dict):
                sr_score = plat_data.get("success_rate_score")
                if sr_score is not None and isinstance(sr_score, (int, float)) and sr_score > 0:
                    return True

    # 回退到 generation.json
    if gen_data and isinstance(gen_data, dict):
        success = gen_data.get("success")
        if success is not None:
            return bool(success)

    # 默认视为成功（旧格式可能没有这些字段）
    return True


def _check_deliverable(
    scores_raw: dict,
    gen_data: dict | None,
) -> bool:
    """检查样本是否可交付。

    判断标准：如果所有平台的 success_rate_score、quality_score 和 experience_score
    都为 0 或 None，则视为不可交付。只要有任一维度有有效分数即为可交付。
    """
    platforms = scores_raw.get("platforms", {})
    if not isinstance(platforms, dict):
        return True

    for _plat, plat_data in platforms.items():
        if not isinstance(plat_data, dict):
            continue
        sr_score = plat_data.get("success_rate_score", 0)
        q_score = plat_data.get("quality_score", 0)
        exp_score = plat_data.get("experience_score", 0)
        if (isinstance(sr_score, (int, float)) and sr_score > 0) or \
           (isinstance(q_score, (int, float)) and q_score > 0) or \
           (isinstance(exp_score, (int, float)) and exp_score > 0):
            return True

    return False


def _build_excluded_entry(
    sample_id: str,
    scores_raw: dict,
    gen_data: dict | None,
    sr_data: dict | None,
    *,
    generation_success: bool,
    is_deliverable: bool,
) -> dict:
    """构建排除样本条目。"""
    reasons: list[str] = []
    if not generation_success:
        reasons.append("生成失败")
    if not is_deliverable:
        reasons.append("不可交付")

    # 尝试从 generation.json 获取错误信息
    error_message = ""
    if gen_data and isinstance(gen_data, dict):
        error_message = gen_data.get("error_message", "") or gen_data.get("error", "")

    # 尝试获取平台信息
    platforms = list(scores_raw.get("platforms", {}).keys()) if isinstance(scores_raw.get("platforms"), dict) else []

    return {
        "sample_id": sample_id,
        "platform": ",".join(platforms),
        "reason": "；".join(reasons),
        "generation_success": generation_success,
        "is_deliverable": is_deliverable,
        "error_message": error_message,
    }


def _build_sample_entry(
    sample_id: str,
    plat_name: str,
    plat_scores: dict,
    *,
    gen_data: dict | None,
    sr_data: dict | None,
    eval_data: dict | None = None,
    workspace_dir: Path | None = None,
) -> dict:
    """构建单个样本-平台条目，从多个数据源合并字段。"""
    flat_entry: dict = {
        "sample_id": sample_id,
        "platform": plat_name,
    }
    # 先平铺 scores.json 的平台级字段
    flat_entry.update(plat_scores)

    # 从 generation.json 补全 token 和 duration
    _backfill_token_duration(flat_entry, plat_name, gen_data)

    # 从 execution_report_data.js 回填成本数据（generation.json 中无有效成本时）
    sample_dir = workspace_dir / sample_id if workspace_dir else None
    _backfill_cost_from_execution_report(flat_entry, plat_name, sample_dir)

    # 注入 execution_report_path（平台产物目录下的执行报告）
    if workspace_dir:
        exec_report_rel = f"{sample_id}/generated_projects/{plat_name}/execution_report.html"
        exec_report_path = workspace_dir / exec_report_rel
        if exec_report_path.exists():
            flat_entry["execution_report_path"] = exec_report_rel

    # 从 sample_report.json 补全缺失字段（兜底）
    _backfill_from_sample_report(flat_entry, plat_name, sr_data)

    # 从 evaluation.json 补全稳定性指标和测试用例
    _backfill_evaluation_data(flat_entry, plat_name, eval_data)

    # 从 sample_scores.json 补全 functionality_score 和 e2e_test_cases
    # sample_scores.json 包含 prompt_result 结构（scores.json 是扁平结构，没有 prompt_result）
    _backfill_from_sample_scores(flat_entry, sample_id, plat_name, workspace_dir)

    # functionality_score 兜底：如果所有数据源都没有设置，默认为 0
    if flat_entry.get("functionality_score") is None:
        flat_entry["functionality_score"] = 0

    # === 启动失败归零逻辑 ===
    # 当 success_rate_score = 0（启动失败）时，稳定性和功能完整性维度无意义，强制归零
    # 注意：experience_score 和 aesthetics_score 保留（截图评分仍有参考价值）
    sr_score = flat_entry.get("success_rate_score")
    if sr_score is not None and isinstance(sr_score, (int, float)) and sr_score == 0:
        flat_entry["stability_score"] = 0
        flat_entry["quality_score"] = 0
        flat_entry["functionality_score"] = 0
        if "compliance_score" in flat_entry:
            flat_entry["compliance_score"] = 0

    return flat_entry


def _backfill_evaluation_data(
    entry: dict,
    platform: str,
    eval_data: dict | None,
) -> None:
    """从 evaluation.json 补全 E2E 测试用例和稳定性指标。"""
    if not eval_data or not isinstance(eval_data, dict):
        return

    eval_platform = eval_data.get("platforms", {}).get(platform, {})
    if not eval_platform or not isinstance(eval_platform, dict):
        return

    # stability 指标
    stab = eval_platform.get("stability_metrics", {})
    if isinstance(stab, dict):
        entry.setdefault("stability_score", stab.get("stability_score", 0))
        entry.setdefault("crash_count", stab.get("crash_count", 0))
        entry.setdefault("anr_count", stab.get("anr_count", 0))
        entry.setdefault("white_screen_count", stab.get("white_screen_count", 0))

        # stability_detail（供前端 StabilityModal 使用）
        stab_detail = entry.get("stability_detail", {})
        if not isinstance(stab_detail, dict):
            stab_detail = {}
        stab_detail.setdefault("crash_events", stab.get("crash_events", []))
        stab_detail.setdefault("anr_events", stab.get("anr_events", []))
        stab_detail.setdefault("crash_rate", stab.get("crash_rate", 0.0))
        stab_detail.setdefault("anr_rate", stab.get("anr_rate", 0.0))
        stab_detail.setdefault("white_screen_count", stab.get("white_screen_count", 0))
        stab_detail.setdefault("white_screen_evidence", stab.get("white_screen_evidence", []))
        entry["stability_detail"] = stab_detail

        # 如果 white_screen_count > 0 且 stability_score 仍为 100.0，重新计算
        crash_count = entry.get("crash_count", 0)
        anr_count = entry.get("anr_count", 0)
        white_screen_count = entry.get("white_screen_count", 0)
        current_score = entry.get("stability_score", 0)
        test_results = eval_platform.get("test_results", [])
        total_test_runs = len(test_results) if test_results else 0

        if white_screen_count > 0 and current_score == 100.0 and total_test_runs > 0:
            issue_rate = (crash_count + anr_count + white_screen_count) / total_test_runs
            if issue_rate == 0.0:
                recalculated = 100.0
            elif issue_rate <= 0.05:
                recalculated = max(80.0, 100.0 - issue_rate * 400)
            elif issue_rate <= 0.15:
                recalculated = max(55.0, 90.0 - issue_rate * 300)
            elif issue_rate <= 0.30:
                recalculated = max(30.0, 70.0 - issue_rate * 200)
            else:
                recalculated = max(0.0, 40.0 - issue_rate * 100)
            entry["stability_score"] = round(recalculated, 1)

    # e2e_test_cases
    test_results = eval_platform.get("test_results", [])
    if test_results and isinstance(test_results, list):
        # 补全 status 字段，尊重 manual_override
        for tr in test_results:
            if isinstance(tr, dict):
                override = tr.get("manual_override")
                if override and isinstance(override, dict):
                    tr["status"] = override.get("new_status", tr.get("status", "FAIL"))
                    tr["passed"] = (tr["status"] == "PASS")
                elif "status" not in tr:
                    tr["status"] = "PASS" if tr.get("passed") else "FAIL"
        entry.setdefault("e2e_test_cases", test_results)
        # 计算 functionality_score (通过率)，尊重 manual_override
        if "functionality_score" not in entry:
            def _is_passed_eval(tr):
                if not isinstance(tr, dict):
                    return False
                override = tr.get("manual_override")
                if override and isinstance(override, dict):
                    return override.get("new_status") == "PASS"
                return tr.get("passed", False)

            passed = sum(1 for tr in test_results if _is_passed_eval(tr))
            total = len(test_results)
            entry["functionality_score"] = round(passed / total * 100, 1) if total > 0 else 0


def _backfill_token_duration(
    entry: dict,
    platform: str,
    gen_data: dict | None,
) -> None:
    """从 generation.json 补全 token 和 duration 数据。"""
    if not gen_data or not isinstance(gen_data, dict):
        return

    # Token 数据：优先从 platform_tokens 获取，回退到全局
    if not entry.get("token_total"):
        platform_tokens = gen_data.get("platform_tokens", {})
        if isinstance(platform_tokens, dict) and platform in platform_tokens:
            pt = platform_tokens[platform]
            if isinstance(pt, dict):
                entry.setdefault("token_input", pt.get("token_input", 0))
                entry.setdefault("token_output", pt.get("token_output", 0))
                entry.setdefault("token_total", pt.get("token_total", 0))
        else:
            entry.setdefault("token_input", gen_data.get("token_input", 0))
            entry.setdefault("token_output", gen_data.get("token_output", 0))
            entry.setdefault("token_total", gen_data.get("token_total", 0))

    # Duration 数据：优先从 platform_durations 获取
    if not entry.get("duration_ms"):
        platform_durations = gen_data.get("platform_durations", {})
        if isinstance(platform_durations, dict) and platform in platform_durations:
            dur = platform_durations[platform]
            if isinstance(dur, dict):
                entry["duration_ms"] = int(dur.get("duration_ms", 0) or 0)
            elif isinstance(dur, (int, float)):
                entry["duration_ms"] = int(dur)
        elif gen_data.get("duration_ms"):
            entry["duration_ms"] = int(gen_data["duration_ms"])

    # Cost 数据（美元口径；generation.json 仅有 cost_cny 人民币时折算）
    if not entry.get("cost_usd"):
        gen_cost = extract_cost_usd(gen_data)
        if gen_cost:
            entry.setdefault("cost_usd", gen_cost)


def _candidate_project_dirs(sample_dir: Path, platform: str) -> list[Path]:
    """候选项目目录：优先 generated_projects/{platform}，其次同级其它目录。

    生成器 expo 系平台的产物目录名为 'expo'，与评测平台名
    expo_web / expo_android / expo_ios 不一致，需要目录探测回退。
    """
    gp = sample_dir / "generated_projects"
    dirs: list[Path] = []
    if (gp / platform).is_dir():
        dirs.append(gp / platform)
    try:
        for child in sorted(gp.iterdir()):
            if child.is_dir() and child not in dirs:
                dirs.append(child)
    except OSError:
        pass
    return dirs


def _read_cost_from_summary_jsonl(summary_path: Path) -> float | None:
    """从 execution_summary.jsonl 首行读取总成本（master + sub_agents，美元）。"""
    try:
        first_line = summary_path.read_text(encoding="utf-8").splitlines()[0].strip()
        if not first_line:
            return None
        data = json.loads(first_line)
    except (json.JSONDecodeError, OSError, IndexError):
        return None
    total = 0.0
    cost = extract_cost_usd(data.get("master") or {})
    if cost is not None:
        total += cost
    for agent in data.get("sub_agents") or []:
        if isinstance(agent, dict):
            cost = extract_cost_usd(agent.get("metrics") or {})
            if cost is not None:
                total += cost
    return total if total > 0 else None


def read_sample_cost_usd(sample_dir: Path, platform: str) -> float | None:
    """从样本 trace 产物读取生成总成本（美元）。

    优先轻量的 execution_summary.jsonl，回退到 execution_report_data.js；
    新版生成器产物为 cost_cny（人民币），按汇率折算为美元。
    供 report_loader 与聚合器共用。
    """
    for proj_dir in _candidate_project_dirs(sample_dir, platform):
        for trace_dir in (proj_dir / "harness" / "trace", proj_dir):
            summary_path = trace_dir / "execution_summary.jsonl"
            if summary_path.exists():
                cost = _read_cost_from_summary_jsonl(summary_path)
                if cost is not None:
                    return round(cost, 6)
            report_path = trace_dir / "execution_report_data.js"
            if report_path.exists():
                cost = _read_cost_from_report_data_js(report_path)
                if cost is not None:
                    return round(cost, 6)
    return None


def _backfill_cost_from_execution_report(
    entry: dict,
    platform: str,
    sample_dir: Path | None,
) -> None:
    """从 trace 产物回填成本数据（美元口径）。

    当 generation.json 中无有效成本时，从执行 trace 产物
    （execution_summary.jsonl / execution_report_data.js）提取实际成本。

    总成本 = master.cost_usd + sum(sub_agents[].metrics.cost_usd)；
    新版生成器产物为 cost_cny（人民币），按汇率折算为美元。
    """
    # 如果已有有效成本，不覆盖
    existing_cost = extract_cost_usd(entry)
    if existing_cost is not None and existing_cost > 0:
        return

    if not sample_dir:
        return

    cost = read_sample_cost_usd(Path(sample_dir), platform)
    if cost is not None and cost > 0:
        entry["cost_usd"] = cost
        logger.debug(
            "从 trace 产物回填成本: %s/%s, cost=%.6f",
            sample_dir, platform, cost,
        )


def _read_cost_from_report_data_js(report_path: Path) -> float | None:
    """从 execution_report_data.js 提取总成本（美元），失败返回 None。"""
    try:
        content = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("读取 execution_report_data.js 失败 %s: %s", report_path, exc)
        return None

    try:
        # 用正则定位所有 push 调用，取最后一个
        push_matches = list(
            re.finditer(r"window\.__REPORT_DATA__\.push\(", content)
        )
        if not push_matches:
            return None

        json_start = push_matches[-1].end()
        # 跳过参数前导空白后用 C 级 JSONDecoder.raw_decode 直接解码 push() 参数：
        # 它在 JSON 值结束处停止并忽略其后的 `)`/`;` 等尾随内容，等价于原括号匹配
        # + json.loads，但避免在 10~35MB 的 execution_report_data.js 上逐字符 Python
        # 循环（原单样本 ~0.85s，是全量扫描长尾主因）。
        n = len(content)
        while json_start < n and content[json_start] in " \t\r\n":
            json_start += 1
        data, _ = json.JSONDecoder().raw_decode(content, json_start)
        if not isinstance(data, dict):
            return None

        # master 和 sub_agents 可能在顶层或 summary 内
        master = data.get("master")
        if not isinstance(master, dict):
            summary = data.get("summary", {})
            master = summary.get("master", {}) if isinstance(summary, dict) else {}

        total_cost = 0.0
        if isinstance(master, dict):
            cost = extract_cost_usd(master)
            if cost is not None:
                total_cost += cost

        sub_agents = data.get("sub_agents")
        if not isinstance(sub_agents, list):
            summary = data.get("summary", {})
            sub_agents = (
                summary.get("sub_agents", []) if isinstance(summary, dict) else []
            )

        if isinstance(sub_agents, list):
            for agent in sub_agents:
                if isinstance(agent, dict):
                    metrics = agent.get("metrics", {})
                    if isinstance(metrics, dict):
                        cost = extract_cost_usd(metrics)
                        if cost is not None:
                            total_cost += cost

        return total_cost if total_cost > 0 else None
    except Exception as exc:
        logger.debug(
            "从 execution_report_data.js 提取成本数据失败 %s: %s",
            report_path, exc,
        )
        return None


def _backfill_from_sample_report(
    entry: dict,
    platform: str,
    sr_data: dict | None,
) -> None:
    """从 sample_report.json 补全缺失字段（兜底数据源）。"""
    if not sr_data or not isinstance(sr_data, dict):
        return

    # Duration 兜底
    if not entry.get("duration_ms"):
        dur = sr_data.get("duration_ms") or sr_data.get("generation_duration", 0)
        if dur:
            entry["duration_ms"] = int(dur)

    # Token 兜底
    if not entry.get("token_total"):
        entry.setdefault("token_input", sr_data.get("token_input", 0))
        entry.setdefault("token_output", sr_data.get("token_output", 0))
        entry.setdefault("token_total", sr_data.get("token_total", 0))

    # Package size
    if not entry.get("package_size_bytes"):
        platform_sizes = sr_data.get("platform_package_sizes", {})
        if isinstance(platform_sizes, dict) and platform in platform_sizes:
            size = platform_sizes[platform]
            if size and isinstance(size, (int, float)):
                entry["package_size_bytes"] = int(size)

    # E2E test cases（sample_report.json 存储了各平台的 e2e_test_cases）
    if "e2e_test_cases" not in entry:
        sr_platforms = sr_data.get("platforms", {})
        if isinstance(sr_platforms, dict):
            plat_section = sr_platforms.get(platform, {})
            if isinstance(plat_section, dict):
                e2e_cases = plat_section.get("e2e_test_cases", [])
                if e2e_cases and isinstance(e2e_cases, list):
                    # 补全 status 字段，尊重 manual_override
                    for tc in e2e_cases:
                        if isinstance(tc, dict):
                            override = tc.get("manual_override")
                            if override and isinstance(override, dict):
                                tc["status"] = override.get("new_status", tc.get("status", "FAIL"))
                                tc["passed"] = (tc["status"] == "PASS")
                            elif "status" not in tc:
                                tc["status"] = "PASS" if tc.get("passed") else "FAIL"
                    entry["e2e_test_cases"] = e2e_cases
                    # 同步计算 functionality_score，尊重 manual_override
                    if "functionality_score" not in entry:
                        def _is_passed_sr(tc):
                            if not isinstance(tc, dict):
                                return False
                            override = tc.get("manual_override")
                            if override and isinstance(override, dict):
                                return override.get("new_status") == "PASS"
                            return tc.get("passed", False)

                        passed = sum(1 for tc in e2e_cases if _is_passed_sr(tc))
                        total = len(e2e_cases)
                        entry["functionality_score"] = round(passed / total * 100, 1) if total > 0 else 0


def _backfill_from_sample_scores(
    entry: dict,
    sample_id: str,
    platform: str,
    workspace_dir: Path | None,
) -> None:
    """从 sample_scores.json 补全 functionality_score 和 e2e_test_cases。

    sample_scores.json 包含完整的 prompt_result 结构，而 scores.json 是扁平结构。
    数据路径：
      - usecase_completeness: platforms.{platform}.prompt_result.quality.usecase_completeness
      - e2e test_results: platforms.{platform}.prompt_result.result_data.e2e_result.test_results
    """
    if not workspace_dir:
        return

    ss_path = workspace_dir / sample_id / "sample_scores.json"
    ss_data = _load_json_safe(ss_path)
    if not ss_data or not isinstance(ss_data, dict):
        return

    plat_data = ss_data.get("platforms", {}).get(platform, {})
    if not isinstance(plat_data, dict):
        return

    prompt_result = plat_data.get("prompt_result", {})
    if not isinstance(prompt_result, dict):
        return

    # functionality_score: 从 prompt_result.quality.usecase_completeness 读取
    if entry.get("functionality_score") is None:
        quality_data = prompt_result.get("quality", {})
        if isinstance(quality_data, dict):
            fc_value = quality_data.get("usecase_completeness")
            if fc_value is not None and isinstance(fc_value, (int, float)):
                entry["functionality_score"] = float(fc_value)

    # e2e_test_cases: 从 prompt_result.result_data.e2e_result.test_results 读取
    if "e2e_test_cases" not in entry:
        result_data = prompt_result.get("result_data", {})
        if isinstance(result_data, dict):
            e2e_result = result_data.get("e2e_result", {})
            if isinstance(e2e_result, dict):
                test_results = e2e_result.get("test_results", [])
                if test_results and isinstance(test_results, list):
                    # 补全 status 字段，尊重 manual_override
                    for tr in test_results:
                        if isinstance(tr, dict):
                            override = tr.get("manual_override")
                            if override and isinstance(override, dict):
                                tr["status"] = override.get("new_status", tr.get("status", "FAIL"))
                                tr["passed"] = (tr["status"] == "PASS")
                            elif "status" not in tr:
                                tr["status"] = "PASS" if tr.get("passed") else "FAIL"
                    entry["e2e_test_cases"] = test_results
                    # 同步计算 functionality_score（如果仍未设置），尊重 manual_override
                    if entry.get("functionality_score") is None:
                        def _is_passed_ss(tr):
                            if not isinstance(tr, dict):
                                return False
                            override = tr.get("manual_override")
                            if override and isinstance(override, dict):
                                return override.get("new_status") == "PASS"
                            return tr.get("passed", False)

                        passed = sum(1 for tr in test_results if _is_passed_ss(tr))
                        total = len(test_results)
                        entry["functionality_score"] = round(passed / total * 100, 1) if total > 0 else 0
