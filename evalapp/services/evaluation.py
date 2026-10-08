"""评测数据持久化服务 - 从 commands/evaluate.py 抽取的业务逻辑。

负责将评测运行结果持久化到工作区文件系统：
- evaluation.json: 每个样本的评测详情（构建/安装/启动状态、测试结果）
- scores.json: 每个样本的各维度评分
- backend_trace_{platform}.json: 后端评测过程记录
- 美观度评分: 通过 services.aesthetics 统一入口调用
"""

from datetime import datetime, timezone
from pathlib import Path

from ..utils.logging import get_logger
from ..workspace._safe_io import atomic_write_json

logger = get_logger(__name__)


def _now_local_iso() -> str:
    """本地时区的秒级 ISO 时间戳。

    与 :func:`..workspace.sample_data.write_sample_scores` 写入的 ``updated_at``
    格式保持一致（naive 本地时间），使 scores.json 与 sample_scores.json 的
    内容时间戳可直接比较（聚合器新鲜度判定依赖此约定）。
    """
    return datetime.now().isoformat(timespec="seconds")


def is_static_resource(req: dict) -> bool:
    """判断是否为静态资源（与 executor.ts 中 isStaticResource 保持一致）"""
    url = req.get("url", "").lower()
    resource_type = (req.get("resourceType") or "").lower()

    static_resource_types = ["stylesheet", "image", "font", "media"]
    if resource_type in static_resource_types:
        return True

    static_extensions = [
        ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
        ".mp4", ".mp3", ".wav", ".ogg", ".flac",
        ".eot", ".map",
    ]
    try:
        from urllib.parse import urlparse
        pathname = urlparse(url).path.lower()
        if any(pathname.endswith(ext) for ext in static_extensions):
            return True
    except Exception:
        path_no_query = url.split("?")[0]
        if any(path_no_query.endswith(ext) for ext in static_extensions):
            return True

    cdn_patterns = [
        "cdn.", ".cdn.", "unpkg.com", "cdnjs.", "jsdelivr.net",
        "fonts.googleapis.com", "fonts.gstatic.com",
    ]
    if any(pattern in url for pattern in cdn_patterns):
        return True

    return False


def is_html_response(req: dict) -> bool:
    """判断响应体是否为 HTML 页面（非真实 API 响应）"""
    response_body = req.get("responseBody", "")
    if not response_body:
        return False
    trimmed = response_body.strip().lower()
    return trimmed.startswith('<!doctype') or trimmed.startswith('<html')


class EvaluationService:
    """评测结果持久化服务。

    封装评测完成后的所有数据写入操作，使 commands/evaluate.py 只关注
    参数解析、流程编排和输出展示。
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    def _build_platform_evaluation(self, pr) -> dict:
        """从单个 PromptResult 构建 evaluation.json 的平台级数据块。

        ``persist_evaluation_results``（批次收尾全量写）与 ``persist_sample``
        （逐样本实时写）共用此序列化逻辑，保证两条路径产出的 schema 一致。
        """
        platform_data = {
            "build_status": "skipped",
            "install_status": "skipped",
            "launch_status": "skipped",
            "stability_metrics": {},
            "test_results": [],
            "error_message": pr.error_message,
            "error_details": [e.model_dump(mode="json") for e in pr.error_details],
            "updated_at": _now_local_iso(),
        }

        if pr.result_data:
            rd = pr.result_data
            platform_data["build_status"] = getattr(rd, "build_status", "skipped") or "skipped"
            platform_data["install_status"] = getattr(rd, "install_status", "skipped") or "skipped"
            platform_data["launch_status"] = getattr(rd, "launch_status", "skipped") or "skipped"
            sm = getattr(rd, "stability_metrics", None)
            if sm:
                platform_data["stability_metrics"] = {
                    "crash_count": getattr(sm, "crash_count", 0),
                    "anr_count": getattr(sm, "anr_count", 0),
                    "crash_free": getattr(sm, "crash_free", True),
                    "stability_score": getattr(sm, "stability_score", 100.0),
                    "white_screen_count": getattr(sm, "white_screen_count", 0),
                    "white_screen_evidence": getattr(sm, "white_screen_evidence", []),
                }

        for tr in pr.test_results or []:
            tc_id = getattr(tr, "test_case_id", "")
            tc_screenshots = self._find_tc_screenshots(pr.sample_id, pr.platform, tc_id)
            # TestCaseResult 模型没有 description/failure_reason 字段，通用描述
            # 存于 details；序列化时映射过去（模型未来加字段则优先取模型字段）
            details = getattr(tr, "details", "")
            platform_data["test_results"].append({
                "test_case_id": tc_id,
                "passed": tr.passed,
                "status": tr.status,
                "duration": tr.duration,
                "error_details": [e.model_dump(mode="json") for e in tr.error_details],
                "description": getattr(tr, "description", "") or details,
                "failure_reason": getattr(tr, "failure_reason", "") or (details if not tr.passed else ""),
                "report_path": getattr(tr, "report_path", ""),
                "screenshots": tc_screenshots,
            })

        return platform_data

    def persist_evaluation_results(self, run) -> None:
        """将评测结果持久化到 evaluation.json（每个样本一个文件）。

        逐平台增量合并（``merge_evaluation_platform``）而非整文件覆盖：
        单平台评测（``--platform``）或分平台多进程时，整文件覆盖会抹除
        本次 run 未包含的其他平台数据，与 scores.json 的合并语义不一致。
        确需整体重置的调用方请直接使用 ``write_evaluation``。

        Args:
            run: EvalRun 对象，包含 prompt_results 列表
        """
        from ..workspace.sample_data import merge_evaluation_platform

        for pr in run.prompt_results:
            try:
                merge_evaluation_platform(
                    self.workspace, pr.sample_id, pr.platform,
                    self._build_platform_evaluation(pr),
                )
            except Exception as e:
                logger.warning(
                    "Failed to write evaluation.json for %s/%s: %s",
                    pr.sample_id, pr.platform, e,
                )
                continue
            logger.debug("Wrote evaluation.json for %s/%s", pr.sample_id, pr.platform)

    def _build_platform_scores(self, pr) -> tuple[dict, list[dict]]:
        """从单个 PromptResult 构建 scores.json 的平台级数据块。

        Returns:
            ``(scores_data, runtime_errors)`` 二元组；runtime_errors 仅 expo*
            平台且提取成功时非空（提取失败仅 warning，返回空列表）。

        ``persist_scores``（批次收尾全量写）与 ``persist_sample``（逐样本实时写）
        共用此序列化逻辑，保证两条路径产出的 schema 一致。
        """
        from ..evaluation.runner.runtime_errors import extract_runtime_errors

        # 从 test_results 中提取后端请求记录（排除静态资源和 HTML 响应）以及页面诊断信息
        backend_requests = []
        page_network_errors = []
        page_http_errors = []
        page_js_errors = []
        page_console_errors = []
        page_diag_summary = {
            "network_monitor_enabled": False,
            "total_requests": 0,
            "network_error_count": 0,
            "http_error_count": 0,
            "js_error_count": 0,
            "console_error_count": 0,
            "console_warn_count": 0,
        }
        if pr.test_results:
            for tr in pr.test_results:
                if not tr.verifications:
                    continue
                if pr.requires_backend and "real_backend" in tr.verifications:
                    rb = tr.verifications["real_backend"]
                    if isinstance(rb, dict) and rb.get("requests"):
                        for req in rb["requests"]:
                            if not is_static_resource(req) and not is_html_response(req):
                                backend_requests.append(req)
                page_diag = tr.verifications.get("page_diagnostics")
                if isinstance(page_diag, dict):
                    summary = page_diag.get("summary") or {}
                    for key in page_diag_summary:
                        value = summary.get(key, 0)
                        if key == "network_monitor_enabled":
                            page_diag_summary[key] = bool(page_diag_summary[key] or value)
                        elif isinstance(value, (int, float)):
                            page_diag_summary[key] += value
                    page_network_errors.extend(page_diag.get("network_errors") or [])
                    page_http_errors.extend(page_diag.get("http_errors") or [])
                    page_js_errors.extend(page_diag.get("js_errors") or [])
                    page_console_errors.extend(page_diag.get("console_errors") or [])

        page_diagnostics_summary = {
            **page_diag_summary,
            "pass": (
                page_diag_summary["network_error_count"] == 0
                and page_diag_summary["http_error_count"] == 0
                and page_diag_summary["js_error_count"] == 0
                and page_diag_summary["console_error_count"] == 0
            ),
        }

        scores_data = {
            "error_details": [e.model_dump(mode="json") for e in pr.error_details],
            "success_rate_score": pr.success_rate.composite_score if pr.success_rate else 0.0,
            "quality_score": pr.quality.composite_score if pr.quality else 0.0,
            "experience_score": pr.experience.composite_score if pr.experience else 0.0,
            "stability_score": pr.quality.stability_score if pr.quality else 0.0,
            "launch_screenshot": "",
            "requires_backend": pr.requires_backend,
            "backend_completeness": pr.quality.backend_completeness if pr.quality else None,
            "backend_completeness_reason": pr.quality.backend_completeness_reason if pr.quality else "",
            "backend_requests": backend_requests,
            "page_diagnostics_summary": page_diagnostics_summary,
            "page_network_errors": page_network_errors[:50],
            "page_http_errors": page_http_errors[:50],
            "page_js_errors": page_js_errors[:50],
            "page_console_errors": page_console_errors[:50],
        }

        # Expo 平台：提取运行时错误（整段 try/except 不阻断评分主流程）
        runtime_errors: list[dict] = []
        if pr.platform.startswith("expo"):
            try:
                runtime_errors = extract_runtime_errors(pr.test_results or []) or []
            except Exception as e:
                logger.warning(
                    "Failed to extract runtime errors for %s/%s: %s",
                    pr.sample_id, pr.platform, e,
                )

        return scores_data, runtime_errors

    def persist_scores(self, run) -> None:
        """将评分结果持久化到 scores.json（每个样本一个文件）。

        同时对 expo* 平台提取运行时错误并写入 runtime_errors.json。
        整段运行时错误处理由 try/except 包裹，失败时仅 warning 不阻断评分主流程。

        Args:
            run: EvalRun 对象
        """
        from ..workspace.sample_data import write_runtime_errors, write_scores

        now = _now_local_iso()
        scores_by_sample: dict[str, dict] = {}
        # sid -> {platform: errors}；仅 expo 平台
        runtime_errors_by_sample: dict[str, dict[str, list[dict]]] = {}
        for pr in run.prompt_results:
            sid = pr.sample_id
            if sid not in scores_by_sample:
                # updated_at：本次评测落盘时间，供聚合器判定 scores.json 与
                # sample_scores.json 的新鲜度（避免报告阶段重写后旧壳掊盖新数据）
                scores_by_sample[sid] = {
                    "sample_id": sid, "updated_at": now, "platforms": {},
                }

            scores_data, runtime_errors = self._build_platform_scores(pr)
            scores_by_sample[sid]["platforms"][pr.platform] = scores_data
            if runtime_errors:
                runtime_errors_by_sample.setdefault(sid, {})[pr.platform] = runtime_errors

        for sid, scores_entry in scores_by_sample.items():
            write_scores(self.workspace, sid, scores_entry)
            logger.debug("Wrote scores.json for %s", sid)

            # 紧随 scores.json 写入 runtime_errors.json（仅 expo 平台）
            plat_errors = runtime_errors_by_sample.get(sid, {})
            for platform, errors in plat_errors.items():
                try:
                    write_runtime_errors(self.workspace, sid, platform, errors)
                    logger.debug(
                        "Wrote runtime_errors.json for %s/%s", sid, platform,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to write runtime_errors.json for %s/%s: %s",
                        sid, platform, e,
                    )

    def persist_sample(self, pr, *, updated_at: str | None = None) -> None:
        """单个样本×平台评测完成后立即落盘 evaluation.json + scores.json。

        背景：``persist_all`` 只在整批评测正常返回后执行，进程被中断
        （kill / OOM / 超时）时不会运行，已完成样本的结果就停留在上一轮的
        旧文件里；后续报告阶段基于旧 evaluation.json 重写 scores.json，
        UI 便显示假 0 分。逐样本实时落盘后，中断也不丢已完成样本的数据。

        与 ``persist_all`` 共用同一套序列化逻辑（``_build_platform_evaluation`` /
        ``_build_platform_scores``），全量跑时 ``persist_all`` 幂等覆盖；写入均为
        平台级增量合并，同一样本的其他平台数据不会被抹除。
        美观度评分（VL 模型调用，耗时）仍由 ``persist_all`` 统一处理。
        写入失败仅记录 warning，不中断评测主流程。

        Args:
            pr: 单个 PromptResult 对象
            updated_at: 可选的秒级 ISO 时间戳，写入 scores.json 的
                ``updated_at``。同一完成事件同时写 sample_scores.json 时应
                传入同一时间戳，使两文件时间戳严格相等，避免跨秒边界时
                新鲜度仲裁非确定性地选中信息更少的降级转换结构；
                缺省取当前本地时间。
        """
        from ..workspace.sample_data import (
            merge_evaluation_platform,
            write_runtime_errors,
            write_scores,
        )

        sid = pr.sample_id
        platform = pr.platform

        try:
            merge_evaluation_platform(
                self.workspace, sid, platform,
                self._build_platform_evaluation(pr),
            )
            logger.debug("Immediately wrote evaluation.json for %s/%s", sid, platform)
        except Exception as e:
            logger.warning(
                "Failed to immediately write evaluation.json for %s/%s: %s",
                sid, platform, e,
            )

        runtime_errors: list[dict] = []
        try:
            scores_data, runtime_errors = self._build_platform_scores(pr)
            write_scores(self.workspace, sid, {
                "sample_id": sid,
                "updated_at": updated_at or _now_local_iso(),
                "platforms": {platform: scores_data},
            })
            logger.debug("Immediately wrote scores.json for %s/%s", sid, platform)
        except Exception as e:
            logger.warning(
                "Failed to immediately write scores.json for %s/%s: %s",
                sid, platform, e,
            )

        if runtime_errors:
            try:
                write_runtime_errors(self.workspace, sid, platform, runtime_errors)
                logger.debug(
                    "Immediately wrote runtime_errors.json for %s/%s", sid, platform,
                )
            except Exception as e:
                logger.warning(
                    "Failed to write runtime_errors.json for %s/%s: %s",
                    sid, platform, e,
                )

    def persist_backend_traces(self, run) -> None:
        """将后端评测过程记录持久化到 backend_trace_{platform}.json。

        Args:
            run: EvalRun 对象
        """
        for pr in run.prompt_results:
            if not pr.requires_backend:
                continue

            per_test_case = []
            for tr in pr.test_results or []:
                real_backend = None
                page_diagnostics = None
                if tr.verifications:
                    real_backend = tr.verifications.get("real_backend")
                    page_diagnostics = tr.verifications.get("page_diagnostics")
                per_test_case.append({
                    "name": tr.test_case_id,
                    "passed": tr.passed,
                    "real_backend": real_backend,
                    "page_diagnostics": page_diagnostics,
                })

            trace_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "platform": pr.platform,
                "requires_backend": True,
                "backend_completeness": pr.quality.backend_completeness if pr.quality else None,
                "backend_completeness_reason": pr.quality.backend_completeness_reason if pr.quality else "",
                "per_test_case": per_test_case,
            }

            trace_path = self.workspace / pr.sample_id / f"backend_trace_{pr.platform}.json"
            atomic_write_json(trace_path, trace_data)
            logger.debug("Wrote backend_trace_%s.json for %s", pr.platform, pr.sample_id)

    def run_aesthetics(self, run, config=None) -> None:
        """对评测结果中的所有样本并行执行美观度评分。

        评分核心是纯 Python VL 模型调用，样本间无共享状态；
        scores.json 写入由 write_scores 的 file_lock 保护，trace 文件按样本
        目录隔离，因此可安全并行（并发度与 reporting._write_scores_files_parallel
        保持一致：4 线程）。

        Args:
            run: EvalRun 对象
            config: EvalApp Config 对象，为 None 时由 score_and_persist 内部
                   通过 get_config() 获取全局单例
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from . import aesthetics as aesthetics_svc

        rules = aesthetics_svc.load_rules()
        if not rules:
            return

        def _score_one(pr):
            app_category = getattr(pr, "sample_top_category", "") or ""
            aesthetics_svc.score_and_persist(
                workspace=self.workspace,
                sample_id=pr.sample_id,
                platform=pr.platform,
                app_category=app_category,
                rules=rules,
                skip_if_exists=False,
                config=config,
            )

        prompt_results = list(run.prompt_results)
        if len(prompt_results) <= 1:
            for pr in prompt_results:
                _score_one(pr)
            return

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="aes") as pool:
            futures = {pool.submit(_score_one, pr): pr for pr in prompt_results}
            for future in as_completed(futures):
                pr = futures[future]
                try:
                    future.result()
                except Exception as e:  # noqa: BLE001 — 单样本评分失败不阻断其余样本
                    logger.warning(
                        "Aesthetics scoring failed for %s/%s: %s",
                        pr.sample_id, pr.platform, e,
                    )

    def finish_run(self, run_dir, run) -> None:
        """结束 run 记录。

        Args:
            run_dir: run 目录路径
            run: EvalRun 对象
        """
        if not run_dir:
            return

        from ..workspace.runs import finish_run

        finish_run(run_dir, exit_code=0, result_summary={
            "phase": "evaluate",
            "total_tasks": len(run.prompt_results),
            "succeeded": sum(1 for pr in run.prompt_results if pr.generation_success),
            "failed": sum(1 for pr in run.prompt_results if not pr.generation_success),
            "skipped": 0,
            "samples_affected": list(set(pr.sample_id for pr in run.prompt_results)),
        })

    def persist_all(self, run, run_dir=None, config=None) -> None:
        """一站式持久化：写入所有评测结果文件 + 美观度评分 + 结束run。

        这是 commands/evaluate.py 调用的主入口，替代原来分散在 do_evaluate() 中的
        大段 try-except 持久化逻辑。

        Args:
            run: EvalRun 对象
            run_dir: run 目录路径（可选）
            config: EvalApp Config 对象（可选，保留参数以兼容旧调用方）。
        """
        try:
            self.persist_evaluation_results(run)
        except Exception as e:
            logger.warning("Failed to write evaluation.json files: %s", e)

        try:
            self.persist_scores(run)
        except Exception as e:
            logger.warning("Failed to write scores.json files: %s", e)

        try:
            self.persist_backend_traces(run)
        except Exception as e:
            logger.warning("Failed to write backend_trace files: %s", e)

        try:
            self.run_aesthetics(run, config=config)
        except Exception as e:
            logger.warning("Failed to run aesthetics evaluation: %s", e)

        try:
            self.finish_run(run_dir, run)
        except Exception as e:
            logger.warning("Failed to finish run record: %s", e)

    def _find_tc_screenshots(self, sample_id: str, platform: str, tc_id: str) -> list[str]:
        """查找测试用例的截图文件，返回相对于样本目录的路径列表"""
        sample_dir = self.workspace / sample_id
        screenshots = []

        # 方式1：从 screenshots/ 目录查找已提取的文件
        screenshots_dir = sample_dir / "screenshots"
        if screenshots_dir.exists():
            pattern = f"{platform}_{tc_id}_step_*.png"
            for f in sorted(screenshots_dir.glob(pattern)):
                screenshots.append(f"screenshots/{f.name}")
            pattern_jpg = f"{platform}_{tc_id}_step_*.jpg"
            for f in sorted(screenshots_dir.glob(pattern_jpg)):
                screenshots.append(f"screenshots/{f.name}")

        # 方式2：从 e2e_reports 中查找原始截图文件
        if not screenshots:
            e2e_dir = sample_dir / "e2e_reports"
            if e2e_dir.exists():
                for tc_dir in e2e_dir.iterdir():
                    if tc_dir.is_dir() and tc_dir.name.startswith(f"{platform}_{tc_id}"):
                        for f in sorted(tc_dir.glob("screenshot_*.png")):
                            screenshots.append(f"e2e_reports/{tc_dir.name}/{f.name}")
                        break

        return screenshots

    # ===================== 评测主流程 =====================

    @staticmethod
    def collect_tasks_from_plan(
        plan_store,
        *,
        sample_id: str | None = None,
        sample_ids: str | None = None,
        platform: str | None = None,
        sample_count: int | None = None,
    ) -> list[dict]:
        """从执行计划按过滤条件构建 sample_platform_tasks 列表。

        Args:
            plan_store: ExecPlanStore 实例
            sample_id: 单样本过滤 (与 sample_ids 互斥时优先 sample_ids)
            sample_ids: 多样本过滤 (逗号分隔)
            platform: 平台过滤
            sample_count: 限制任务数

        Returns:
            sample_platform_tasks (list[dict])
        """
        tasks = plan_store.get_tasks()
        if sample_ids:
            target_ids = [s.strip() for s in sample_ids.split(",")]
            tasks = [t for t in tasks if t.get("sample_id") in target_ids]
        elif sample_id:
            tasks = [t for t in tasks if t.get("sample_id") == sample_id]
        if platform:
            tasks = [t for t in tasks if t.get("platform") == platform]
        if sample_count is not None:
            tasks = tasks[:sample_count]

        sample_platform_tasks: list[dict] = []
        for task in tasks:
            sample = plan_store.get_sample(task["sample_id"])
            if sample:
                task_entry = {
                    "sample": sample,
                    "platform": task["platform"],
                    "end_case": task.get("end_case"),
                    "priority": task.get("priority"),
                }
                if task.get("skip_generate"):
                    task_entry["skip_generate"] = True
                sample_platform_tasks.append(task_entry)
        return sample_platform_tasks

    @staticmethod
    def resolve_samples_dirs(
        plan_store=None,
        samples_dir: Path | None = None,
    ) -> list[Path]:
        """解析样本集目录列表：优先 samples_dir，再从 plan_store 补充。

        plan_store 内部已完成版本子目录解析（如 dataset/beverage -> dataset/V2/beverage），
        此处优先使用已解析的真实路径，回退到原始相对路径拼接。
        """
        from ..utils.paths import get_project_root
        project_root = get_project_root()
        samples_dirs: list[Path] = []
        if samples_dir:
            samples_dirs.append(Path(samples_dir))
        if plan_store is not None:
            # 优先使用已解析的真实路径（含版本子目录）
            resolved = getattr(plan_store, 'list_resolved_dataset_dirs', None)
            if resolved:
                for d in resolved():
                    if d not in samples_dirs:
                        samples_dirs.append(d)
            else:
                # 兜底：原始路径拼接
                for ds in plan_store.list_datasets() or []:
                    d = project_root / ds
                    if d not in samples_dirs:
                        samples_dirs.append(d)
        return samples_dirs

    @staticmethod
    def infer_generator_name(workspace, default: str = "unknown") -> str:
        """从工作区目录名或 meta.json 推断生成器名称。"""
        import json as _json
        workspace_path = Path(workspace)
        # 优先从 meta.json 读取
        meta_file = workspace_path / "meta.json"
        if meta_file.exists():
            try:
                meta = _json.loads(meta_file.read_text(encoding="utf-8"))
                gen = meta.get("generator", "")
                if gen:
                    return gen
            except Exception:
                pass
        # 回退：从目录名推断（使用配置的 default_generator）
        workspace_name = workspace_path.name
        try:
            from ..config import get_config
            cfg_gen = get_config().default_generator
            if cfg_gen and (workspace_name.startswith(cfg_gen + "_") or workspace_name == cfg_gen):
                return cfg_gen
        except Exception:
            pass
        # 其他已知生成器名
        for gen in ("claude",):
            if workspace_name.startswith(gen + "_") or workspace_name == gen:
                return gen
        return default

    @staticmethod
    def resolve_generator_adapter(workspace, generator_name: str) -> str:
        """解析实际用于实例化生成器的适配器名。

        第三方平台产物以来源名称作为 ``generator`` 持久化，供报告展示真实来源；
        真正的执行适配器按产物形态记录在 meta.json 的 ``generator_adapter``
        字段（例如链接或安装包适配器）。来源名称不在 ``GeneratorRegistry``
        中注册，直接
        实例化会抛 Unknown generator，故未注册时回落到适配器名。

        已注册的名称优先返回，保证 ``--generator`` 的显式覆盖语义不被
        meta.json 干扰；无适配器记录时保持原名，交由 ``get_generator``
        抛出带安装指引的原始错误。
        """
        from ..generators import is_generator_registered

        if is_generator_registered(generator_name):
            return generator_name
        try:
            from ..workspace.meta import load_meta
            meta = load_meta(Path(workspace)) or {}
        except Exception as e:
            logger.debug("读取 meta.json 解析生成器适配器失败: %s", e)
            return generator_name
        adapter = meta.get("generator_adapter") or ""
        if adapter and adapter != generator_name:
            logger.info(
                "生成器 %r 未注册，按 meta.json generator_adapter 回落为 %r",
                generator_name, adapter,
            )
            return adapter
        return generator_name

    def regenerate_retest_report(
        self,
        all_results,
        samples_dirs,
        generator_name: str,
        config,
        *,
        no_open_report: bool = False,
        console=None,
    ) -> None:
        """retest 完成后：删除旧报告并重新生成工作区报告。

        使用聚合器从所有样本的 sample_report.json 聚合生成完整报告，
        避免仅包含 retest 样本导致未重测样本数据丢失。
        """
        from ..evaluation.results.models import EvalRun
        from ..evaluation.results.store import ResultStore, _sanitise_name
        from .report_aggregator import aggregate_report
        from .reporting import ReportService

        def _say(msg: str) -> None:
            if console is not None:
                console.print(msg)

        old_report = self.workspace / "report.html"
        if old_report.exists():
            old_report.unlink()
            _say("[dim]已删除旧报告: report.html[/dim]")

        _say("\n[blue]重新生成工作区报告...[/blue]")
        try:
            # 1. 导出 E2E 报告（仅 retest 的样本）
            run_obj = EvalRun(
                run_id=f"retest_{self.workspace.name}",
                generator_name=generator_name,
                prompt_results=[result for _, _, result in all_results],
            )

            retest_export_pairs = set()
            for sid, plat, _result in all_results:
                item_token = _sanitise_name(sid, "item")
                platform_token = _sanitise_name(plat, "unknown")
                retest_export_pairs.add((item_token, platform_token))

            result_store = ResultStore(self.workspace)
            exported = result_store.export_e2e_reports(
                run_obj, self.workspace, only_pairs=retest_export_pairs,
            )
            if exported:
                _say(f"  E2E 报告: 已导出 {len(exported)} 份到各样本目录下")

            # 2. 使用聚合器获取完整报告数据（包含所有样本）
            report_data = aggregate_report(self.workspace)
            if report_data is None:
                _say("[yellow]聚合报告数据失败，无法生成报告[/yellow]")
                return

            # 3. 使用聚合数据重新生成报告
            svc = ReportService(self.workspace, config)
            html_path = svc.regenerate_workspace_report(
                no_open_report=no_open_report,
                console=console,
                report_data=report_data,
                write_report_data=False,
            )
            if html_path:
                _say(f"[green]报告已重新生成: {html_path}[/green]")
        except Exception as e:
            _say(f"[yellow]报告生成失败: {e}[/yellow]")

    @staticmethod
    def build_retest_pairs(
        workspace: Path,
        target_ids,
        plan_store=None,
    ):
        """从样本 ID 列表构建重跑的 (sample_id, platform) 任务对。

        优先用 plan_store 调度设置，否则从 workspace 目录结构推断。
        已对结果去重。
        """
        from ..workspace.paths import is_expo_platform, is_multi_expo_workspace

        retest_pairs: list[tuple[str, str]] = []
        if plan_store is not None:
            plan_tasks = plan_store.get_tasks()
            for t in plan_tasks:
                if t.get("sample_id") in target_ids:
                    retest_pairs.append((t["sample_id"], t["platform"]))
        else:
            multi_expo = is_multi_expo_workspace(workspace)
            for sid in target_ids:
                gen_dir = workspace / sid / "generated_projects"
                if gen_dir.exists() and gen_dir.is_dir():
                    for plat_dir in gen_dir.iterdir():
                        if not plat_dir.is_dir():
                            continue
                        # 多 Expo 模式下，expo/ 目录需要展开为 expo_ios + expo_android
                        if multi_expo and plat_dir.name == "expo":
                            # 从 meta.json 读取 expo 平台列表
                            from ..workspace.paths import load_workspace_platforms
                            for p in load_workspace_platforms(workspace):
                                if is_expo_platform(p):
                                    file_count = sum(
                                        1 for _ in plat_dir.rglob("*") if _.is_file()
                                    )
                                    if file_count > 0:
                                        retest_pairs.append((sid, p))
                            continue
                        file_count = sum(
                            1 for _ in plat_dir.rglob("*") if _.is_file()
                        )
                        if file_count > 0:
                            retest_pairs.append((sid, plat_dir.name))
        return list(dict.fromkeys(retest_pairs))

    def run_evaluation(
        self,
        samples_dirs,
        sample_platform_tasks,
        generator_name: str,
        workers: int,
        config,
        *,
        auto_uninstall: bool = True,
        show_browser: bool = False,
        manifest=None,
        console=None,
        wait_generate: bool = False,
    ):
        """核心评测逻辑——对已生成代码执行 E2E 测试。

        负责：
        - 检查样本在工作区中是否已有代码
        - 初始化评测组件 (Evaluator/TestExecutor/Generator/manifest)
        - 评测并发限制（仅 miniprogram 允许并发）
        - 调用 evaluator.evaluate_existing_samples
        - 一站式持久化所有评测结果

        Args:
            samples_dirs: 样本集目录列表 (list[Path])
            sample_platform_tasks: 任务列表 (list[dict]，含 sample/platform/end_case/priority)
            generator_name: 生成器名称
            workers: 并发线程数
            config: Config 对象
            auto_uninstall: 评测后是否自动卸载应用
            show_browser: 是否显示浏览器界面
            manifest: 可选 ExecutionManifest。为 None 时尝试 load_or_create。
            console: 可选 Rich Console，用于输出人读信息。
            wait_generate: 流水线模式。为 True 时不做前置代码存在性过滤，
                改为按样本轮询 manifest 的 generate 阶段状态门控：
                completed → 投入评测；failed/skipped → 跳过；超时 → 跳过。
                用于与 generate 进程并行执行，重叠生成长尾时间。

        Returns:
            (run, dataset_version) 元组；无可评测样本时返回 (None, None)。
        """
        from pathlib import Path
        from ..evaluation.execution_manifest import ExecutionManifest
        from ..generators import get_generator
        from ..evaluation.runner.evaluator import Evaluator
        from ..evaluation.runner.executor import TestExecutor
        from ..benchset.testcases.store import TestCaseStore

        def _say(msg: str) -> None:
            if console is not None:
                console.print(msg)

        # 提取唯一样本列表
        samples = []
        for task_item in sample_platform_tasks:
            samples.append(task_item["sample"])
        samples = list({s.sample_id: s for s in samples}.values())
        samples = sorted(samples, key=lambda item: item.sample_id)

        # 验证 workspace 中已有代码；流水线模式（wait_generate）下生成尚在
        # 进行中，跳过前置过滤，改由评测时的 manifest 门控按样本判定
        valid_tasks = []
        missing_tasks = []
        from ..workspace.paths import resolve_generated_project_dir
        if wait_generate:
            valid_tasks = list(sample_platform_tasks)
        else:
            for task_item in sample_platform_tasks:
                sample = task_item["sample"]
                plat = task_item["platform"]
                # 统一通过 resolve_generated_project_dir 获取路径（含 Expo 跨平台 fallback）
                project_dir = resolve_generated_project_dir(
                    self.workspace, sample.sample_id, plat
                )
                has_content = False
                if project_dir.exists() and project_dir.is_dir():
                    file_count = sum(1 for _ in project_dir.rglob("*") if _.is_file())
                    has_content = file_count > 0

                # 补充检查：三方链接直评/安装包平台无本地代码，但有标记文件或 generation.json 中有 h5_url
                if not has_content:
                    gen_json = self.workspace / sample.sample_id / "generation.json"
                    if gen_json.exists():
                        try:
                            import json
                            gen_data = json.loads(gen_json.read_text(encoding="utf-8"))
                            if gen_data.get("h5_url") or gen_data.get("generator") in ("tusi", "miaoda", "miaowu", "codeflying"):
                                has_content = True
                        except (json.JSONDecodeError, OSError):
                            pass
                # 再次兑底：检查 workspace_dir 中是否有标记文件
                if not has_content:
                    if (project_dir / ".h5_url").exists() or (project_dir / ".package_installed").exists():
                        has_content = True

                if has_content:
                    valid_tasks.append(task_item)
                else:
                    missing_tasks.append(f"{sample.sample_id}/{plat}")

        if missing_tasks:
            _say(f"[yellow]跳过 {len(missing_tasks)} 个缺失代码的样本:[/yellow]")
            for m in missing_tasks:
                _say(f"  - {m}")

        if not valid_tasks:
            _say("[red]没有可评测的样本代码[/red]")
            return None, None

        _say(f"[blue]开始评测 {len(valid_tasks)} 个样本-平台组合...[/blue]")

        # 创建 run 记录
        run_dir = None
        try:
            from ..workspace.runs import create_run
            run_dir = create_run(self.workspace, phase="evaluate")
        except Exception as e:
            logger.warning("Failed to create run record: %s", e)

        # 初始化评测组件
        tc_store = TestCaseStore(samples_dirs)
        # 实例化用适配器名（三方产物品牌名需回落），generator_name 保持品牌名用于报告展示
        generator = get_generator(
            self.resolve_generator_adapter(self.workspace, generator_name), config
        )

        # 加载或创建 execution manifest
        local_manifest = manifest
        if local_manifest is None:
            try:
                local_manifest = ExecutionManifest.load_or_create(
                    self.workspace, sample_platform_tasks,
                )
            except Exception as _me:
                logger.warning("Failed to initialize execution manifest: %s", _me)
                local_manifest = None

        evaluator = Evaluator(
            generator=generator,
            executor=TestExecutor(
                config=config, auto_uninstall=auto_uninstall,
                show_browser=show_browser,
            ),
            test_case_store=tc_store,
            workspace_path=self.workspace,
            manifest=local_manifest,
        )

        # 构建 (sample, platform, end_case, priority) 元组列表
        platform_task_tuples = []
        for task_item in valid_tasks:
            platform_task_tuples.append((
                task_item["sample"],
                task_item["platform"],
                task_item.get("end_case"),
                task_item.get("priority"),
            ))

        config.stream_output = True

        # 评测并发限制：
        # - miniprogram/expo_web/h5 走浏览器本地 serve（见 executor._run_platform_tests），
        #   不占真机/模拟器，可直接按 workers 并发
        # - android/ios/expo_android/expo_ios 由 DevicePool 接管，硬上限取
        #   install_app.max_devices（默认 5）
        # 注意：DevicePool 已对端口/UDID 做隔离，evaluator 内部还会再按实际可用
        # 设备数对 max_workers 进行截断，因此此处仅做配置层 cap，不再强制降为 1。
        task_platforms = {t[1] for t in platform_task_tuples}
        device_platforms = task_platforms - {"miniprogram", "expo_web", "h5"}
        effective_workers = workers
        if workers > 1 and device_platforms:
            try:
                max_devices = int(getattr(config.install_app, "max_devices", 5))
            except Exception:
                max_devices = 5
            max_devices = max(1, max_devices)
            if workers > max_devices:
                _say(
                    f"[yellow]评测并发 {workers} 超过设备池上限 {max_devices}（平台 "
                    f"{sorted(device_platforms)}），按 {max_devices} 执行[/yellow]"
                )
                effective_workers = max_devices

        # 重置目标样本的 evaluate 阶段状态，防止被 manifest 跳过
        if local_manifest is not None:
            from ..evaluation.execution_manifest import PHASE_PENDING
            for sample, platform, _end_case, _priority in platform_task_tuples:
                if local_manifest.is_phase_completed(sample.sample_id, platform, "evaluate"):
                    local_manifest.update_item(sample.sample_id, platform, "evaluate", PHASE_PENDING)

        # 流水线模式：构建基于 manifest 磁盘状态的生成门控（generate 进程
        # 在另一个进程写 manifest，每次轮询必须重新从磁盘加载）
        ready_check = None
        if wait_generate:
            ready_check = self._build_generate_gate()
            _say("[blue]流水线模式：按样本等待生成完成后立即投入评测[/blue]")

        run = evaluator.evaluate_existing_samples(
            samples=samples,
            platform_tasks=platform_task_tuples,
            max_workers=effective_workers,
            generator_name=generator_name,
            ready_check=ready_check,
        )
        run.sample_source = str(samples_dirs[0])

        # 一站式持久化所有评测结果（复用本 Service 的 persist_all）
        self.persist_all(run, run_dir=run_dir, config=config)

        dataset_version = Path(samples_dirs[0]).name
        return run, dataset_version

    # 流水线门控：等待单样本生成完成的最长时间（本次批次最慢样本实测 2h06m，
    # 留足余量；超时后该样本标记 skipped，不阻塞其余样本）
    WAIT_GENERATE_TIMEOUT = 4 * 3600

    def _build_generate_gate(self):
        """构建流水线门控函数：(sample_id, platform) -> (state, reason)。

        每次调用从磁盘重新加载 manifest（generate 由另一进程写入，本进程
        内存副本会过期；manifest 文件 ~百 KB 量级，15s 轮询开销可忽略）。

        状态判定：
        - generate=completed → ready
        - generate=failed/skipped → failed（跳过该样本）
        - 其余（pending/running）→ wait；超过总体截止时间 → failed
        - manifest 文件不存在（无 generate 进程在跑的误用场景）→ 退化为
          代码存在性判定，避免空等 4 小时；壳工程占位文件恒存在，故还需
          结合 harness 产物证据排除"生成完全失败仅剩空壳"的场景
        """
        import time as _time

        from ..evaluation.execution_manifest import (
            ExecutionManifest,
            PHASE_COMPLETED,
            PHASE_FAILED,
            PHASE_SKIPPED,
        )
        from ..workspace.paths import resolve_generated_project_dir
        
        # 注：内部版此处调用生成器专属的业务代码证据检查
        # （check_business_code_evidence）排除“仅剩空壳工程”场景；
        # 该检查属于生成仓能力，开源评测仓不内置，退化为纯代码存在性判定。
        deadline = _time.monotonic() + self.WAIT_GENERATE_TIMEOUT
        workspace = self.workspace
        
        def gate(sample_id: str, platform: str) -> tuple[str, str]:
            manifest = ExecutionManifest.load(workspace)
            if manifest is None:
                # 无 manifest：退化为代码存在性一次性判定
                project_dir = resolve_generated_project_dir(workspace, sample_id, platform)
                if project_dir.exists() and any(
                    p.is_file() for p in project_dir.rglob("*")
                ):
                    return "ready", ""
                return "failed", "无 manifest 且未发现已生成代码"
            status = manifest.get_phase_status(sample_id, platform, "generate")
            if status == PHASE_COMPLETED:
                return "ready", ""
            if status in (PHASE_FAILED, PHASE_SKIPPED):
                return "failed", f"生成阶段状态为 {status}，跳过评测"
            if _time.monotonic() > deadline:
                return "failed", "等待生成完成超时，跳过评测"
            return "wait", ""

        return gate

    # ===================== retest 增量合并辅助方法 =====================

    @staticmethod
    def _merge_test_results(
        existing_results: list[dict],
        new_results: list[dict],
    ) -> list[dict]:
        """按 test_case_id 增量合并：新结果替换同 ID 旧结果，其余保持不变"""
        existing_map = {r["test_case_id"]: r for r in existing_results}
        for nr in new_results:
            existing_map[nr["test_case_id"]] = nr
        # 保持原顺序
        original_ids = [r["test_case_id"] for r in existing_results]
        merged: list[dict] = []
        seen: set[str] = set()
        for tc_id in original_ids:
            if tc_id in existing_map and tc_id not in seen:
                merged.append(existing_map[tc_id])
                seen.add(tc_id)
        # 追加新增的（原列表中没有的）
        for nr in new_results:
            if nr["test_case_id"] not in seen:
                merged.append(nr)
        return merged

    def _build_test_results_from_prompt_result(self, pr) -> list[dict]:
        """从 PromptResult 构建 test_results 字典列表（与 persist_evaluation_results 逻辑一致）"""
        results = []
        for tr in pr.test_results or []:
            tc_id = getattr(tr, "test_case_id", "")
            tc_screenshots = self._find_tc_screenshots(pr.sample_id, pr.platform, tc_id)
            # 与 _build_platform_evaluation 保持一致：details 映射到
            # description（通用描述）与 failure_reason（仅失败时）
            details = getattr(tr, "details", "")
            results.append({
                "test_case_id": tc_id,
                "passed": tr.passed,
                "status": tr.status,
                "duration": tr.duration,
                "error_details": [e.model_dump(mode="json") for e in tr.error_details],
                "description": getattr(tr, "description", "") or details,
                "failure_reason": getattr(tr, "failure_reason", "") or (details if not tr.passed else ""),
                "report_path": getattr(tr, "report_path", ""),
                "screenshots": tc_screenshots,
            })
        return results

    def _persist_evaluation_incremental(self, all_results, test_case_ids: list[str]) -> None:
        """增量模式：读取现有 evaluation.json，仅替换指定 test_case_id 的结果，其余保留"""
        from ..workspace.sample_data import read_evaluation, write_evaluation

        for sid, plat, pr in all_results:
            existing_eval = read_evaluation(self.workspace, sid) or {"sample_id": sid, "platforms": {}}
            if "platforms" not in existing_eval:
                existing_eval["platforms"] = {}

            existing_platform = existing_eval["platforms"].get(plat, {
                "build_status": "skipped",
                "install_status": "skipped",
                "launch_status": "skipped",
                "stability_metrics": {},
                "test_results": [],
            })

            # 构建新结果
            new_test_results = self._build_test_results_from_prompt_result(pr)

            # 合并
            existing_test_results = existing_platform.get("test_results", [])
            merged = self._merge_test_results(existing_test_results, new_test_results)
            existing_platform["test_results"] = merged
            existing_platform["updated_at"] = _now_local_iso()
            existing_platform["error_message"] = pr.error_message
            # 保留未重测用例携带的错误，不将局部结果写成完整原生快照。
            errors = [e for r in merged for e in r.get("error_details", [])]
            retained_errors = [
                error for error in existing_platform.get("error_details", [])
                if error.get("stage") == "generation"
                or (error.get("stage") == "launch" and "TC_LAUNCH" not in test_case_ids)
            ]
            for error in [*retained_errors, *[e.model_dump(mode="json") for e in pr.error_details]]:
                if error not in errors:
                    errors.append(error)
            existing_platform["error_details"] = errors

            # 更新状态字段（取最新的）
            if pr.result_data:
                rd = pr.result_data
                existing_platform["build_status"] = getattr(rd, "build_status", "skipped") or "skipped"
                existing_platform["install_status"] = getattr(rd, "install_status", "skipped") or "skipped"
                existing_platform["launch_status"] = getattr(rd, "launch_status", "skipped") or "skipped"

            existing_eval["platforms"][plat] = existing_platform
            write_evaluation(self.workspace, sid, existing_eval)
            logger.info("Incremental merge evaluation.json for %s/%s (test_case_ids=%s)", sid, plat, test_case_ids)

    def _persist_evaluation_full(self, all_results) -> None:
        """全量模式：直接用新结果覆写对应平台的 evaluation.json"""
        from ..workspace.sample_data import read_evaluation, write_evaluation

        for sid, plat, pr in all_results:
            existing_eval = read_evaluation(self.workspace, sid) or {"sample_id": sid, "platforms": {}}
            if "platforms" not in existing_eval:
                existing_eval["platforms"] = {}

            platform_data = {
                "build_status": "skipped",
                "install_status": "skipped",
                "launch_status": "skipped",
                "stability_metrics": {},
                "test_results": self._build_test_results_from_prompt_result(pr),
                "error_message": pr.error_message,
                "error_details": [e.model_dump(mode="json") for e in pr.error_details],
                "updated_at": _now_local_iso(),
            }
            if pr.result_data:
                rd = pr.result_data
                platform_data["build_status"] = getattr(rd, "build_status", "skipped") or "skipped"
                platform_data["install_status"] = getattr(rd, "install_status", "skipped") or "skipped"
                platform_data["launch_status"] = getattr(rd, "launch_status", "skipped") or "skipped"
                sm = getattr(rd, "stability_metrics", None)
                if sm:
                    platform_data["stability_metrics"] = {
                        "crash_count": getattr(sm, "crash_count", 0),
                        "anr_count": getattr(sm, "anr_count", 0),
                        "crash_free": getattr(sm, "crash_free", True),
                        "stability_score": getattr(sm, "stability_score", 100.0),
                        "white_screen_count": getattr(sm, "white_screen_count", 0),
                        "white_screen_evidence": getattr(sm, "white_screen_evidence", []),
                    }

            existing_eval["platforms"][plat] = platform_data
            write_evaluation(self.workspace, sid, existing_eval)
            logger.info("Full overwrite evaluation.json for %s/%s", sid, plat)

    def retest_samples(
        self,
        retest_pairs,
        samples_dirs,
        generator_name: str,
        config,
        *,
        auto_uninstall: bool = True,
        show_browser: bool = False,
        console=None,
        test_case_ids: list[str] | None = None,
    ):
        """对已有工作区中的指定 (sample_id, platform) 重跑 E2E 测试并同步 sample_report.json。

        负责：
        - 验证项目目录存在且非空
        - 加载样本定义
        - 调用 Evaluator._evaluate_existing_item 重跑 E2E
        - 同步更新各样本的 sample_report.json中的 platform_durations / duration_ms

        Args:
            retest_pairs: list[tuple[str, str]] 待重跑的 (sample_id, platform) 列表
            samples_dirs: 样本集目录列表
            generator_name: 生成器名称
            config: Config 对象
            auto_uninstall: 评测后是否自动卸载应用
            show_browser: 是否显示浏览器界面
            console: 可选 Rich Console。

        Returns:
            list[tuple[str, str, PromptResult]]: 成功运行的结果列表（已同步到 sample_report.json）。
        """
        from ..generators import get_generator
        from ..evaluation.runner.evaluator import Evaluator
        from ..evaluation.runner.executor import TestExecutor
        from ..benchset.samples.store import SampleStore
        from ..benchset.testcases.store import TestCaseStore
        from ..utils.json_io import read_json as _read_json
        from ..utils.json_io import write_json as _write_json
        from ..workspace.paths import is_expo_platform, resolve_generated_project_dir

        def _say(msg: str) -> None:
            if console is not None:
                console.print(msg)

        all_results = []
        tc_store = TestCaseStore(samples_dirs)
        generator = get_generator(
            self.resolve_generator_adapter(self.workspace, generator_name), config
        )

        evaluator = Evaluator(
            generator,
            TestExecutor(
                config=config, auto_uninstall=auto_uninstall,
                show_browser=show_browser,
            ),
            tc_store,
            workspace_path=self.workspace,
        )

        config.stream_output = True

        for idx, (sid, plat) in enumerate(retest_pairs, 1):
            # expo_* 平台共享 generated_projects/expo/ 目录
            if is_expo_platform(plat):
                project_dir = resolve_generated_project_dir(self.workspace, sid, plat)
            else:
                project_dir = self.workspace / sid / "generated_projects" / plat
            if not project_dir.exists() or not project_dir.is_dir():
                _say(f"[yellow]跳过 {sid}/{plat}: 项目目录不存在[/yellow]")
                continue

            file_count = sum(1 for _ in project_dir.rglob("*") if _.is_file())
            if file_count == 0:
                _say(f"[yellow]跳过 {sid}/{plat}: 项目目录为空[/yellow]")
                continue

            sample = None
            for sd in samples_dirs:
                sample_store = SampleStore(sd)
                sample = sample_store.get(sid)
                if sample:
                    break

            if not sample:
                _say(f"[yellow]跳过 {sid}/{plat}: 样本定义不存在[/yellow]")
                continue

            prefix = f"[{idx}/{len(retest_pairs)}] " if len(retest_pairs) > 1 else ""
            _say(f"{prefix}[blue]重跑 {sid}/{plat}...[/blue]")

            result = evaluator._evaluate_existing_item(
                sample=sample,
                platform=plat,
                test_case_ids=test_case_ids,
            )

            status = "[green]✓[/green]" if result.generation_success else "[red]✗[/red]"
            _say(
                f"  {status} {sid}/{plat}: {result.pass_count}/{result.total_count} "
                f"({result.pass_rate:.0%})"
            )
            if result.success_rate:
                _say(f"    成功率: {result.success_rate.composite_score:.1f}")
            if result.quality:
                _say(f"    功能完整性: {result.quality.composite_score:.1f}")
            if result.experience:
                _say(f"    体验: {result.experience.composite_score:.1f}")

            all_results.append((sid, plat, result))

        if not all_results:
            return all_results

        # --- 增量合并 evaluation.json（test_case_ids 模式下只替换指定用例结果）---
        if test_case_ids:
            self._persist_evaluation_incremental(all_results, test_case_ids)
        else:
            self._persist_evaluation_full(all_results)

        # 同步更新各样本的 sample_report.json
        for sid, plat, result in all_results:
            sample_report_path = self.workspace / sid / "sample_report.json"
            sample_report_data: dict = {}
            if sample_report_path.exists():
                try:
                    sample_report_data = _read_json(sample_report_path) or {}
                except Exception:
                    sample_report_data = {}

            new_duration_sec = result.generation_duration
            if new_duration_sec > 0:
                new_duration_ms = round(new_duration_sec * 1000, 2)
                platform_durations = sample_report_data.get("platform_durations", {})
                if not isinstance(platform_durations, dict):
                    platform_durations = {}
                platform_durations[plat] = new_duration_ms
                sample_report_data["platform_durations"] = platform_durations

            # 重新计算 duration_ms（取所有已知成功平台的最大耗时）
            platform_durations = sample_report_data.get("platform_durations", {})
            success_durations = []
            for _sid2, _plat2, _res2 in all_results:
                if _sid2 == sid and _res2.generation_success:
                    dur = platform_durations.get(_plat2)
                    if isinstance(dur, (int, float)) and dur > 0:
                        success_durations.append(dur)
            sample_report_data["duration_ms"] = max(success_durations) if success_durations else 0

            sample_report_data["sample_id"] = sid
            sample_report_data["generator"] = generator_name

            sample_report_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(sample_report_path, sample_report_data, indent=2)
            _say(f"[dim]已更新 {sid}/sample_report.json[/dim]")

        # 合并各 retest 样本的分散数据到自包含 sample_report.json
        try:
            from ..workspace.sample_data import consolidate_sample_report
            for sid, _plat, _result in all_results:
                consolidate_sample_report(self.workspace, sid)
        except Exception as _consol_e:
            logger.warning("Failed to consolidate retest sample_report.json: %s", _consol_e)

        return all_results
