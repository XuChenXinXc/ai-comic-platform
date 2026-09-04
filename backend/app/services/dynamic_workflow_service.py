from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import Database, db
from app.dynamic_pipeline import materializer as dynamic_materializer
from app.dynamic_pipeline import normalizers, script_stage
from app.dynamic_pipeline.diagnostics import diagnostics_status
from app.dynamic_pipeline.materializer import MaterializationConflict, MaterializationError
from app.dynamic_pipeline.media_queue_service import DynamicMediaQueueService
from app.dynamic_pipeline.prompt_ir import (
    apply_human_override,
    calculate_prompt_bundle_hashes,
)
from app.dynamic_pipeline.prompt_stage import PromptStageOutput, prompt_stage_override_hash
from app.dynamic_pipeline.prompt_stage_service import DynamicPromptStageService
from app.dynamic_pipeline.provider_capabilities import normalize_capabilities
from app.dynamic_pipeline.reference_plan import build_reference_plan
from app.dynamic_pipeline.reference_resolver import adapt_reference_bindings
from app.dynamic_pipeline.schemas import (
    AssetMapping,
    CharacterBiosOutput,
    Diagnostic,
    StageConfirmRequest,
    StageDraftUpdate,
    StageGateResult,
    WorkflowRunCreate,
)
from app.dynamic_pipeline.stage_catalog import (
    STAGE_KEYS,
    get_next_stage_key,
    get_stage_definition,
    get_stage_ordinal,
)
from app.dynamic_pipeline.stage_gates import (
    GATE_VERSION,
    GateInputError,
    evaluate_stage_gate,
    server_bound_rhythm_content,
)
from app.dynamic_pipeline.state_machine import (
    RevisionConflict,
    StageBlocked,
    can_confirm_stage,
    can_transition_status,
    ensure_stage_prerequisites_confirmed,
    require_revision,
    transition_status,
)
from app.dynamic_pipeline.storyboard_stage import StoryboardOutput, validate_storyboard_output
from app.dynamic_pipeline.visual_anchor import anchor_version, asset_profile_to_anchor
from app.jobs.dynamic_bridge import DynamicJobBridge, DynamicJobBridgeError
from app.jobs.repository import JobRepository
from app.jobs.runner import job_runner
from app.models.tables import (
    api_provider,
    asset,
    chapter,
    job,
    novel_analysis,
    project,
    screenplay,
    shot,
    workflow_run,
    workflow_stage,
)
from app.services.dynamic_workflow_repository import (
    DynamicWorkflowNotFoundError,
    DynamicWorkflowRepository,
    DynamicWorkflowRepositoryError,
)
from app.services.errors import ServiceError, bad_request, conflict, not_found
from app.services.file_service import FileService, FileServiceError
from app.services.signature_service import calculate_signature, source_signature
from app.services.utils import (
    json_dumps,
    json_loads,
    new_id,
    row_dict,
    sanitize_error_message,
    utc_now,
)

_STAGE_JSON_FIELDS = (
    ("raw_output_json", "raw_output", {}),
    ("normalized_output_json", "normalized_output", {}),
    ("draft_output_json", "draft_output", {}),
    ("output_refs_json", "output_refs", []),
    ("gate_result_json", "gate_result", {}),
)
_RESUMABLE_RUN_STATUSES = {"blocked", "stale", "failed", "interrupted", "partial_failed"}
_RESUMABLE_STAGE_STATUSES = {
    "pending",
    "blocked",
    "stale",
    "failed",
    "interrupted",
    "partial_failed",
}
_CANCELLABLE_STAGE_STATUSES = {"queued", "running", "blocked", "ready", "stale"}
_PHASE2_STAGE_KEYS = frozenset({"0-source", "1-character-bios", "2-rhythm"})
_PHASE2_QUEUEABLE_STAGE_STATUSES = frozenset({"pending", "stale", "failed", "interrupted"})
_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "waiting_provider", "downloading"})
_PROMPT_STAGE_KEYS = ("5a-image-prompts", "5b-video-prompts")
_PROMPT_STAGE_COMPILER_VERSION = "dynamic-prompt-2"
_PHASE2_CONTENT_KEYS = {
    "0-source": frozenset(
        {
            "artifact_meta",
            "metadata",
            "episode_logline",
            "scenes",
            "character_roster",
            "conflict_turns",
            "visualizable_content",
            "hard_to_visualize",
            "rewrite_requirements",
            "assumptions",
            "open_questions",
            "continuation_outline",
            "content",
        }
    ),
    "1-character-bios": frozenset(
        {
            "artifact_meta",
            "characters",
            "ensemble_anchors",
            "unresolved_character_refs",
            "content",
        }
    ),
    "2-rhythm": frozenset(
        {
            "artifact_meta",
            "target_duration_sec",
            "timeline",
            "core_payoff",
            "small_beats",
            "strong_dynamic_budget",
            "foreshadowing",
            "open_space",
            "dialogue_suggestions",
            "sound_suggestions",
            "assumptions",
            "content",
        }
    ),
}


class _DynamicMaterializerAdapter:
    """将纯物化 builder 作为动态确认服务的默认依赖。"""

    @staticmethod
    def build_novel_analysis_projection(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dynamic_materializer.build_novel_analysis_projection(*args, **kwargs)

    @staticmethod
    def build_asset_materialization_plan(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return dynamic_materializer.build_asset_materialization_plan(*args, **kwargs)

    @staticmethod
    def build_screenplay_projection(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dynamic_materializer.build_screenplay_projection(*args, **kwargs)

    @staticmethod
    def build_shot_materialization_plan(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return dynamic_materializer.build_shot_materialization_plan(*args, **kwargs)

    @staticmethod
    def validate_storyboard_output(*args: Any, **kwargs: Any) -> list[Any]:
        return validate_storyboard_output(*args, **kwargs)


class DynamicWorkflowService:
    """动态工作流的运行与阶段协调服务。

    该服务只负责运行容器、阶段状态和快照，不在事务内调用 Provider、Runner
    或媒体服务。现有项目、章节和媒体服务继续由旧 API 使用。
    """

    def __init__(
        self,
        database: Database | None = None,
        storage: FileService | None = None,
        job_bridge: DynamicJobBridge | None = None,
        materializer: Any | None = None,
        media_queue_service: Any | None = None,
    ) -> None:
        self.database = database or db
        self.repository = DynamicWorkflowRepository(self.database)
        self.storage = storage or FileService(database=self.database)
        if job_bridge is None:
            bridge_repository = JobRepository(self.database)
            self.job_bridge = DynamicJobBridge(bridge_repository, enqueue=job_runner.enqueue)
        else:
            self.job_bridge = job_bridge
        self.materializer = materializer or _DynamicMaterializerAdapter()
        bridge_repository = getattr(self.job_bridge, "repository", None) or JobRepository(
            self.database
        )
        bridge_enqueue = getattr(self.job_bridge, "enqueue", None)
        self.media_queue_service = (
            media_queue_service
            if media_queue_service is not None
            else DynamicMediaQueueService(
                self.database,
                job_repository=bridge_repository,
                enqueue=bridge_enqueue,
            )
        )

    @staticmethod
    def _to_service_error(exc: Exception) -> ServiceError:
        if isinstance(exc, ServiceError):
            return exc
        if isinstance(exc, RevisionConflict):
            return conflict("运行内容已被其他操作修改，请刷新后重试")
        if isinstance(exc, StageBlocked):
            return conflict(str(exc))
        if isinstance(exc, DynamicJobBridgeError):
            return conflict(str(exc))
        if isinstance(exc, MaterializationConflict):
            return conflict(f"动态 canonical 物化冲突：{sanitize_error_message(exc)}")
        if isinstance(exc, MaterializationError):
            diagnostics = getattr(exc, "diagnostics", ())
            detail = sanitize_error_message(exc)
            blocking = next(
                (
                    sanitize_error_message(getattr(item, "message", ""))
                    for item in diagnostics
                    if getattr(item, "level", None) == "blocking"
                ),
                "",
            )
            return bad_request(f"动态 canonical 物化失败：{detail or blocking or '阶段产物无效'}")
        if isinstance(exc, DynamicWorkflowNotFoundError):
            return not_found("动态运行")
        if isinstance(exc, IntegrityError):
            # SQLite 错误信息形如 "UNIQUE constraint failed: workflow_stage.id"
            # 或 "FOREIGN KEY constraint failed"。区分 unique 冲突（409 建议重试）
            # 与外键/检查约束失败（422 是业务错误，重试无意义）。
            message = sanitize_error_message(exc).lower()
            if "unique" in message or "constraint failed" in message and "foreign" not in message:
                return conflict("动态工作流数据冲突，请刷新后重试")
            return bad_request("动态工作流数据完整性校验失败，请检查参数")
        if isinstance(exc, FileServiceError):
            return bad_request(str(exc))
        if isinstance(exc, DynamicWorkflowRepositoryError):
            detail = sanitize_error_message(exc)
            return bad_request(f"动态工作流操作失败：{detail or '数据库操作失败'}")
        if isinstance(exc, SQLAlchemyError):
            return bad_request("动态工作流数据库操作失败")
        if isinstance(exc, ValueError):
            return bad_request(str(exc))
        return bad_request("动态工作流操作失败，请稍后重试")

    @classmethod
    def _raise_service_error(cls, exc: Exception) -> None:
        raise cls._to_service_error(exc) from exc

    @staticmethod
    def _model_dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return DynamicWorkflowService._model_dump(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return {key: DynamicWorkflowService._model_dump(item) for key, item in value.items()}
        if isinstance(value, list):
            return [DynamicWorkflowService._model_dump(item) for item in value]
        if isinstance(value, tuple):
            return [DynamicWorkflowService._model_dump(item) for item in value]
        return value

    @staticmethod
    def _normalize_run(record: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result["source_snapshot"] = (
            dict(result["source_snapshot"])
            if isinstance(result.get("source_snapshot"), Mapping)
            else {}
        )
        result["config_snapshot"] = (
            dict(result["config_snapshot"])
            if isinstance(result.get("config_snapshot"), Mapping)
            else {}
        )
        result["asset_mapping"] = (
            list(result["asset_mapping"]) if isinstance(result.get("asset_mapping"), list) else []
        )
        result["current_stage"] = result.get("current_stage") or "0-source"
        return result

    @staticmethod
    def _normalize_stage(record: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(record)
        try:
            result["gate_result"] = StageGateResult.model_validate(
                result.get("gate_result") or {}
            ).model_dump(mode="json")
        except (TypeError, ValueError):
            result["gate_result"] = StageGateResult(status="blocked").model_dump(mode="json")
        for field, default in (("raw_output", {}), ("normalized_output", {}), ("draft_output", {})):
            if not isinstance(result.get(field), Mapping):
                result[field] = default
        if not isinstance(result.get("output_refs"), list):
            result["output_refs"] = []
        return result

    @staticmethod
    def _validate_stage_key(stage_key: str) -> None:
        try:
            get_stage_definition(stage_key)
        except ValueError as exc:
            raise bad_request(str(exc)) from exc

    @staticmethod
    def _revision_value(expected_revision: Any) -> int:
        expected_revision = getattr(expected_revision, "expected_revision", expected_revision)
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise bad_request("expected_revision 必须是正整数")
        return expected_revision

    @staticmethod
    def _run_from_connection(connection: Any, run_id: str) -> dict[str, Any] | None:
        return row_dict(
            connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
        )

    @staticmethod
    def _decode_stage_row(row: Any) -> dict[str, Any] | None:
        record = row_dict(row)
        if record is None:
            return None
        for database_name, public_name, default in _STAGE_JSON_FIELDS:
            value = json_loads(record.pop(database_name, None), default)
            expected_type = list if public_name == "output_refs" else dict
            record[public_name] = value if isinstance(value, expected_type) else default
        return record

    @classmethod
    def _stage_from_connection(
        cls,
        connection: Any,
        run_id: str,
        stage_key: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            select(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .order_by(
                workflow_stage.c.attempt.desc(),
                workflow_stage.c.updated_at.desc(),
                workflow_stage.c.id.desc(),
            )
            .limit(1)
        ).first()
        return cls._decode_stage_row(row)

    @classmethod
    def _stages_from_connection(cls, connection: Any, run_id: str) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            select(workflow_stage)
            .where(workflow_stage.c.workflow_run_id == run_id)
            .order_by(
                workflow_stage.c.ordinal,
                workflow_stage.c.stage_key,
                workflow_stage.c.attempt.desc(),
                workflow_stage.c.updated_at.desc(),
                workflow_stage.c.id.desc(),
            )
        ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = cls._decode_stage_row(row)
            if record is not None and record["stage_key"] not in latest:
                latest[record["stage_key"]] = record
        return latest

    @staticmethod
    def _confirmed_keys(stages: Mapping[str, Mapping[str, Any]]) -> list[str]:
        return [
            stage_key
            for stage_key in STAGE_KEYS
            if stages.get(stage_key, {}).get("status") == "confirmed"
        ]

    @staticmethod
    def _current_stage_for(stage_key: str) -> str:
        if stage_key in {"5a-image-prompts", "5b-video-prompts"}:
            return "5a-image-prompts"
        return stage_key

    @staticmethod
    def _next_current_stage(stage_key: str, confirmed_keys: set[str]) -> str:
        if stage_key in {"5a-image-prompts", "5b-video-prompts"}:
            if {"5a-image-prompts", "5b-video-prompts"}.issubset(confirmed_keys):
                return "6-delivery"
            return "5a-image-prompts"
        return get_next_stage_key(stage_key) or stage_key

    @classmethod
    def _delivery_stage_input_signature(
        cls,
        run: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """按根输入和两个已确认 Prompt 分支生成 6-delivery 输入签名。"""

        prompt_inputs = [
            {
                "stage_key": stage_key,
                "stage_id": stages.get(stage_key, {}).get("id", ""),
                "input_signature": stages.get(stage_key, {}).get("input_signature", ""),
                "confirmed_revision": stages.get(stage_key, {}).get("confirmed_revision"),
            }
            for stage_key in _PROMPT_STAGE_KEYS
        ]
        return calculate_signature(
            "6-delivery",
            run.get("input_signature") or "",
            prompt_inputs,
        )

    @staticmethod
    def _delivery_dependency_ids(
        connection: Any,
        run_id: str,
        prompt_stage_ids: Iterable[str],
        delivery_stage_id: str,
    ) -> list[str]:
        prompt_stage_ids = [str(value) for value in prompt_stage_ids if value]
        media_types = (
            "generate_keyframe",
            "generate_video",
            "generate_dialogue_audio",
            "generate_dynamic_subtitle",
        )
        conditions = [
            job.c.workflow_run_id == run_id,
            job.c.job_type.in_(media_types),
        ]
        stage_conditions = []
        if prompt_stage_ids:
            stage_conditions.append(job.c.workflow_stage_id.in_(prompt_stage_ids))
        if stage_conditions:
            conditions.append(or_(*stage_conditions))
        rows = connection.execute(
            select(job.c.id)
            .where(*conditions)
            .order_by(job.c.created_at, job.c.id)
        ).all()
        return [str(row.id) for row in rows if row.id]

    def _refresh_delivery_dependencies(self, run_id: str) -> None:
        """媒体 Job 事务提交后补齐 6-delivery 的真实 DAG 边。"""
        with self.database.engine.begin() as connection:
            run = self._run_from_connection(connection, run_id)
            if run is None:
                return
            stages = self._stages_from_connection(connection, run_id)
            delivery_stage = stages.get("6-delivery")
            if delivery_stage is None:
                return
            prompt_stage_ids = [
                str(stages[key]["id"])
                for key in _PROMPT_STAGE_KEYS
                if stages.get(key) and stages[key].get("id")
            ]
            dependency_ids = self._delivery_dependency_ids(
                connection,
                run_id,
                prompt_stage_ids,
                str(delivery_stage["id"]),
            )
            delivery_jobs = connection.execute(
                select(job).where(
                    job.c.workflow_run_id == run_id,
                    job.c.workflow_stage_id == delivery_stage["id"],
                    job.c.job_type == "dynamic_stage_generate",
                    job.c.input_signature == delivery_stage.get("input_signature", ""),
                    job.c.status.in_(tuple(_ACTIVE_JOB_STATUSES)),
                )
            ).all()
            for row in delivery_jobs:
                record = row_dict(row) or {}
                input_data = json_loads(record.get("input_json"), {})
                input_data = dict(input_data) if isinstance(input_data, Mapping) else {}
                input_data["depends_on"] = dependency_ids
                input_data["workflow_run_revision"] = run.get("revision")
                input_data["workflow_stage_input_signature"] = delivery_stage.get(
                    "input_signature", ""
                )
                connection.execute(
                    update(job)
                    .where(job.c.id == record["id"], job.c.status.in_(tuple(_ACTIVE_JOB_STATUSES)))
                    .values(
                        depends_on_json=json_dumps(dependency_ids),
                        input_json=json_dumps(input_data),
                        updated_at=utc_now(),
                    )
                )

    @staticmethod
    def _queue_status(current_status: str) -> str:
        """将可恢复状态按状态机规则转换为 queued。"""
        if current_status == "ready":
            transition_status(current_status, "stale")
            return transition_status("stale", "queued")
        return transition_status(current_status, "queued")

    @staticmethod
    def _phase2_content(stage: Mapping[str, Any]) -> Any:
        draft = stage.get("draft_output")
        if draft not in (None, {}):
            return draft
        normalized = stage.get("normalized_output")
        return normalized if normalized is not None else {}

    @staticmethod
    def _phase2_content_requires_recalculation(
        stage_key: str, content: Any, stage: Mapping[str, Any]
    ) -> bool:
        """只有精确的旧摘要格式走 legacy gate，其余内容都重新校验。"""

        del stage
        if not isinstance(content, Mapping) or not content:
            return True
        return not (
            set(content) == {"summary"}
            and isinstance(content.get("summary"), str)
        )

    @staticmethod
    def _blocked_gate_from_input_error(stage_key: str, error: GateInputError) -> StageGateResult:
        return StageGateResult(
            status="blocked",
            passed=False,
            diagnostics=[
                Diagnostic(
                    level="blocking",
                    code="stage_gate_input_invalid",
                    message=f"阶段 {stage_key} 产物结构校验失败",
                    details={"reason": str(error)},
                )
            ],
            checked_at=None,
            can_confirm=False,
        )

    @staticmethod
    def _legacy_summary_gate(stage_key: str, content: Mapping[str, Any]) -> StageGateResult:
        if stage_key != "0-source":
            return StageGateResult(
                status="blocked",
                passed=False,
                diagnostics=[
                    Diagnostic(
                        level="blocking",
                        code="legacy_summary_stage_unsupported",
                        message=f"阶段 {stage_key} 不允许使用 legacy summary 草稿",
                        path="summary",
                    )
                ],
                checked_at=None,
                can_confirm=False,
            )
        summary = content.get("summary")
        if isinstance(summary, str) and summary.strip():
            return StageGateResult(
                status="warning",
                passed=True,
                diagnostics=[
                    Diagnostic(
                        level="warning",
                        code="legacy_summary_compatibility",
                        message=f"阶段 {stage_key} 使用 legacy summary 草稿，仅保留兼容确认语义",
                        path="summary",
                    )
                ],
                checked_at=None,
                can_confirm=True,
            )
        return StageGateResult(
            status="blocked",
            passed=False,
            diagnostics=[
                Diagnostic(
                    level="blocking",
                    code="legacy_summary_empty",
                    message="legacy summary 草稿不能为空",
                    path="summary",
                )
            ],
            checked_at=None,
            can_confirm=False,
        )

    @classmethod
    def _recalculate_phase2_gate(
        cls,
        stage_key: str,
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> StageGateResult | None:
        content = cls._phase2_content(stage)
        if not cls._phase2_content_requires_recalculation(stage_key, content, stage):
            assert isinstance(content, Mapping)
            return cls._legacy_summary_gate(stage_key, content)
        config_snapshot = run.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = json_loads(run.get("config_snapshot_json"), {})
        target_duration_sec = (
            config_snapshot.get("target_duration_sec")
            if isinstance(config_snapshot, Mapping)
            else None
        )
        try:
            return evaluate_stage_gate(
                stage_key,
                content,
                target_duration_sec=target_duration_sec,
                config_snapshot=config_snapshot,
                now=None,
            )
        except GateInputError as exc:
            return cls._blocked_gate_from_input_error(stage_key, exc)

    @staticmethod
    def _script_gate_from_diagnostics(
        diagnostics: list[Any],
    ) -> StageGateResult:
        ordered = sorted(
            diagnostics,
            key=lambda item: (
                getattr(item, "code", ""),
                getattr(item, "path", "") or "",
                getattr(item, "message", ""),
            ),
        )
        has_blocking = any(getattr(item, "level", None) == "blocking" for item in ordered)
        return StageGateResult(
            status="blocked" if has_blocking else "warning" if ordered else "ready",
            passed=not has_blocking,
            diagnostics=ordered,
            checked_at=None,
            can_confirm=not has_blocking,
        )

    @classmethod
    def _recalculate_script_gate(
        cls,
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
    ) -> StageGateResult:
        content = cls._phase2_content(stage)
        rhythm_stage = stages.get("2-rhythm")
        rhythm_content = cls._phase2_content(rhythm_stage or {})
        config_snapshot = run.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = json_loads(run.get("config_snapshot_json"), {})
        target_duration_sec = (
            config_snapshot.get("target_duration_sec", 30)
            if isinstance(config_snapshot, Mapping)
            else 30
        )
        if target_duration_sec is None:
            target_duration_sec = 30
        try:
            diagnostics = script_stage.validate_script_output(
                content,
                rhythm=rhythm_content,
                target_duration_sec=target_duration_sec,
            )
        except (TypeError, ValueError) as exc:
            return cls._blocked_gate_from_input_error("3-script", GateInputError(str(exc)))
        return cls._script_gate_from_diagnostics(diagnostics)

    @staticmethod
    def _run_config_snapshot(run: Mapping[str, Any]) -> Mapping[str, Any]:
        config_snapshot = run.get("config_snapshot")
        if isinstance(config_snapshot, Mapping):
            return config_snapshot
        decoded = json_loads(run.get("config_snapshot_json"), {})
        return decoded if isinstance(decoded, Mapping) else {}

    @staticmethod
    def _blocking_gate(
        stage_key: str,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> StageGateResult:
        return StageGateResult(
            status="blocked",
            passed=False,
            diagnostics=[
                Diagnostic(
                    level="blocking",
                    code=code,
                    message=message,
                    path=path,
                    details=dict(details or {}),
                )
            ],
            checked_at=None,
            can_confirm=False,
        )

    @classmethod
    def _recalculate_storyboard_gate(
        cls,
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
    ) -> StageGateResult:
        config_snapshot = cls._run_config_snapshot(run)
        target_duration_sec = config_snapshot.get("target_duration_sec", 30)
        script_content = cls._stage_content(stages.get("3-script", {}))
        rhythm_content = server_bound_rhythm_content(
            cls._stage_content(stages.get("2-rhythm", {})),
            config_snapshot,
        )
        try:
            diagnostics = validate_storyboard_output(
                cls._stage_content(stage),
                script=script_content if script_content else None,
                rhythm=rhythm_content if rhythm_content else None,
                target_duration_sec=target_duration_sec,
            )
        except (TypeError, ValueError) as exc:
            return cls._blocked_gate_from_input_error(
                "4-storyboard",
                GateInputError(str(exc)),
            )
        return cls._script_gate_from_diagnostics(diagnostics)

    @classmethod
    def _recalculate_prompt_gate(
        cls,
        stage_key: str,
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
    ) -> StageGateResult:
        content = cls._stage_content(stage)
        if isinstance(content, Mapping):
            for key in ("compiled_output", "stage_output", "content", "output"):
                nested = content.get(key)
                if isinstance(nested, Mapping) and "stage_key" not in content:
                    content = nested
                    break
            if isinstance(content, Mapping) and "shot_map" in content:
                content = {key: value for key, value in content.items() if key != "shot_map"}
        try:
            output = PromptStageOutput.model_validate(content)
        except (TypeError, ValueError) as exc:
            return cls._blocked_gate_from_input_error(stage_key, GateInputError(str(exc)))

        diagnostics = list(output.diagnostics)
        if output.stage_key != stage_key:
            diagnostics.append(
                Diagnostic(
                    level="blocking",
                    code="prompt_stage_key_mismatch",
                    message="Prompt 产物的 stage_key 与当前阶段不一致",
                    path="stage_key",
                    details={"expected": stage_key, "actual": output.stage_key},
                )
            )
        if not output.bundles:
            diagnostics.append(
                Diagnostic(
                    level="blocking",
                    code="prompt_stage_no_bundles",
                    message="Prompt 阶段至少需要一个 compiled bundle",
                    path="bundles",
                )
            )

        stage_signature = str(stage.get("input_signature") or "").strip()
        if stage_signature and output.artifact_meta.input_signature != stage_signature:
            diagnostics.append(
                Diagnostic(
                    level="blocking",
                    code="prompt_stage_input_signature_mismatch",
                    message="Prompt 产物 input_signature 与阶段输入不一致",
                    path="artifact_meta.input_signature",
                    details={
                        "expected": stage_signature,
                        "actual": output.artifact_meta.input_signature,
                    },
                )
            )
        provenance_signature = str(output.provenance.get("input_signature") or "").strip()
        if stage_signature and provenance_signature and provenance_signature != stage_signature:
            diagnostics.append(
                Diagnostic(
                    level="blocking",
                    code="prompt_stage_provenance_signature_mismatch",
                    message="Prompt provenance 的 input_signature 与阶段输入不一致",
                    path="provenance.input_signature",
                    details={"expected": stage_signature, "actual": provenance_signature},
                )
            )

        config_snapshot = cls._run_config_snapshot(run)
        target_duration = config_snapshot.get("target_duration_sec")
        if isinstance(target_duration, int | float) and not isinstance(target_duration, bool):
            if abs(output.target_duration_sec - float(target_duration)) > 0.01:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_stage_target_duration_mismatch",
                        message="Prompt 产物目标时长与运行配置不一致",
                        path="target_duration_sec",
                        details={
                            "expected": float(target_duration),
                            "actual": output.target_duration_sec,
                        },
                    )
                )

        channel_name = "image" if stage_key == "5a-image-prompts" else "video"
        for bundle_key, bundle in output.bundles.items():
            channel = bundle.image if channel_name == "image" else bundle.video
            bundle_path = f"bundles[{bundle_key!r}]"
            if channel is None:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_stage_channel_missing",
                        message=f"{stage_key} bundle 缺少 {channel_name} channel",
                        path=f"{bundle_path}.{channel_name}",
                    )
                )
                continue
            prompt_text = str(getattr(channel, "prompt", "") or "").strip()
            if not prompt_text:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_missing",
                        message="compiled Prompt 不能为空",
                        path=f"{bundle_path}.{channel_name}.prompt",
                    )
                )
            if bundle.status == "blocked" and not any(
                item.level == "blocking" for item in bundle.diagnostics
            ):
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_bundle_status_inconsistent",
                        message="bundle 标记为 blocked 但没有 blocking 诊断",
                        path=f"{bundle_path}.status",
                    )
                )

            expected_base_hash, expected_input_hash = calculate_prompt_bundle_hashes(bundle)
            if bundle.base_compiler_hash != expected_base_hash:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_bundle_base_hash_mismatch",
                        message="compiled bundle 的基础签名与内容不一致",
                        path=f"{bundle_path}.base_compiler_hash",
                    )
                )
            if bundle.input_hash != expected_input_hash:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_bundle_input_hash_mismatch",
                        message="compiled bundle 的输入签名与内容不一致",
                        path=f"{bundle_path}.input_hash",
                    )
                )
            try:
                expected_final = apply_human_override(bundle.generated, bundle.human_override)
                if cls._model_dump(expected_final) != cls._model_dump(bundle.final):
                    diagnostics.append(
                        Diagnostic(
                            level="blocking",
                            code="prompt_bundle_final_mismatch",
                            message="compiled bundle final 未保持 generated/人工覆盖契约",
                            path=f"{bundle_path}.final",
                        )
                    )
            except (TypeError, ValueError):
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="prompt_bundle_final_invalid",
                        message="compiled bundle final 无法按人工覆盖契约校验",
                        path=f"{bundle_path}.final",
                    )
                )

            if stage_key == "5b-video-prompts" and bundle.video is not None:
                if bundle.video.end_state in (None, ""):
                    diagnostics.append(
                        Diagnostic(
                            level="blocking",
                            code="prompt_video_end_state_missing",
                            message="视频 Prompt 必须包含明确结束状态",
                            path=f"{bundle_path}.video.end_state",
                        )
                    )
                timeline = list(bundle.video.timeline)
                if not timeline:
                    diagnostics.append(
                        Diagnostic(
                            level="blocking",
                            code="prompt_video_timeline_missing",
                            message="视频 Prompt 必须包含动作时间轴",
                            path=f"{bundle_path}.video.timeline",
                        )
                    )
                else:
                    previous_end = 0.0
                    for index, segment in enumerate(timeline):
                        if abs(segment.start - previous_end) > 0.01:
                            diagnostics.append(
                                Diagnostic(
                                    level="blocking",
                                    code="prompt_video_timeline_discontinuous",
                                    message="视频 Prompt 时间轴存在间隙或未从 0 开始",
                                    path=f"{bundle_path}.video.timeline[{index}]",
                                )
                            )
                            break
                        previous_end = segment.end
                    if abs(previous_end - bundle.video.generation_duration_sec) > 0.01:
                        diagnostics.append(
                            Diagnostic(
                                level="blocking",
                                code="prompt_video_timeline_end_mismatch",
                                message="视频 Prompt 时间轴未覆盖实际生成时长",
                                path=f"{bundle_path}.video.timeline",
                            )
                        )

        if output.status == "blocked" and not any(
            item.level == "blocking" for item in diagnostics
        ):
            diagnostics.append(
                Diagnostic(
                    level="blocking",
                    code="prompt_stage_status_inconsistent",
                    message="Prompt 产物标记为 blocked 但缺少 blocking 诊断",
                    path="status",
                )
            )
        return diagnostics_status(diagnostics, checked_at=None)

    @classmethod
    def _recalculate_stage_gate(
        cls,
        stage_key: str,
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
    ) -> StageGateResult:
        if stage_key in _PHASE2_STAGE_KEYS:
            result = cls._recalculate_phase2_gate(stage_key, stage, run)
            if result is not None:
                return result
        elif stage_key == "3-script":
            return cls._recalculate_script_gate(stage, run, stages)
        elif stage_key == "4-storyboard":
            return cls._recalculate_storyboard_gate(stage, run, stages)
        elif stage_key in _PROMPT_STAGE_KEYS:
            return cls._recalculate_prompt_gate(stage_key, stage, run)
        return cls._blocking_gate(
            stage_key,
            "stage_gate_not_recalculable",
            "当前阶段没有可验证的服务端草稿门禁",
        )

    @staticmethod
    def _artifact_schema_version(content: Any, run: Mapping[str, Any]) -> str:
        if isinstance(content, Mapping):
            for metadata_key in ("artifact_meta", "meta"):
                metadata = content.get(metadata_key)
                if isinstance(metadata, Mapping) and metadata.get("schema_version"):
                    return str(metadata["schema_version"])
            nested = content.get("content")
            if isinstance(nested, Mapping):
                for metadata_key in ("artifact_meta", "meta"):
                    metadata = nested.get(metadata_key)
                    if isinstance(metadata, Mapping) and metadata.get("schema_version"):
                        return str(metadata["schema_version"])
        return str(run.get("schema_version") or "1")

    @classmethod
    def _downstream_input_signature(
        cls,
        next_stage_key: str,
        run: Mapping[str, Any],
        upstream_content: Any,
    ) -> str:
        config_snapshot = run.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = json_loads(run.get("config_snapshot_json"), {})
        return calculate_signature(
            next_stage_key,
            run.get("input_signature") or "",
            upstream_content,
            config_snapshot if isinstance(config_snapshot, Mapping) else {},
            {
                "schema_version": cls._artifact_schema_version(upstream_content, run),
                "gate_version": GATE_VERSION,
            },
        )

    @staticmethod
    def _interrupt_active_stage_jobs(connection: Any, stage_id: str, now: str) -> None:
        connection.execute(
            update(job)
            .where(
                job.c.workflow_stage_id == stage_id,
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .values(
                status="interrupted",
                message="任务因阶段输入签名变化而中断",
                error="动态 Job 与新的阶段 input_signature 不一致，禁止复用",
                finished_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _stale_delivery_for_prompt_change(
        connection: Any,
        run_id: str,
        message: str,
        now: str,
    ) -> None:
        delivery = row_dict(
            connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.workflow_run_id == run_id,
                    workflow_stage.c.stage_key == "6-delivery",
                )
            ).first()
        )
        if delivery is None:
            return
        connection.execute(
            update(job)
            .where(
                job.c.workflow_stage_id == delivery["id"],
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .values(
                status="interrupted",
                error=message,
                message="任务因 Prompt 输入快照变化而中断",
                finished_at=now,
                updated_at=now,
            )
        )
        if delivery.get("status") not in {"confirmed", "cancelled", "stale"}:
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == delivery["id"])
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

    def _prompt_provider_capabilities(
        self,
        run: Mapping[str, Any],
        stage_key: str,
    ) -> dict[str, Any]:
        config = self._run_config_snapshot(run)
        channel = "image" if stage_key == "5a-image-prompts" else "video"
        candidates: list[Any] = []
        for key in (
            f"{stage_key}_provider_capabilities",
            f"{channel}_provider_capabilities",
            f"{stage_key}_capabilities",
            f"{channel}_capabilities",
            "provider_capabilities",
            "provider_capability_snapshot",
            "capabilities",
        ):
            candidate = config.get(key)
            if isinstance(candidate, Mapping):
                candidates.append(candidate.get(stage_key) or candidate.get(channel) or candidate)
        raw = next((value for value in candidates if isinstance(value, Mapping)), {})
        provider_id, provider_model = self._prompt_provider_config(run, {}, stage_key)
        normalized = normalize_capabilities(raw).model_dump(mode="json")
        normalized["provider_id"] = provider_id
        normalized["model"] = provider_model
        return normalized

    def _prompt_stage_input_signature(
        self,
        connection: Any,
        next_stage_key: str,
        run: Mapping[str, Any],
        stage4: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        config_snapshot = run.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = json_loads(run.get("config_snapshot_json"), {})
        content = self._stage_content(stage4)
        raw_shots = content.get("shots", []) if isinstance(content, Mapping) else []
        raw_shots = raw_shots if isinstance(raw_shots, list) else []
        capabilities = self._prompt_provider_capabilities(run, next_stage_key)
        asset_ids: set[str] = set()
        shot_snapshots: list[dict[str, Any]] = []
        reference_hashes: dict[str, str] = {}

        current_reference_plans: dict[str, Mapping[str, Any]] = {}
        current_rows = connection.execute(
            select(shot).where(shot.c.chapter_id == run.get("chapter_id"))
        ).all()
        if current_rows:
            for row in current_rows:
                record = dict(row._mapping)
                external_key = str(record.get("external_key") or "")
                if not external_key:
                    continue
                resolved = DynamicPromptStageService._resolve_reference_plan(
                    connection,
                    record.get("reference_bindings_json"),
                    project_id=str(run.get("project_id") or ""),
                    chapter_id=str(run.get("chapter_id") or ""),
                    provider_capabilities=capabilities,
                    video_stage=next_stage_key == "5b-video-prompts",
                )
                if isinstance(resolved, Mapping):
                    current_reference_plans[external_key] = resolved

        project_root = None
        try:
            project_root = self.storage.project_path(str(run.get("project_id") or ""))
        except (FileServiceError, OSError, ValueError):
            project_root = None
        for raw_shot in raw_shots:
            if not isinstance(raw_shot, Mapping):
                continue
            external_key = str(raw_shot.get("external_key") or "")
            character_ids = [
                str(value) for value in raw_shot.get("character_asset_ids", []) if value
            ]
            prop_ids = [str(value) for value in raw_shot.get("prop_asset_ids", []) if value]
            scene_id = str(raw_shot.get("scene_asset_id") or "")
            asset_ids.update(character_ids, prop_ids)
            if scene_id:
                asset_ids.add(scene_id)
            canonical_plan = current_reference_plans.get(external_key)
            if canonical_plan is None:
                raw_references = raw_shot.get("reference_bindings", [])
                canonical_plan = build_reference_plan(
                    adapt_reference_bindings(raw_references),
                    project_root=project_root,
                    provider_capabilities=capabilities,
                    video=next_stage_key == "5b-video-prompts",
                ).model_dump(mode="json")
            has_reference_inputs = isinstance(canonical_plan, Mapping) and any(
                canonical_plan.get(name)
                for name in ("slots", "actual_local_files", "actual_urls", "text_mapping")
            )
            reference_hashes[external_key] = str(
                canonical_plan.get("snapshot_hash")
                if has_reference_inputs and isinstance(canonical_plan, Mapping)
                else ""
            )
            shot_snapshots.append(
                {
                    "external_key": external_key,
                    "shot_no": raw_shot.get("shot_no"),
                    "ordinal": raw_shot.get("ordinal"),
                    "scene_asset_id": scene_id,
                    "character_asset_ids": character_ids,
                    "prop_asset_ids": prop_ids,
                    "reference_plan_hash": reference_hashes[external_key],
                }
            )
        asset_rows = (
            connection.execute(select(asset).where(asset.c.id.in_(sorted(asset_ids)))).all()
            if asset_ids
            else []
        )
        canonical_anchors: dict[str, Any] = {}
        try:
            storyboard = StoryboardOutput.model_validate(content)
            character_stage = self._stage_from_connection(
                connection,
                str(run.get("id") or ""),
                "1-character-bios",
            )
            character_bios = None
            if character_stage is not None and character_stage.get("status") == "confirmed":
                character_bios = CharacterBiosOutput.model_validate(
                    self._stage_content(character_stage)
                )
            anchor_run = {
                **dict(run),
                "asset_mapping": json_loads(run.get("asset_mapping_json"), []),
            }
            canonical_anchors = DynamicPromptStageService._build_anchors(
                connection,
                run=anchor_run,
                storyboard=storyboard,
                character_bios=character_bios,
            )
        except (TypeError, ValueError):
            canonical_anchors = {}
        asset_versions = [
            {
                "asset_id": str(row.id),
                "asset_type": row.asset_type,
                "revision": row.revision,
                "anchor_version": anchor_version(canonical_anchors[str(row.id)])
                if str(row.id) in canonical_anchors
                else self._asset_anchor_version(dict(row._mapping)),
            }
            for row in asset_rows
        ]
        asset_versions.sort(key=lambda value: (value["asset_id"], str(value["asset_type"])))
        provider_id = str(capabilities.get("provider_id") or "")
        provider_model = str(capabilities.get("model") or "")
        payload = dynamic_materializer.build_prompt_stage_signature_payload(
            stage_key=next_stage_key,
            run_input_signature=str(run.get("input_signature") or ""),
            shots=shot_snapshots,
            asset_versions=asset_versions,
            reference_plan_hashes=reference_hashes,
            provider_id=provider_id,
            provider_model=provider_model,
            provider_capabilities=capabilities,
            compiler_version=_PROMPT_STAGE_COMPILER_VERSION,
            normalizer_version=normalizers.NORMALIZER_VERSION,
            gate_version=GATE_VERSION,
        )
        prompt_stage = self._stage_from_connection(
            connection,
            str(run.get("id") or ""),
            next_stage_key,
        )
        override_snapshot = self._prompt_stage_override_snapshot(
            connection,
            prompt_stage,
        )
        payload["human_override_hash"] = prompt_stage_override_hash(override_snapshot)
        return dynamic_materializer.calculate_prompt_stage_input_signature(payload), payload

    @staticmethod
    def _prompt_stage_override_snapshot(
        connection: Any,
        stage: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        if stage is None:
            return {}
        rows = connection.execute(
            select(job.c.input_json)
            .where(
                job.c.workflow_stage_id == stage.get("id"),
                job.c.job_type == "dynamic_stage_generate",
            )
            .order_by(job.c.updated_at.desc(), job.c.id.desc())
            .limit(1)
        ).all()
        for row in rows:
            input_data = json_loads(row.input_json, {})
            if not isinstance(input_data, Mapping):
                continue
            for key in ("overrides_by_external_key", "human_overrides", "overrides"):
                value = input_data.get(key)
                if isinstance(value, Mapping):
                    return value
        return {}

    def _prepare_prompt_stage_barrier(
        self,
        connection: Any,
        run: Mapping[str, Any],
        stage4: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
    ) -> list[str]:
        """在阶段 4 确认事务内准备 5a/5b 两个并行 Prompt 阶段。"""
        self._require_runner()
        prompt_stages: list[tuple[str, Mapping[str, Any]]] = []
        for stage_key in _PROMPT_STAGE_KEYS:
            prompt_stage = stages.get(stage_key)
            if prompt_stage is None:
                raise not_found("阶段")
            prompt_stages.append((stage_key, prompt_stage))

        job_ids: list[str] = []
        for stage_key, prompt_stage in prompt_stages:
            current_status = str(prompt_stage.get("status") or "")
            if current_status in {"confirmed", "cancelled"}:
                continue
            if current_status not in _PHASE2_QUEUEABLE_STAGE_STATUSES:
                # queued/running/ready 由当前执行或人工确认继续处理，不能重复创建。
                continue

            input_signature, signature_payload = self._prompt_stage_input_signature(
                connection,
                stage_key,
                run,
                stage4,
            )
            now = utc_now()
            if prompt_stage.get("input_signature") != input_signature:
                self._interrupt_active_stage_jobs(connection, prompt_stage["id"], now)
                self._stale_delivery_for_prompt_change(
                    connection,
                    str(run.get("id") or ""),
                    "Prompt 输入快照变化，delivery 已标记 stale",
                    now,
                )
            self._queue_status(current_status)
            self._update_stage(
                connection,
                prompt_stage["id"],
                {
                    "status": "queued",
                    "input_signature": input_signature,
                    "output_refs_json": json_dumps(
                        [
                            {
                                "capture_mode": "prompt_input_snapshot",
                                "snapshot_version": "dynamic-prompt-stage-input-v2",
                                "snapshot": signature_payload,
                            }
                        ]
                    ),
                    "updated_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "error": "",
                },
            )
            updated_stage = {
                **prompt_stage,
                "status": "queued",
                "input_signature": input_signature,
            }
            reference = self.job_bridge.ensure_stage_job_in_transaction(
                connection,
                run,
                updated_stage,
                retry_failed=True,
            )
            if reference.status == "queued":
                job_ids.append(reference.job_id)
        return job_ids

    @classmethod
    def _bump_run(
        cls,
        connection: Any,
        run_id: str,
        expected_revision: int,
        values: Mapping[str, Any] | None = None,
    ) -> int:
        update_values = {
            "revision": workflow_run.c.revision + 1,
            "updated_at": utc_now(),
            **dict(values or {}),
        }
        result = connection.execute(
            update(workflow_run)
            .where(
                workflow_run.c.id == run_id,
                workflow_run.c.revision == expected_revision,
            )
            .values(**update_values)
        )
        if result.rowcount != 1:
            current = cls._run_from_connection(connection, run_id)
            if current is None:
                raise DynamicWorkflowNotFoundError(f"运行不存在：{run_id}")
            raise RevisionConflict(current.get("revision"), expected_revision)
        return expected_revision + 1

    @staticmethod
    def _update_stage(connection: Any, stage_id: str, values: Mapping[str, Any]) -> None:
        result = connection.execute(
            update(workflow_stage).where(workflow_stage.c.id == stage_id).values(**dict(values))
        )
        if result.rowcount != 1:
            raise DynamicWorkflowRepositoryError(f"阶段状态更新失败：{stage_id}")

    @staticmethod
    def _stage_content(stage: Mapping[str, Any]) -> Any:
        draft = stage.get("draft_output")
        if draft not in (None, {}):
            return draft
        normalized = stage.get("normalized_output")
        return normalized if normalized is not None else {}

    @staticmethod
    def _materialize_dynamic_stage(stage_key: str, content: Any) -> bool:
        """保留旧 API 的摘要确认语义，只对标准动态产物接入 canonical。"""
        if stage_key == "2-rhythm":
            return False
        if stage_key not in {"0-source", "1-character-bios", "3-script", "4-storyboard"}:
            return False
        if stage_key == "0-source" and isinstance(content, Mapping):
            # 旧手工 API 只保存 {summary: ...}，不能因为接入动态物化而改变其确认行为。
            return set(content) != {"summary"} or not isinstance(content.get("summary"), str)
        return True

    @staticmethod
    def _dynamic_source_signature(run: Mapping[str, Any]) -> str:
        value = str(run.get("input_signature") or "").strip()
        if value:
            return value
        value = str(run.get("source_signature") or "").strip()
        if value:
            return value
        return "dynamic-source-unknown"

    @staticmethod
    def _canonical_revision(record: Mapping[str, Any] | None) -> int:
        if record is None:
            return 0
        value = record.get("revision")
        if type(value) is not int or value < 0:
            raise MaterializationConflict("canonical 内容 revision 无效")
        return value

    @staticmethod
    def _raise_materialization_revision_conflict(expected: int, actual: Any) -> None:
        raise MaterializationConflict(
            expected_revision=expected,
            actual_revision=actual if type(actual) is int else None,
        )

    @staticmethod
    def _builder_revision(existing_revision: int) -> int:
        # 纯 builder 合同要求正整数；数据库不存在 canonical 时仍按 0 做
        # optimistic check，传给 builder 的占位 revision 仅用于生成纯 payload。
        return max(existing_revision, 1)

    @staticmethod
    def _requested_canonical_revision(content: Any, actual_revision: int) -> int:
        if isinstance(content, Mapping):
            for key in ("expected_revision", "canonical_revision", "revision"):
                value = content.get(key)
                if type(value) is int and value >= 0:
                    return value
        return actual_revision

    @staticmethod
    def _asset_anchor_version(row: Mapping[str, Any]) -> str:
        profile = json_loads(row.get("profile_json"), {})
        visual_anchor = json_loads(row.get("visual_anchor_json"), {})
        raw = {
            "asset_id": row.get("id"),
            "asset_type": row.get("asset_type"),
            "name": row.get("name"),
            "profile_json": profile if isinstance(profile, Mapping) else {},
            "visual_anchor_json": visual_anchor if isinstance(visual_anchor, Mapping) else {},
        }
        try:
            asset_type_value = str(row.get("asset_type") or "")
            anchor = asset_profile_to_anchor(
                raw,
                asset_id=str(row.get("id") or ""),
                asset_revision=row.get("revision"),
                asset_type=(
                    cast(Literal["character", "scene", "prop"], asset_type_value)
                    if asset_type_value in {"character", "scene", "prop"}
                    else None
                ),
            )
        except (TypeError, ValueError):
            return ""
        return anchor_version(anchor)

    def _validate_shot_asset_bindings(
        self,
        connection: Any,
        run: Mapping[str, Any],
        plan: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """在阶段 4 事务中校验显式素材的项目、类型和锁定 revision。"""

        requirements = dynamic_materializer.extract_shot_asset_requirements(plan)
        if not requirements:
            return []
        asset_ids = sorted({str(item["asset_id"]) for item in requirements})
        rows = connection.execute(
            select(asset).where(asset.c.id.in_(asset_ids)).with_for_update()
        ).all()
        by_id = {str(row.id): dict(row._mapping) for row in rows}
        mappings = run.get("asset_mapping")
        if not isinstance(mappings, list):
            mappings = json_loads(run.get("asset_mapping_json"), [])
        mapping_by_asset_id = {
            str(item.get("asset_id")): item
            for item in mappings
            if isinstance(item, Mapping) and item.get("asset_id")
        }
        diagnostics: list[Diagnostic] = []
        snapshots: list[dict[str, Any]] = []
        project_id = str(run.get("project_id") or "")
        for item in requirements:
            asset_id = str(item["asset_id"])
            expected_type = str(item["expected_asset_type"])
            path = str(item["path"])
            row = by_id.get(asset_id)
            if row is None:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="shot_asset_missing",
                        message=f"分镜绑定的 asset 不存在：{asset_id}",
                        path=path,
                        details={"asset_id": asset_id, "expected_asset_type": expected_type},
                    )
                )
                continue
            if row.get("project_id") != project_id:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="shot_asset_project_mismatch",
                        message=f"分镜绑定的 asset 不属于当前项目：{asset_id}",
                        path=path,
                        details={"asset_id": asset_id, "project_id": row.get("project_id")},
                    )
                )
            if row.get("asset_type") != expected_type:
                diagnostics.append(
                    Diagnostic(
                        level="blocking",
                        code="shot_asset_type_mismatch",
                        message=f"分镜绑定的 asset 类型不匹配：{asset_id}",
                        path=path,
                        details={
                            "asset_id": asset_id,
                            "expected_asset_type": expected_type,
                            "actual_asset_type": row.get("asset_type"),
                        },
                    )
                )
            mapping = mapping_by_asset_id.get(asset_id)
            if isinstance(mapping, Mapping) and mapping.get("reuse_mode") == "approved_existing":
                locked_revision = mapping.get("locked_revision")
                if type(locked_revision) is not int or locked_revision < 1:
                    diagnostics.append(
                        Diagnostic(
                            level="blocking",
                            code="shot_asset_locked_revision_missing",
                            message=f"approved_existing asset 缺少 locked_revision：{asset_id}",
                            path=path,
                        )
                    )
                elif row.get("revision") != locked_revision:
                    raise MaterializationConflict(
                        f"approved_existing asset revision 已变化：{asset_id}",
                        expected_revision=locked_revision,
                        actual_revision=row.get("revision"),
                        diagnostics=[
                            Diagnostic(
                                level="blocking",
                                code="shot_asset_locked_revision_mismatch",
                                message=f"asset revision 与 locked_revision 不一致：{asset_id}",
                                path=path,
                                details={
                                    "asset_id": asset_id,
                                    "locked_revision": locked_revision,
                                    "actual_revision": row.get("revision"),
                                },
                            )
                        ],
                    )
            snapshots.append(
                {
                    "asset_id": asset_id,
                    "asset_type": row.get("asset_type"),
                    "revision": row.get("revision"),
                    "anchor_version": self._asset_anchor_version(row),
                }
            )
        if diagnostics:
            raise MaterializationError("阶段 4 分镜资产绑定校验失败", diagnostics)
        snapshots.sort(key=lambda value: (str(value["asset_id"]), str(value["asset_type"])))
        return snapshots

    @staticmethod
    def _diagnostic_level(value: Any) -> Any:
        if isinstance(value, Mapping):
            return value.get("level")
        return getattr(value, "level", None)

    def _prepare_dynamic_materialization(
        self,
        connection: Any,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        stages: Mapping[str, Mapping[str, Any]],
        stage_key: str,
    ) -> dict[str, Any] | None:
        """在确认事务中构建纯 payload，并读取 canonical 的 revision 快照。"""
        content = self._stage_content(stage)
        if not self._materialize_dynamic_stage(stage_key, content):
            return None

        chapter_id = str(run.get("chapter_id") or "")
        project_id = str(run.get("project_id") or "")
        run_id = str(run.get("id") or "")
        source_sig = self._dynamic_source_signature(run)

        if stage_key == "0-source":
            existing = row_dict(
                connection.execute(
                    select(novel_analysis).where(novel_analysis.c.chapter_id == chapter_id)
                ).first()
            )
            existing_revision = self._canonical_revision(existing)
            expected_canonical_revision = self._requested_canonical_revision(
                content, existing_revision
            )
            if expected_canonical_revision != existing_revision:
                self._raise_materialization_revision_conflict(
                    expected_canonical_revision, existing_revision
                )
            projection = self.materializer.build_novel_analysis_projection(
                content,
                chapter_id=chapter_id,
                source_signature=source_sig,
                expected_revision=self._builder_revision(expected_canonical_revision),
            )
            return {
                "stage_key": stage_key,
                "existing": existing,
                "existing_revision": existing_revision,
                "expected_canonical_revision": expected_canonical_revision,
                "payload": projection,
                "source_signature": source_sig,
            }

        if stage_key == "1-character-bios":
            plan = self.materializer.build_asset_materialization_plan(
                content,
                project_id=project_id,
                run_id=run_id,
                reuse_mapping=run.get("asset_mapping")
                or json_loads(run.get("asset_mapping_json"), []),
            )
            for index, item in enumerate(plan):
                if item.get("reuse_mode") != "approved_existing":
                    continue
                expected_type = "character"
                planned_type = str(item.get("asset_type") or expected_type)
                if planned_type != expected_type:
                    raise MaterializationError(
                        "approved_existing 阶段 1 资产类型与角色阶段不匹配",
                        [
                            Diagnostic(
                                level="blocking",
                                code="approved_existing_asset_type_mismatch",
                                message="阶段 1 approved_existing 只能复用 character asset",
                                path=f"characters[{index}].asset_type",
                                details={
                                    "expected_asset_type": expected_type,
                                    "planned_asset_type": planned_type,
                                    "asset_id": item.get("asset_id"),
                                },
                            )
                        ],
                    )
                existing = row_dict(
                    connection.execute(
                        select(asset).where(asset.c.id == str(item.get("asset_id") or ""))
                    ).first()
                )
                if existing is not None and existing.get("asset_type") != expected_type:
                    raise MaterializationError(
                        "approved_existing 素材类型与阶段 1 期望类型不匹配",
                        [
                            Diagnostic(
                                level="blocking",
                                code="approved_existing_asset_type_mismatch",
                                message="不能用非 character asset 复用角色阶段素材",
                                path=f"characters[{index}].asset_id",
                                details={
                                    "expected_asset_type": expected_type,
                                    "actual_asset_type": existing.get("asset_type"),
                                    "asset_id": item.get("asset_id"),
                                },
                            )
                        ],
                    )
            mapping: list[dict[str, Any]] = []
            for item in plan:
                reuse_mode = str(item.get("reuse_mode") or "new")
                if reuse_mode not in {"new", "approved_existing"}:
                    raise MaterializationError(f"不支持的素材复用模式：{reuse_mode}")
                typed_reuse_mode = cast(Literal["new", "approved_existing"], reuse_mode)
                locked_revision = item.get("locked_revision")
                if typed_reuse_mode == "approved_existing":
                    if type(locked_revision) is not int or locked_revision < 1:
                        raise MaterializationError("approved_existing 素材映射缺少 locked_revision")
                else:
                    locked_revision = None
                mapping.append(
                    AssetMapping(
                        workflow_asset_id=str(item.get("workflow_asset_id") or ""),
                        asset_id=str(item.get("asset_id") or "") or None,
                        usage="character",
                        locked_revision=(
                            int(locked_revision or 0) + 1
                            if typed_reuse_mode == "approved_existing"
                            else locked_revision
                        ),
                        reuse_mode=typed_reuse_mode,
                    ).model_dump(mode="json")
                )
            return {
                "stage_key": stage_key,
                "plan": list(plan),
                "mapping": mapping,
                "source_signature": source_sig,
            }

        if stage_key == "2-rhythm":
            return None

        if stage_key == "3-script":
            existing = row_dict(
                connection.execute(
                    select(screenplay).where(screenplay.c.chapter_id == chapter_id)
                ).first()
            )
            existing_revision = self._canonical_revision(existing)
            expected_canonical_revision = self._requested_canonical_revision(
                content, existing_revision
            )
            if expected_canonical_revision != existing_revision:
                self._raise_materialization_revision_conflict(
                    expected_canonical_revision, existing_revision
                )
            projection = self.materializer.build_screenplay_projection(
                content,
                chapter_id=chapter_id,
                source_signature=source_sig,
                expected_revision=self._builder_revision(expected_canonical_revision),
            )
            return {
                "stage_key": stage_key,
                "existing": existing,
                "existing_revision": existing_revision,
                "expected_canonical_revision": expected_canonical_revision,
                "payload": projection,
                "source_signature": source_sig,
            }

        if stage_key == "4-storyboard":
            script_content = self._stage_content(stages.get("3-script", {}))
            rhythm_content = self._stage_content(stages.get("2-rhythm", {}))
            diagnostics = self.materializer.validate_storyboard_output(
                content,
                script=script_content if script_content else None,
                rhythm=server_bound_rhythm_content(
                    rhythm_content if rhythm_content else None,
                    self._run_config_snapshot(run),
                )
                if rhythm_content
                else None,
            )
            blocking = [item for item in diagnostics if self._diagnostic_level(item) == "blocking"]
            if blocking:
                raise MaterializationError("阶段 4 分镜门禁存在 blocking 诊断", blocking)
            existing_numbers = list(
                connection.execute(
                    select(shot.c.shot_no)
                    .where(shot.c.chapter_id == chapter_id)
                    .order_by(shot.c.shot_no)
                ).scalars()
            )
            plan = self.materializer.build_shot_materialization_plan(
                content,
                chapter_id=chapter_id,
                project_id=project_id,
                source_signature=source_sig,
                existing_shot_numbers=existing_numbers,
            )
            asset_versions = self._validate_shot_asset_bindings(connection, run, plan)
            existing_external_keys = {
                str(value)
                for value in connection.execute(
                    select(shot.c.external_key).where(shot.c.chapter_id == chapter_id)
                ).scalars()
                if value
            }
            duplicated = sorted(
                str(item.get("external_key"))
                for item in plan
                if item.get("external_key") in existing_external_keys
            )
            if duplicated:
                raise MaterializationConflict(
                    f"章节已存在相同 external_key：{', '.join(duplicated)}"
                )
            return {
                "stage_key": stage_key,
                "plan": list(plan),
                "existing_external_keys": existing_external_keys,
                "asset_versions": asset_versions,
                "source_signature": source_sig,
            }

        return None

    @staticmethod
    def _json_column(value: Any, default: Any) -> str:
        decoded = json_loads(value, default) if isinstance(value, str) else value
        return json_dumps(decoded if isinstance(decoded, type(default)) else default)

    def _apply_dynamic_materialization(
        self,
        connection: Any,
        run: Mapping[str, Any],
        materialization: Mapping[str, Any],
    ) -> None:
        stage_key = str(materialization["stage_key"])
        source_sig = str(materialization["source_signature"])
        now = utc_now()
        if stage_key == "0-source":
            existing = materialization.get("existing")
            expected_revision = int(materialization["expected_canonical_revision"])
            target_revision = expected_revision + 1
            values = {
                "content_json": json_dumps(materialization["payload"]),
                "status": "confirmed",
                "source_signature": source_sig,
                "revision": target_revision,
                "generated_by": f"dynamic:{run['id']}",
                "error": "",
                "updated_at": now,
            }
            if existing is None:
                try:
                    connection.execute(
                        insert(novel_analysis).values(chapter_id=run["chapter_id"], **values)
                    )
                except IntegrityError as exc:
                    raise MaterializationConflict("novel_analysis 已被其他操作创建") from exc
                return
            result = connection.execute(
                update(novel_analysis)
                .where(
                    novel_analysis.c.chapter_id == run["chapter_id"],
                    novel_analysis.c.revision == expected_revision,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                current = row_dict(
                    connection.execute(
                        select(novel_analysis.c.revision).where(
                            novel_analysis.c.chapter_id == run["chapter_id"]
                        )
                    ).first()
                )
                self._raise_materialization_revision_conflict(
                    expected_revision,
                    current.get("revision") if current else None,
                )
            return

        if stage_key == "1-character-bios":
            project_id = str(run["project_id"])
            expected_asset_type = "character"
            approved_items = [
                item
                for item in materialization["plan"]
                if item.get("reuse_mode") == "approved_existing"
            ]
            for item in approved_items:
                asset_id = str(item.get("asset_id") or "")
                existing = row_dict(
                    connection.execute(select(asset).where(asset.c.id == asset_id)).first()
                )
                planned_type = str(item.get("asset_type") or expected_asset_type)
                actual_type = existing.get("asset_type") if existing is not None else None
                if planned_type != expected_asset_type or actual_type != expected_asset_type:
                    raise MaterializationError(
                        "approved_existing 素材类型与阶段 1 期望类型不匹配",
                        [
                            Diagnostic(
                                level="blocking",
                                code="approved_existing_asset_type_mismatch",
                                message="阶段 1 不能复用非 character asset",
                                path="asset_mapping",
                                details={
                                    "asset_id": asset_id,
                                    "expected_asset_type": expected_asset_type,
                                    "planned_asset_type": planned_type,
                                    "actual_asset_type": actual_type,
                                },
                            )
                        ],
                    )
            for item in materialization["plan"]:
                asset_id = str(item["asset_id"])
                approved = item.get("reuse_mode") == "approved_existing"
                existing = row_dict(
                    connection.execute(select(asset).where(asset.c.id == asset_id)).first()
                )
                profile_json = self._json_column(item.get("profile_json"), {})
                visual_anchor_json = self._json_column(item.get("visual_anchor_json"), {})
                if approved:
                    if existing is None or existing.get("project_id") != project_id:
                        raise MaterializationConflict(f"approved_existing 素材不存在：{asset_id}")
                    locked_revision = item.get("locked_revision")
                    if existing.get("revision") != locked_revision:
                        self._raise_materialization_revision_conflict(
                            int(locked_revision), existing.get("revision")
                        )
                    values = {
                        "asset_type": existing["asset_type"],
                        "name": item.get("name", existing.get("name", "")),
                        "profile_json": profile_json,
                        "visual_anchor_json": visual_anchor_json,
                        "importance": item.get("importance", "normal"),
                        "status": "confirmed",
                        "source_signature": source_sig,
                        "own_signature": item.get("own_signature", ""),
                        "revision": int(locked_revision) + 1,
                        "updated_at": now,
                    }
                    result = connection.execute(
                        update(asset)
                        .where(asset.c.id == asset_id, asset.c.revision == int(locked_revision))
                        .values(**values)
                    )
                    if result.rowcount != 1:
                        current = row_dict(
                            connection.execute(
                                select(asset.c.revision).where(asset.c.id == asset_id)
                            ).first()
                        )
                        self._raise_materialization_revision_conflict(
                            int(locked_revision), current.get("revision") if current else None
                        )
                    continue

                if existing is not None:
                    raise MaterializationConflict(f"动态素材 ID 已存在，拒绝静默覆盖：{asset_id}")
                name = str(item.get("name") or "").strip()
                same_name = connection.execute(
                    select(asset.c.id).where(
                        asset.c.project_id == project_id,
                        asset.c.asset_type == item.get("asset_type", "character"),
                        asset.c.name == name,
                    )
                ).first()
                if same_name is not None:
                    # asset 表保留了同项目同类型同名唯一约束；动态运行不合并同名
                    # 素材，使用运行作用域后缀保留两份独立 canonical 记录。
                    name = f"{name} [dynamic:{run['id']}]"
                    suffix = 2
                    while (
                        connection.execute(
                            select(asset.c.id).where(
                                asset.c.project_id == project_id,
                                asset.c.asset_type == item.get("asset_type", "character"),
                                asset.c.name == name,
                            )
                        ).first()
                        is not None
                    ):
                        name = f"{item.get('name', '')} [dynamic:{run['id']}:{suffix}]"
                        suffix += 1
                try:
                    connection.execute(
                        insert(asset).values(
                            id=asset_id,
                            project_id=project_id,
                            asset_type=item.get("asset_type", "character"),
                            name=name,
                            profile_json=profile_json,
                            visual_anchor_json=visual_anchor_json,
                            importance=item.get("importance", "normal"),
                            status="confirmed",
                            source_signature=source_sig,
                            own_signature=item.get("own_signature", ""),
                            revision=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                except IntegrityError as exc:
                    raise MaterializationConflict(f"动态素材写入冲突：{asset_id}") from exc
            return

        if stage_key == "3-script":
            existing = materialization.get("existing")
            expected_revision = int(materialization["expected_canonical_revision"])
            projection = dict(materialization["payload"])
            target_revision = expected_revision + 1
            content = projection.get("content", projection)
            values = {
                "content_json": json_dumps(content),
                "status": "confirmed",
                "source_signature": source_sig,
                "own_signature": projection.get("own_signature", ""),
                "revision": target_revision,
                "generated_by": f"dynamic:{run['id']}",
                "error": "",
                "updated_at": now,
            }
            if isinstance(projection, Mapping) and isinstance(
                projection.get("provenance"), Mapping
            ):
                provenance = dict(projection["provenance"])
                provenance["revision"] = target_revision
                projection["provenance"] = provenance
                projection["revision"] = target_revision
            if existing is None:
                try:
                    connection.execute(
                        insert(screenplay).values(chapter_id=run["chapter_id"], **values)
                    )
                except IntegrityError as exc:
                    raise MaterializationConflict("screenplay 已被其他操作创建") from exc
                return
            result = connection.execute(
                update(screenplay)
                .where(
                    screenplay.c.chapter_id == run["chapter_id"],
                    screenplay.c.revision == expected_revision,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                current = row_dict(
                    connection.execute(
                        select(screenplay.c.revision).where(
                            screenplay.c.chapter_id == run["chapter_id"]
                        )
                    ).first()
                )
                self._raise_materialization_revision_conflict(
                    expected_revision,
                    current.get("revision") if current else None,
                )
            return

        if stage_key == "4-storyboard":
            asset_versions = materialization.get("asset_versions", [])
            for snapshot in asset_versions:
                asset_id = str(snapshot.get("asset_id") or "")
                expected_revision = snapshot.get("revision")
                if not asset_id or type(expected_revision) is not int:
                    raise MaterializationConflict("阶段 4 asset revision 快照无效")
                result = connection.execute(
                    update(asset)
                    .where(
                        asset.c.id == asset_id,
                        asset.c.revision == expected_revision,
                    )
                    .values(revision=asset.c.revision)
                )
                if result.rowcount != 1:
                    current_revision = connection.scalar(
                        select(asset.c.revision).where(asset.c.id == asset_id)
                    )
                    self._raise_materialization_revision_conflict(
                        expected_revision,
                        current_revision,
                    )
            for item in materialization["plan"]:
                external_key = item.get("external_key")
                existing = connection.execute(
                    select(shot.c.id).where(
                        shot.c.chapter_id == run["chapter_id"],
                        shot.c.external_key == external_key,
                    )
                ).first()
                if existing is not None:
                    raise MaterializationConflict(f"章节已存在相同 external_key：{external_key}")
                values = {
                    "id": item["id"],
                    "project_id": run["project_id"],
                    "chapter_id": run["chapter_id"],
                    "shot_no": item["shot_no"],
                    "shot_order": item["shot_order"],
                    "source_type": "screenplay",
                    "source_ref": item.get("source_ref", ""),
                    "source_signature": source_sig,
                    "external_key": external_key,
                    "scene_asset_id": item.get("scene_asset_id"),
                    "character_ids_json": self._json_column(item.get("character_ids_json"), []),
                    "prop_ids_json": self._json_column(item.get("prop_ids_json"), []),
                    "positions_json": self._json_column(item.get("positions_json"), []),
                    "start_state": item.get("start_state", ""),
                    "action": item.get("action", ""),
                    "action_events_json": self._json_column(item.get("action_events_json"), []),
                    "expressions_json": self._json_column(item.get("expressions_json"), []),
                    "dialogue_json": self._json_column(item.get("dialogue_json"), []),
                    "dialogue_cues_json": self._json_column(item.get("dialogue_cues_json"), []),
                    "sound_cues_json": self._json_column(item.get("sound_cues_json"), []),
                    "narration": item.get("narration", ""),
                    "shot_type": item.get("shot_type", ""),
                    "camera_angle": item.get("camera_angle", ""),
                    "camera_move": item.get("camera_move", ""),
                    "duration_sec": item.get("duration_sec", 6),
                    "end_state": item.get("end_state", ""),
                    "next_hook": item.get("next_hook", ""),
                    "transition_from_prev": item.get("transition_from_prev", "scene_change"),
                    "continuity_json": self._json_column(item.get("continuity_json"), {}),
                    "pipeline_metadata_json": self._json_column(
                        item.get("pipeline_metadata_json"), {}
                    ),
                    "frame_description": item.get("frame_description", ""),
                    "image_prompt": item.get("image_prompt", ""),
                    "video_prompt": item.get("video_prompt", ""),
                    "negative_prompt": item.get("negative_prompt", ""),
                    "reference_bindings_json": self._json_column(
                        item.get("reference_bindings_json"), []
                    ),
                    "status": "confirmed",
                    "own_signature": item.get("own_signature", ""),
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                try:
                    connection.execute(insert(shot).values(**values))
                except IntegrityError as exc:
                    raise MaterializationConflict(
                        f"动态分镜写入冲突：external_key={external_key}"
                    ) from exc

    def _enqueue_after_commit(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        enqueue_ids: list[str] = []
        repository = getattr(self.job_bridge, "repository", None)
        for job_id in job_ids:
            if repository is None:
                enqueue_ids.append(job_id)
                continue
            record = repository.get(job_id)
            if record is None:
                continue
            input_data = record.get("input_data")
            input_data = input_data if isinstance(input_data, Mapping) else {}
            if (
                record.get("job_type") == "dynamic_stage_generate"
                and input_data.get("stage_key") == "6-delivery"
            ):
                dependencies = record.get("depends_on")
                dependencies = dependencies if isinstance(dependencies, list) else []
                if dependencies:
                    dependency_statuses = [
                        repository.get(str(dependency_id))
                        for dependency_id in dependencies
                    ]
                    if any(
                        dependency is None or dependency.get("status") != "completed"
                        for dependency in dependency_statuses
                    ):
                        # 交付顶层 Job 保持 queued，待最后一个媒体 Job 完成后由
                        # MediaService 的下游唤醒逻辑入队，避免提前进入 running。
                        continue
            enqueue_ids.append(job_id)
        if not enqueue_ids:
            return
        try:
            self.job_bridge.enqueue_after_commit(enqueue_ids)
        except Exception:
            # 入队失败时保留 queued 状态，由动态恢复流程后续补偿。
            return

    def _require_runner(self) -> None:
        enqueue = getattr(self.job_bridge, "enqueue", None)
        runner = getattr(enqueue, "__self__", None)
        if not callable(enqueue) or bool(getattr(runner, "_shutdown", False)):
            raise conflict("动态 Runner 未注入，不能创建 queued Job；请配置真实 JobRunner")

    def _rebuild_delivery_audio_children(
        self,
        connection: Any,
        *,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        delivery_job_id: str,
        new_revision: int,
    ) -> list[str]:
        """为恢复中的 delivery 创建新音频 Job，并切换顶层依赖。

        失败/中断子 Job 保留作审计记录，但不会被新 delivery 继续依赖。字幕 Job
        由恢复后的 delivery 在新音频完成后按新 generation 重建，避免沿用旧音频 ID。
        """
        top_row = connection.execute(select(job).where(job.c.id == delivery_job_id)).first()
        top = row_dict(top_row)
        if top is None:
            raise DynamicWorkflowRepositoryError("恢复 delivery Job 不存在")
        top_input = json_loads(top.get("input_json"), {})
        top_input = dict(top_input) if isinstance(top_input, Mapping) else {}
        source_generation = str(top_input.get("delivery_generation") or "").strip()
        generation = "resume-" + calculate_signature(
            "dynamic-delivery-resume-v1",
            run["id"],
            stage["id"],
            new_revision,
            delivery_job_id,
        )[:24]

        source_rows = connection.execute(
            select(job)
            .where(
                job.c.workflow_run_id == run["id"],
                job.c.workflow_stage_id == stage["id"],
                job.c.job_type == "generate_dialogue_audio",
            )
            .order_by(job.c.created_at, job.c.id)
        ).all()
        new_audio_ids: list[str] = []
        now = utc_now()
        for source_row in source_rows:
            source = row_dict(source_row) or {}
            source_input = json_loads(source.get("input_json"), {})
            source_input = dict(source_input) if isinstance(source_input, Mapping) else {}
            source_row_generation = str(source_input.get("delivery_generation") or "").strip()
            if source_row_generation != source_generation:
                continue
            source_key = str(source.get("idempotency_key") or "")
            base_key = source_key.split(":resume:", 1)[0]
            if not base_key:
                base_key = "dynamic-audio-resume:" + calculate_signature(
                    source.get("id"), source_input
                )
            new_key = f"{base_key}:resume:{generation}"
            new_input = copy.deepcopy(source_input)
            new_input.update(
                {
                    "delivery_generation": generation,
                    "workflow_run_id": run["id"],
                    "workflow_stage_id": stage["id"],
                    "workflow_stage_attempt": stage.get("attempt"),
                    "workflow_run_revision": new_revision,
                    "idempotency_key": new_key,
                }
            )
            resumed_signature = calculate_signature(
                "dynamic-audio-resume-v1",
                str(source.get("input_signature") or ""),
                generation,
            )
            new_input["input_signature"] = resumed_signature
            source_provenance = new_input.get("provenance")
            if isinstance(source_provenance, Mapping):
                source_provenance = dict(source_provenance)
                source_provenance["input_signature"] = resumed_signature
                source_provenance["delivery_generation"] = generation
                new_input["provenance"] = source_provenance
            child_id = new_id("job")
            connection.execute(
                insert(job).values(
                    id=child_id,
                    project_id=run["project_id"],
                    chapter_id=run["chapter_id"],
                    workflow_run_id=run["id"],
                    workflow_stage_id=stage["id"],
                    job_type="generate_dialogue_audio",
                    target_type=source.get("target_type") or "shot_audio",
                    target_id=source.get("target_id"),
                    batch_id=None,
                    idempotency_key=new_key,
                    provider_kind=source.get("provider_kind") or "tts",
                    provider_id=source.get("provider_id"),
                    provider_task_id="",
                    status="queued",
                    progress_current=0,
                    progress_total=int(source.get("progress_total") or 1),
                    message="",
                    input_json=json_dumps(new_input),
                    input_signature=resumed_signature,
                    depends_on_json=json_dumps([]),
                    result_json=json_dumps({}),
                    error="",
                    retry_count=0,
                    created_at=now,
                    updated_at=now,
                    started_at=None,
                    finished_at=None,
                )
            )
            new_audio_ids.append(child_id)

        prompt_stage_ids = [
            str(row.id)
            for stage_key in _PROMPT_STAGE_KEYS
            for row in [
                connection.execute(
                    select(workflow_stage.c.id)
                    .where(
                        workflow_stage.c.workflow_run_id == run["id"],
                        workflow_stage.c.stage_key == stage_key,
                    )
                    .order_by(
                        workflow_stage.c.attempt.desc(),
                        workflow_stage.c.updated_at.desc(),
                        workflow_stage.c.id.desc(),
                    )
                    .limit(1)
                ).first()
            ]
            if row is not None
        ]
        prompt_dependencies = self._delivery_dependency_ids(
            connection,
            str(run["id"]),
            prompt_stage_ids,
            str(stage["id"]),
        )
        dependency_ids = list(dict.fromkeys([*prompt_dependencies, *new_audio_ids]))
        top_input.update(
            {
                "delivery_generation": generation,
                "workflow_run_revision": new_revision,
                "workflow_stage_attempt": stage.get("attempt"),
                "depends_on": dependency_ids,
            }
        )
        updated = connection.execute(
            update(job)
            .where(
                job.c.id == delivery_job_id,
                job.c.workflow_run_id == run["id"],
                job.c.workflow_stage_id == stage["id"],
                job.c.input_signature == stage.get("input_signature"),
                job.c.status.in_(tuple(_ACTIVE_JOB_STATUSES)),
            )
            .values(
                depends_on_json=json_dumps(dependency_ids),
                input_json=json_dumps(top_input),
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            raise DynamicWorkflowRepositoryError("恢复 delivery Job 依赖条件更新未命中")
        return new_audio_ids

    def _get_run_record(self, run_id: str) -> dict[str, Any]:
        record: dict[str, Any] | None = None
        try:
            record = self.repository.get_run(run_id)
        except Exception as exc:
            self._raise_service_error(exc)
        if record is None:
            raise not_found("运行")
        return self._normalize_run(record)

    def _get_action_record(self, run_id: str) -> dict[str, Any]:
        record: dict[str, Any] | None = None
        try:
            record = self.repository.get_run_with_stages(run_id)
        except Exception as exc:
            self._raise_service_error(exc)
        if record is None:
            raise not_found("运行")
        return {
            "run": self._normalize_run(record["run"]),
            "stages": [self._normalize_stage(stage) for stage in record["stages"]],
        }

    def _get_free_project(self, project_id: str) -> dict[str, Any]:
        record: dict[str, Any] | None = None
        try:
            with self.database.engine.connect() as connection:
                record = row_dict(
                    connection.execute(select(project).where(project.c.id == project_id)).first()
                )
        except Exception as exc:
            self._raise_service_error(exc)
        if record is None:
            raise not_found("项目")
        if not bool(record.get("is_free_create")):
            raise bad_request("动态运行只能创建于 free-create 项目")
        return record

    @staticmethod
    def _default_provider_ids(connection: Any) -> dict[str, str]:
        defaults: dict[str, str] = {}
        for kind in ("text", "image", "video"):
            provider_id = connection.execute(
                select(api_provider.c.id)
                .where(
                    api_provider.c.kind == kind,
                    api_provider.c.enabled == 1,
                    api_provider.c.is_default == 1,
                )
                .order_by(api_provider.c.id)
                .limit(1)
            ).scalar()
            if not provider_id:
                raise bad_request(f"缺少已启用的 {kind} 默认 Provider")
            defaults[f"default_{kind}_provider_id"] = str(provider_id)
        return defaults

    def _prompt_provider_snapshot(
        self,
        connection: Any,
        project_id: str,
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        run = {"project_id": project_id}
        for stage_key, id_key, model_key in (
            ("5a-image-prompts", "image_provider_id", "image_provider_model"),
            ("5b-video-prompts", "video_provider_id", "video_provider_model"),
        ):
            provider_id, provider_model = self._default_prompt_provider(
                run,
                stage_key,
                connection=connection,
            )
            if provider_id:
                values[id_key] = provider_id
            if provider_model:
                values[model_key] = provider_model
        return values

    @staticmethod
    def _prompt_stage_channel(stage_key: str) -> str:
        return "image" if stage_key == "5a-image-prompts" else "video"

    @staticmethod
    def _snapshot_text(
        value: Any,
        names: tuple[str, ...],
        *,
        model_value: bool = False,
    ) -> str:
        if not isinstance(value, Mapping):
            return ""
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, Mapping):
                candidate_names = (
                    ("model", "provider_model") if model_value else ("id", "provider_id")
                )
                candidate = next(
                    (candidate.get(key) for key in candidate_names if candidate.get(key)),
                    "",
                )
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @classmethod
    def _prompt_provider_sources(
        cls,
        value: Any,
        stage_key: str,
    ) -> list[Mapping[str, Any]]:
        if not isinstance(value, Mapping):
            return []
        channel = cls._prompt_stage_channel(stage_key)
        sources: list[Mapping[str, Any]] = [value]
        for key in (
            stage_key,
            channel,
            f"{stage_key}_provider",
            f"{channel}_provider",
            "providers",
            "provider_snapshot",
            "provider_capabilities",
            "provider_capability_snapshot",
            "provenance",
            "artifact_meta",
            "provider",
        ):
            nested = value.get(key)
            if not isinstance(nested, Mapping):
                continue
            sources.append(nested)
            for nested_key in (stage_key, channel):
                selected = nested.get(nested_key)
                if isinstance(selected, Mapping):
                    sources.append(selected)
        return sources

    @classmethod
    def _prompt_provider_pair(
        cls,
        value: Any,
        stage_key: str,
    ) -> tuple[str, str]:
        channel = cls._prompt_stage_channel(stage_key)
        provider_id = ""
        provider_model = ""
        id_names = (
            f"{stage_key}_provider_id",
            f"{channel}_provider_id",
            f"{stage_key}_provider",
            f"{channel}_provider",
            f"default_{channel}_provider_id",
            "provider_id",
            "default_provider_id",
            "selected_provider_id",
            "id",
        )
        model_names = (
            f"{stage_key}_provider_model",
            f"{channel}_provider_model",
            f"{stage_key}_model",
            f"{channel}_model",
            f"default_{channel}_provider_model",
            f"default_{channel}_model",
            "provider_model",
            "model",
        )
        for source in cls._prompt_provider_sources(value, stage_key):
            provider_id = provider_id or cls._snapshot_text(source, id_names)
            provider_model = provider_model or cls._snapshot_text(
                source,
                model_names,
                model_value=True,
            )
            if provider_id and provider_model:
                break
        return provider_id, provider_model

    def _default_prompt_provider(
        self,
        run: Mapping[str, Any],
        stage_key: str,
        *,
        connection: Any | None = None,
    ) -> tuple[str, str]:
        channel = self._prompt_stage_channel(stage_key)
        provider_column = (
            project.c.default_image_provider_id
            if channel == "image"
            else project.c.default_video_provider_id
        )
        project_id = str(run.get("project_id") or "").strip()
        if not project_id:
            return "", ""

        owns_connection = connection is None
        current_connection = connection or self.database.engine.connect()
        try:
            selected_id = current_connection.scalar(
                select(provider_column).where(project.c.id == project_id)
            )
            provider_query = select(api_provider.c.id, api_provider.c.model).where(
                api_provider.c.kind == channel,
                api_provider.c.enabled == 1,
            )
            if selected_id:
                provider_query = provider_query.where(api_provider.c.id == selected_id)
            else:
                provider_query = provider_query.where(api_provider.c.is_default == 1)
            provider_record = current_connection.execute(
                provider_query.order_by(api_provider.c.id).limit(1)
            ).first()
            if provider_record is None:
                return "", ""
            return str(provider_record.id or ""), str(provider_record.model or "")
        finally:
            if owns_connection:
                current_connection.close()

    def _prompt_provider_config(
        self,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        stage_key: str,
        *,
        connection: Any | None = None,
    ) -> tuple[str, str]:
        config = run.get("config_snapshot")
        if not isinstance(config, Mapping):
            config = json_loads(run.get("config_snapshot_json"), {})

        provider_id = ""
        provider_model = ""
        provider_id_from_config, provider_model_from_config = self._prompt_provider_pair(
            config, stage_key
        )
        provider_id = provider_id_from_config
        provider_model = provider_model_from_config

        content = self._stage_content(stage)
        if isinstance(content, Mapping):
            candidates: list[Any] = [content]
            for key in ("content", "stage_output", "compiled_output", "data", "output"):
                nested = content.get(key)
                if isinstance(nested, Mapping):
                    candidates.append(nested)
            bundles = content.get("bundles")
            if isinstance(bundles, Mapping):
                candidates.extend(bundles[key] for key in sorted(bundles, key=str))
            for candidate in candidates:
                candidate_id, candidate_model = self._prompt_provider_pair(candidate, stage_key)
                provider_id = provider_id or candidate_id
                provider_model = provider_model or candidate_model
                if provider_id and provider_model:
                    break

        default_id, default_model = self._default_prompt_provider(
            run,
            stage_key,
            connection=connection,
        )
        if not provider_id or provider_id.startswith("dynamic-default-"):
            provider_id = default_id or provider_id
        if not provider_model or provider_model.startswith("dynamic-default-"):
            provider_model = default_model or provider_model
        return provider_id, provider_model

    def _validate_prompt_media_prerequisites(
        self,
        connection: Any,
        run: Mapping[str, Any],
        stage_key: str,
        provider_id: str,
        provider_model: str,
    ) -> None:
        queue = self.media_queue_service
        enqueue = getattr(queue, "enqueue", None)
        if not callable(enqueue):
            delegate = getattr(queue, "delegate", None)
            enqueue = getattr(delegate, "enqueue", None)
        if not callable(enqueue):
            raise conflict(
                "动态媒体 Runner 未注入，不能确认 Prompt 阶段；"
                "请配置与 DynamicJobBridge 相同的真实 Runner"
            )
        if not provider_id:
            raise conflict(
                f"阶段 {stage_key} 缺少可用的 {self._prompt_stage_channel(stage_key)} Provider"
            )
        if not provider_model:
            raise conflict(f"Provider {provider_id} 缺少 model，不能确认阶段 {stage_key}")
        provider = row_dict(
            connection.execute(
                select(api_provider).where(
                    api_provider.c.id == provider_id,
                    api_provider.c.kind == self._prompt_stage_channel(stage_key),
                    api_provider.c.enabled == 1,
                )
            ).first()
        )
        if provider is None:
            raise conflict(
                f"Provider {provider_id} 不存在、未启用或类型不匹配，不能确认阶段 {stage_key}"
            )

    def _record_prompt_queue_warning(
        self,
        run_id: str,
        stage_id: str,
        stage_key: str,
        *,
        code: str,
    ) -> None:
        message = f"阶段 {stage_key} 已确认，但媒体队列未完成；可重试媒体入队。"
        warning_ref = {
            "capture_mode": "dynamic_media_queue_warning",
            "code": code,
            "message": message,
            "status": "warning",
            "workflow_run_id": run_id,
            "workflow_stage_id": stage_id,
            "stage_key": stage_key,
        }
        try:
            with self.database.engine.begin() as connection:
                stage = row_dict(
                    connection.execute(
                        select(workflow_stage).where(
                            workflow_stage.c.id == stage_id,
                            workflow_stage.c.workflow_run_id == run_id,
                        )
                    ).first()
                )
                if stage is None or stage.get("status") != "confirmed":
                    return
                output_refs = json_loads(stage.get("output_refs_json"), [])
                output_refs = output_refs if isinstance(output_refs, list) else []
                if warning_ref not in output_refs:
                    output_refs.append(warning_ref)
                connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                    .values(
                        error=message,
                        output_refs_json=json_dumps(output_refs),
                        updated_at=utc_now(),
                    )
                )
        except Exception:
            # 确认事务已经提交，warning 记录失败不能反向影响阶段确认结果。
            return

    def _clear_prompt_queue_warning(self, run_id: str, stage_id: str) -> None:
        try:
            with self.database.engine.begin() as connection:
                stage = row_dict(
                    connection.execute(
                        select(workflow_stage).where(
                            workflow_stage.c.id == stage_id,
                            workflow_stage.c.workflow_run_id == run_id,
                        )
                    ).first()
                )
                if (
                    stage is None
                    or not str(stage.get("error") or "").startswith("阶段 ")
                    or "媒体队列未完成" not in str(stage.get("error") or "")
                ):
                    return
                connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                    .values(error="", updated_at=utc_now())
                )
        except Exception:
            return

    @staticmethod
    def _next_chapter_no(connection: Any, project_id: str) -> int:
        current = connection.execute(
            select(func.max(chapter.c.chapter_no)).where(chapter.c.project_id == project_id)
        ).scalar()
        return int(current or 0) + 1

    def create_run(
        self,
        payload: WorkflowRunCreate,
        project_id_path: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, WorkflowRunCreate):
            try:
                payload = WorkflowRunCreate.model_validate(payload)
            except (TypeError, ValueError) as exc:
                self._raise_service_error(exc)
        if payload.auto_run:
            self._require_runner()

        body_project_id = payload.project_id
        if body_project_id is not None:
            body_project_id = body_project_id.strip()
            if not body_project_id:
                raise bad_request("project_id 不能为空")
        path_project_id = project_id_path.strip() if project_id_path is not None else None
        if path_project_id == "":
            raise bad_request("项目 ID 不能为空")
        if body_project_id and path_project_id and body_project_id != path_project_id:
            raise bad_request("请求体 project_id 与路径 project_id 不一致")

        selected_project_id = path_project_id or body_project_id
        source_kind = "full_text" if payload.source_text else "synopsis"
        source_text = payload.source_text or payload.synopsis or ""
        source_snapshot = {
            "source_kind": source_kind,
            "text": source_text,
            "title": payload.title,
            "chapter_title": payload.chapter_title,
        }
        config_snapshot = {
            "target_duration_sec": payload.target_duration_sec,
            "strong_dynamic_max_events": payload.strong_dynamic_max_events,
            "aspect_ratio": payload.aspect_ratio,
            "style_prompt": payload.style_prompt,
            "auto_run": payload.auto_run,
            "generate_audio": payload.generate_audio,
            "generate_subtitle": payload.generate_subtitle,
            "generate_bgm": payload.generate_bgm,
            "generate_composition": payload.generate_composition,
            "reuse_approved_assets": payload.reuse_approved_assets,
        }
        input_signature = calculate_signature(source_snapshot, config_snapshot)
        project_id = selected_project_id or new_id("project")
        chapter_id = new_id("chapter")
        run_id = new_id("run")
        created_project_target = None
        job_ids: list[str] = []
        initial_status = "queued" if payload.auto_run else "pending"

        try:
            storage_name = ""
            storage_root = ""
            if selected_project_id is None:
                storage_path, storage_name = self.storage.create_categorized_project(payload.title)
                created_project_target = storage_path
                storage_root = str(storage_path.parent)

            now = utc_now()
            with self.database.engine.begin() as connection:
                if selected_project_id is None:
                    provider_defaults = self._default_provider_ids(connection)
                    project_values = {
                        "id": project_id,
                        "name": payload.title,
                        "storage_name": storage_name,
                        "storage_root": storage_root,
                        "storage_layout": "categorized",
                        "source_text": "",
                        "source_signature": source_signature(""),
                        "source_revision": 1,
                        "style_prompt": payload.style_prompt,
                        "aspect_ratio": payload.aspect_ratio,
                        "image_size": "1920x1080",
                        "video_size": "16:9",
                        "default_duration_sec": 6,
                        **provider_defaults,
                        "is_free_create": 1,
                        "created_at": now,
                        "updated_at": now,
                    }
                    connection.execute(insert(project).values(**project_values))
                else:
                    existing_project = row_dict(
                        connection.execute(
                            select(project).where(project.c.id == selected_project_id)
                        ).first()
                    )
                    if existing_project is None:
                        raise not_found("项目")
                    if not bool(existing_project.get("is_free_create")):
                        raise bad_request("动态运行只能创建于 free-create 项目")

                config_snapshot = {
                    **config_snapshot,
                    **{
                        key: value
                        for key, value in self._prompt_provider_snapshot(
                            connection,
                            project_id,
                        ).items()
                        if value
                    },
                }
                input_signature = calculate_signature(source_snapshot, config_snapshot)

                connection.execute(
                    insert(chapter).values(
                        id=chapter_id,
                        project_id=project_id,
                        chapter_no=self._next_chapter_no(connection, project_id),
                        title=payload.chapter_title,
                        source_text=source_text,
                        source_signature=source_signature(source_text),
                        source_revision=1,
                        status="draft",
                        created_at=now,
                        updated_at=now,
                    )
                )

                stage_values = [
                    {
                        "id": new_id("stage"),
                        "stage_key": stage_key,
                        "ordinal": get_stage_ordinal(stage_key),
                        "status": (
                            initial_status
                            if stage_key == "0-source" and payload.auto_run
                            else "pending"
                        ),
                        "input_signature": input_signature,
                        "raw_output": {},
                        "normalized_output": {},
                        "draft_output": {},
                        "output_refs": [],
                        "gate_result": {},
                        "attempt": 1,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for stage_key in STAGE_KEYS
                ]
                self.repository.create_run_with_stages(
                    connection,
                    {
                        "id": run_id,
                        "project_id": project_id,
                        "chapter_id": chapter_id,
                        "kind": "dynamic_comic_0_6",
                        "schema_version": "1",
                        "status": initial_status,
                        "current_stage": "0-source",
                        "source_kind": source_kind,
                        "source_snapshot": source_snapshot,
                        "config_snapshot": config_snapshot,
                        "asset_mapping": [],
                        "input_signature": input_signature,
                        "revision": 1,
                        "error": "",
                        "created_at": now,
                        "updated_at": now,
                    },
                    stage_values,
                )
                if payload.auto_run:
                    reference = self.job_bridge.ensure_stage_job_in_transaction(
                        connection,
                        {
                            "id": run_id,
                            "project_id": project_id,
                            "chapter_id": chapter_id,
                            "status": initial_status,
                            "current_stage": "0-source",
                            "input_signature": input_signature,
                        },
                        {
                            **stage_values[0],
                            "workflow_run_id": run_id,
                            "status": initial_status,
                        },
                    )
                    if reference.status == "queued":
                        job_ids.append(reference.job_id)
        except Exception as exc:
            if created_project_target is not None:
                try:
                    self.storage.delete_reserved_project(
                        created_project_target.parent,
                        created_project_target,
                    )
                except Exception as cleanup_exc:
                    raise bad_request(
                        f"动态项目创建失败，且项目目录清理失败：{sanitize_error_message(cleanup_exc)}"
                    ) from exc
            self._raise_service_error(exc)

        self._enqueue_after_commit(job_ids)
        return self._get_action_record(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get_run_record(run_id)

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        self._get_free_project(project_id)
        records: list[dict[str, Any]] = []
        try:
            records = self.repository.list_runs(project_id)
        except Exception as exc:
            self._raise_service_error(exc)
        return [self._normalize_run(record) for record in records]

    def get_run_with_stages(self, run_id: str) -> dict[str, Any]:
        return self._get_action_record(run_id)

    def get_stage(self, run_id: str, stage_key: str) -> dict[str, Any]:
        self._validate_stage_key(stage_key)
        self._get_run_record(run_id)
        record: dict[str, Any] | None = None
        try:
            record = self.repository.get_stage(run_id, stage_key)
        except Exception as exc:
            self._raise_service_error(exc)
        if record is None:
            raise not_found("阶段")
        return self._normalize_stage(record)

    def start_run(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        expected_revision = self._revision_value(expected_revision)
        self._require_runner()
        job_ids: list[str] = []
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                if run.get("status") not in {"pending", "ready", "stale"}:
                    raise conflict(f"运行当前状态为 {run.get('status')!r}，不能启动")
                stage = self._stage_from_connection(connection, run_id, "0-source")
                if stage is None:
                    raise not_found("阶段")
                ensure_stage_prerequisites_confirmed("0-source", self._confirmed_keys({}))
                if stage["status"] in {"queued", "running", "confirmed", "cancelled"}:
                    raise conflict(f"阶段 0 当前状态为 {stage['status']!r}，不能重复启动")
                if stage["status"] not in {"pending", "ready", "stale"}:
                    raise conflict(f"阶段 0 当前状态为 {stage['status']!r}，不能启动")
                try:
                    self._queue_status(str(run["status"]))
                    self._queue_status(str(stage["status"]))
                except ValueError as exc:
                    raise conflict(str(exc)) from exc
                new_revision = self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {"status": "queued", "current_stage": "0-source"},
                )
                updated_run = {
                    **run,
                    "status": "queued",
                    "current_stage": "0-source",
                    "revision": new_revision,
                }
                updated_stage = {**stage, "status": "queued"}
                self._update_stage(
                    connection,
                    stage["id"],
                    {"status": "queued", "updated_at": utc_now(), "finished_at": None},
                )
                reference = self.job_bridge.ensure_stage_job_in_transaction(
                    connection,
                    updated_run,
                    updated_stage,
                    retry_failed=False,
                )
                job_ids.append(reference.job_id)
        except Exception as exc:
            self._raise_service_error(exc)
        self._enqueue_after_commit(job_ids)
        return self._get_action_record(run_id)

    def pause_run(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        expected_revision = self._revision_value(expected_revision)
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                current_status = str(run.get("status"))
                if current_status not in {"pending", "queued", "running"}:
                    raise conflict(f"运行当前状态为 {current_status!r}，不能暂停")
                transition_status(current_status, "blocked")
                current_stage = run.get("current_stage") or "0-source"
                stage = self._stage_from_connection(connection, run_id, current_stage)
                if stage is None:
                    raise not_found("阶段")
                new_values: dict[str, Any] = {
                    "status": "blocked",
                    "current_stage": current_stage,
                }
                self._bump_run(connection, run_id, expected_revision, new_values)
                stages_to_pause = [stage]
                if current_stage in _PROMPT_STAGE_KEYS:
                    stages_to_pause = []
                    for candidate_key in _PROMPT_STAGE_KEYS:
                        candidate = self._stage_from_connection(connection, run_id, candidate_key)
                        if candidate is not None:
                            stages_to_pause.append(candidate)
                for candidate in stages_to_pause:
                    if candidate["status"] in {"pending", "queued", "running"}:
                        transition_status(candidate["status"], "blocked")
                        self._update_stage(
                            connection,
                            candidate["id"],
                            {"status": "blocked", "updated_at": utc_now()},
                        )
                    # 同时中断在途 Job：暂停/取消后后台不应继续消耗 Provider 配额并写文件。
                    # Job 在 runner/handler 的执行循环中会按 interrupted 状态尽快停止。
                    self._interrupt_active_stage_jobs(connection, candidate["id"], utc_now())
        except Exception as exc:
            self._raise_service_error(exc)
        return self._get_action_record(run_id)

    def cancel_run(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        expected_revision = self._revision_value(expected_revision)
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                current_status = str(run.get("status"))
                if current_status == "cancelled":
                    raise conflict("运行已经取消")
                if not can_transition_status(current_status, "cancelled"):
                    raise conflict(f"运行当前状态为 {current_status!r}，不能取消")
                now = utc_now()
                self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {"status": "cancelled", "finished_at": now},
                )
                connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.status.in_(_CANCELLABLE_STAGE_STATUSES),
                    )
                    .values(status="cancelled", updated_at=now, finished_at=now)
                )
                # 取消语义应真正停止后台执行：中断该 run 下所有在途 Job，
                # 避免已发出的 Provider 调用与文件写出继续消耗资源。
                connection.execute(
                    update(job)
                    .where(
                        job.c.workflow_run_id == run_id,
                        job.c.status.in_(_ACTIVE_JOB_STATUSES),
                    )
                    .values(
                        status="interrupted",
                        message="任务因运行取消而中断",
                        error="运行已取消，任务停止执行",
                        finished_at=now,
                        updated_at=now,
                    )
                )
        except Exception as exc:
            self._raise_service_error(exc)
        return self._get_action_record(run_id)

    def resume_run(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        expected_revision = self._revision_value(expected_revision)
        self._require_runner()
        job_ids: list[str] = []
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                current_status = str(run.get("status"))
                if current_status == "cancelled":
                    raise conflict("已取消的运行不能恢复")
                if current_status not in _RESUMABLE_RUN_STATUSES:
                    raise conflict(f"运行当前状态为 {current_status!r}，不能恢复")
                stages = self._stages_from_connection(connection, run_id)
                confirmed = set(self._confirmed_keys(stages))
                current_stage = run.get("current_stage") or "0-source"
                candidates = [current_stage, *STAGE_KEYS]
                target_stage: dict[str, Any] | None = None
                target_key = ""
                seen_candidates: set[str] = set()
                for candidate_key in candidates:
                    if candidate_key in seen_candidates:
                        continue
                    seen_candidates.add(candidate_key)
                    candidate = stages.get(candidate_key)
                    if candidate is None or candidate["status"] not in _RESUMABLE_STAGE_STATUSES:
                        continue
                    if candidate["status"] == "ready":
                        continue
                    try:
                        ensure_stage_prerequisites_confirmed(candidate_key, confirmed)
                    except StageBlocked:
                        continue
                    target_stage = candidate
                    target_key = candidate_key
                    break
                if target_stage is None:
                    raise conflict("没有可恢复的未确认阶段")
                target_stages = [target_stage]
                if target_key in _PROMPT_STAGE_KEYS:
                    target_stages = [
                        candidate
                        for candidate_key in _PROMPT_STAGE_KEYS
                        if (candidate := stages.get(candidate_key)) is not None
                        and candidate["status"] in _RESUMABLE_STAGE_STATUSES
                    ]
                    target_stages.sort(
                        key=lambda candidate: STAGE_KEYS.index(candidate["stage_key"])
                    )
                try:
                    self._queue_status(current_status)
                    for candidate in target_stages:
                        self._queue_status(str(candidate["status"]))
                except ValueError as exc:
                    raise conflict(str(exc)) from exc
                for candidate in target_stages:
                    ensure_stage_prerequisites_confirmed(candidate["stage_key"], confirmed)
                new_revision = self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {
                        "status": "queued",
                        "current_stage": self._current_stage_for(target_key),
                        "error": "",
                    },
                )
                updated_run = {
                    **run,
                    "status": "queued",
                    "current_stage": self._current_stage_for(target_key),
                    "revision": new_revision,
                    "error": "",
                }
                for candidate in target_stages:
                    updated_stage = {**candidate, "status": "queued"}
                    self._update_stage(
                        connection,
                        candidate["id"],
                        {
                            "status": "queued",
                            "updated_at": utc_now(),
                            "finished_at": None,
                            "error": "",
                        },
                    )
                    reference = self.job_bridge.ensure_stage_job_in_transaction(
                        connection,
                        updated_run,
                        updated_stage,
                        retry_failed=True,
                    )
                    if candidate["stage_key"] == "6-delivery":
                        job_ids.extend(
                            self._rebuild_delivery_audio_children(
                                connection,
                                run=updated_run,
                                stage=updated_stage,
                                delivery_job_id=reference.job_id,
                                new_revision=new_revision,
                            )
                        )
                    if reference.status == "queued":
                        job_ids.append(reference.job_id)
        except Exception as exc:
            self._raise_service_error(exc)
        self._enqueue_after_commit(job_ids)
        return self._get_action_record(run_id)

    def save_stage_draft(
        self,
        run_id: str,
        stage_key: str,
        payload: StageDraftUpdate,
    ) -> dict[str, Any]:
        self._validate_stage_key(stage_key)
        if not isinstance(payload, StageDraftUpdate):
            try:
                payload = StageDraftUpdate.model_validate(payload)
            except (TypeError, ValueError) as exc:
                self._raise_service_error(exc)
        expected_revision = self._revision_value(payload.expected_revision)
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                stage = self._stage_from_connection(connection, run_id, stage_key)
                if stage is None:
                    raise not_found("阶段")
                if stage["status"] == "cancelled":
                    raise conflict("已取消的阶段不能保存草稿")
                if stage["status"] in {"queued", "running"}:
                    # 阶段正在生成：保存草稿意味着用户放弃在途生成结果。
                    # 显式中断该阶段的在途 Job（而非静默让 stage_service 在下次
                    # 执行时因状态变为 ready/blocked 丢弃结果），并同步 run/stage
                    # 状态，保证「保存草稿 → 在途生成停止」语义明确、可观测。
                    self._interrupt_active_stage_jobs(connection, stage["id"], utc_now())
                stages = self._stages_from_connection(connection, run_id)
                draft_output = self._model_dump(payload.draft_output)
                if stage_key == "2-rhythm":
                    draft_output = server_bound_rhythm_content(
                        draft_output,
                        self._run_config_snapshot(run),
                    )
                normalized_output = (
                    draft_output
                    if stage_key == "2-rhythm"
                    else stage.get("normalized_output", {})
                )
                candidate_stage = {**stage, "draft_output": draft_output}
                server_gate = self._recalculate_stage_gate(
                    stage_key,
                    candidate_stage,
                    run,
                    {**stages, stage_key: candidate_stage},
                )
                values: dict[str, Any] = {
                    "draft_output_json": json_dumps(draft_output),
                    "gate_result_json": json_dumps(self._model_dump(server_gate)),
                    "updated_at": utc_now(),
                }
                if stage_key == "2-rhythm":
                    values["normalized_output_json"] = json_dumps(normalized_output)
                if stage["status"] == "confirmed":
                    values["status"] = "stale"
                else:
                    values["status"] = "ready" if server_gate.can_confirm else "blocked"
                self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {"current_stage": self._current_stage_for(stage_key)},
                )
                self._update_stage(connection, stage["id"], values)
                self.repository.mark_descendants_stale(connection, run_id, stage_key)
        except Exception as exc:
            self._raise_service_error(exc)
        return self._get_action_record(run_id)

    def confirm_stage(
        self,
        run_id: str,
        stage_key: str,
        payload: StageConfirmRequest,
    ) -> dict[str, Any]:
        self._validate_stage_key(stage_key)
        if not isinstance(payload, StageConfirmRequest):
            try:
                payload = StageConfirmRequest.model_validate(payload)
            except (TypeError, ValueError) as exc:
                self._raise_service_error(exc)
        expected_revision = self._revision_value(payload.expected_revision)
        self._require_runner()
        job_ids: list[str] = []
        gate_rejected = False
        prompt_queue_request: tuple[str, str, str, str, int] | None = None
        media_queue_service = self.media_queue_service
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                if run.get("status") == "cancelled":
                    raise conflict("已取消的运行不能确认阶段")
                stage = self._stage_from_connection(connection, run_id, stage_key)
                if stage is None:
                    raise not_found("阶段")
                stages = self._stages_from_connection(connection, run_id)
                confirmed_keys = self._confirmed_keys(stages)
                ensure_stage_prerequisites_confirmed(stage_key, confirmed_keys)

                if stage_key == "2-rhythm":
                    rhythm_content = self._phase2_content(stage)
                    bounded_rhythm = server_bound_rhythm_content(
                        rhythm_content,
                        self._run_config_snapshot(run),
                    )
                    stage_values = {
                        "draft_output_json": json_dumps(bounded_rhythm),
                        "normalized_output_json": json_dumps(bounded_rhythm),
                    }
                    self._update_stage(connection, stage["id"], stage_values)
                    stage = {
                        **stage,
                        "draft_output": bounded_rhythm,
                        "normalized_output": bounded_rhythm,
                    }
                    stages[stage_key] = stage

                recalculated_gate = None
                if stage_key in _PHASE2_STAGE_KEYS:
                    recalculated_gate = self._recalculate_phase2_gate(stage_key, stage, run)
                elif stage_key == "3-script":
                    recalculated_gate = self._recalculate_script_gate(stage, run, stages)
                elif stage_key in _PROMPT_STAGE_KEYS:
                    recalculated_gate = self._recalculate_prompt_gate(stage_key, stage, run)
                gate_for_confirmation = recalculated_gate or stage.get("gate_result")
                if recalculated_gate is not None:
                    gate_payload = self._model_dump(recalculated_gate)
                    now = utc_now()
                    stage_values: dict[str, Any] = {
                        "gate_result_json": json_dumps(gate_payload),
                        "updated_at": now,
                    }
                    if not can_confirm_stage(stage["status"], gate_payload):
                        if stage_key not in _PROMPT_STAGE_KEYS or stage["status"] == "ready":
                            self._bump_run(
                                connection,
                                run_id,
                                expected_revision,
                                {"current_stage": self._current_stage_for(stage_key)},
                            )
                        self._update_stage(connection, stage["id"], stage_values)
                        gate_rejected = True
                    else:
                        # 门禁快照和确认/屏障 revision 必须在同一个事务内落盘。
                        gate_values = {"gate_result_json": json_dumps(gate_payload)}
                        self._update_stage(connection, stage["id"], gate_values)
                if gate_rejected:
                    pass
                elif not can_confirm_stage(stage["status"], gate_for_confirmation):
                    raise conflict(f"阶段 {stage_key} 尚未满足确认门禁")
                elif stage_key in _PROMPT_STAGE_KEYS:
                    gate_status = (
                        gate_for_confirmation.get("status")
                        if isinstance(gate_for_confirmation, Mapping)
                        else getattr(gate_for_confirmation, "status", None)
                    )
                    if gate_status not in {"ready", "warning"}:
                        raise conflict(f"阶段 {stage_key} 尚未满足确认门禁")

                if not gate_rejected:
                    materialization = self._prepare_dynamic_materialization(
                        connection,
                        run,
                        stage,
                        stages,
                        stage_key,
                    )
                    confirmed_after = set(confirmed_keys)
                    confirmed_after.add(stage_key)
                    current_stage = self._next_current_stage(stage_key, confirmed_after)
                    now = utc_now()
                    run_values: dict[str, Any] = {"current_stage": current_stage}
                    if stage_key == "4-storyboard" and run.get("status") not in {
                        "cancelled",
                        "confirmed",
                    }:
                        run_values["status"] = "queued"
                    if current_stage == "6-delivery":
                        run_status = str(run.get("status") or "")
                        if run_status not in {"queued", "running"}:
                            try:
                                self._queue_status(run_status)
                            except ValueError as exc:
                                raise conflict(str(exc)) from exc
                            run_values["status"] = "queued"
                    if materialization is not None and stage_key == "1-character-bios":
                        run_values["asset_mapping_json"] = json_dumps(materialization["mapping"])
                    new_revision = self._bump_run(
                        connection,
                        run_id,
                        expected_revision,
                        run_values,
                    )
                    self._update_stage(
                        connection,
                        stage["id"],
                        {
                            "status": "confirmed",
                            "updated_at": now,
                            "finished_at": now,
                            "confirmed_at": now,
                            "confirmed_revision": new_revision,
                        },
                    )
                    if materialization is not None:
                        self._apply_dynamic_materialization(connection, run, materialization)
                    if media_queue_service is not None and stage_key in _PROMPT_STAGE_KEYS:
                        provider_id, provider_model = self._prompt_provider_config(
                            run,
                            stage,
                            stage_key,
                            connection=connection,
                        )
                        self._validate_prompt_media_prerequisites(
                            connection,
                            run,
                            stage_key,
                            provider_id,
                            provider_model,
                        )
                        prompt_queue_request = (
                            run_id,
                            stage["id"],
                            provider_id,
                            provider_model,
                            new_revision,
                        )
                    if current_stage == "6-delivery":
                        delivery_stage = self._stage_from_connection(
                            connection,
                            run_id,
                            "6-delivery",
                        )
                        if delivery_stage is None:
                            raise not_found("阶段")
                        delivery_signature = self._delivery_stage_input_signature(
                            run,
                            {
                                **stages,
                                stage_key: {
                                    **stage,
                                    "status": "confirmed",
                                    "confirmed_revision": new_revision,
                                },
                            },
                        )
                        delivery_status = str(delivery_stage.get("status") or "")
                        same_input = delivery_stage.get("input_signature") == delivery_signature
                        if delivery_status != "running" or not same_input:
                            if not same_input:
                                self._interrupt_active_stage_jobs(
                                    connection,
                                    delivery_stage["id"],
                                    now,
                                )
                            if delivery_status != "queued":
                                try:
                                    self._queue_status(delivery_status)
                                except ValueError as exc:
                                    raise conflict(str(exc)) from exc
                            self._update_stage(
                                connection,
                                delivery_stage["id"],
                                {
                                    "status": "queued",
                                    "input_signature": delivery_signature,
                                    "updated_at": now,
                                    "started_at": None,
                                    "finished_at": None,
                                    "error": "",
                                },
                            )
                            delivery_stage = {
                                **delivery_stage,
                                "status": "queued",
                                "input_signature": delivery_signature,
                            }
                        else:
                            delivery_stage = {
                                **delivery_stage,
                                "input_signature": delivery_signature,
                            }
                        delivery_run_status = run_values.get("status", run.get("status"))
                        delivery_run = {
                            **run,
                            "status": delivery_run_status,
                            "current_stage": current_stage,
                            "revision": new_revision,
                        }
                        reference = self.job_bridge.ensure_stage_job_in_transaction(
                            connection,
                            delivery_run,
                            delivery_stage,
                            retry_failed=True,
                        )
                        dependency_ids = self._delivery_dependency_ids(
                            connection,
                            run_id,
                            [
                                str(stages[key]["id"])
                                for key in _PROMPT_STAGE_KEYS
                                if stages.get(key) and stages[key].get("id")
                            ],
                            str(delivery_stage["id"]),
                        )
                        delivery_job = row_dict(
                            connection.execute(
                                select(job).where(job.c.id == reference.job_id)
                            ).first()
                        )
                        delivery_input = json_loads(
                            delivery_job.get("input_json") if delivery_job else None,
                            {},
                        )
                        delivery_input = (
                            dict(delivery_input) if isinstance(delivery_input, Mapping) else {}
                        )
                        delivery_input.update(
                            {
                                "stage_key": "6-delivery",
                                "depends_on": dependency_ids,
                                "workflow_run_revision": new_revision,
                                "workflow_stage_input_signature": delivery_signature,
                            }
                        )
                        connection.execute(
                            update(job)
                            .where(
                                job.c.id == reference.job_id,
                                job.c.workflow_run_id == run_id,
                                job.c.workflow_stage_id == delivery_stage["id"],
                                job.c.input_signature == delivery_signature,
                            )
                            .values(
                                depends_on_json=json_dumps(dependency_ids),
                                input_json=json_dumps(delivery_input),
                                updated_at=now,
                            )
                        )
                        if reference.status == "queued":
                            job_ids.append(reference.job_id)

                    if stage_key == "4-storyboard":
                        updated_run = {
                            **run,
                            "status": run_values.get("status", run.get("status")),
                            "current_stage": current_stage,
                            "revision": new_revision,
                        }
                        job_ids.extend(
                            self._prepare_prompt_stage_barrier(
                                connection,
                                updated_run,
                                {**stage, "status": "confirmed"},
                                stages,
                            )
                        )

                    # Phase 2 只在服务端重新计算过产物门禁后推进屏障；旧 API
                    # 的摘要草稿继续沿用原有确认语义，不凭空创建下游 Job。
                    if recalculated_gate is not None and stage_key in _PHASE2_STAGE_KEYS:
                        if stage_key == "2-rhythm":
                            if run.get("status") in {"running", "queued", "blocked", "ready"}:
                                connection.execute(
                                    update(workflow_run)
                                    .where(
                                        workflow_run.c.id == run_id,
                                        workflow_run.c.revision == new_revision,
                                    )
                                    .values(status="ready", updated_at=utc_now())
                                )
                        else:
                            next_stage_key = get_next_stage_key(stage_key)
                            if next_stage_key is not None:
                                next_stage = self._stage_from_connection(
                                    connection, run_id, next_stage_key
                                )
                                if next_stage is None:
                                    raise not_found("阶段")

                                next_status = str(next_stage.get("status") or "")
                                if (
                                    next_status
                                    not in {
                                        "confirmed",
                                        "cancelled",
                                        "queued",
                                        "running",
                                        "ready",
                                    }
                                    and next_status in _PHASE2_QUEUEABLE_STAGE_STATUSES
                                ):
                                    upstream_content = self._phase2_content(stage)
                                    next_signature = self._downstream_input_signature(
                                        next_stage_key,
                                        run,
                                        upstream_content,
                                    )
                                    now = utc_now()
                                    if next_stage.get("input_signature") != next_signature:
                                        self._interrupt_active_stage_jobs(
                                            connection, next_stage["id"], now
                                        )
                                    self._queue_status(next_status)
                                    self._update_stage(
                                        connection,
                                        next_stage["id"],
                                        {
                                            "status": "queued",
                                            "input_signature": next_signature,
                                            "updated_at": now,
                                            "finished_at": None,
                                            "error": "",
                                        },
                                    )
                                    updated_stage = {
                                        **next_stage,
                                        "status": "queued",
                                        "input_signature": next_signature,
                                    }
                                    target_run_status = run.get("status")
                                    if target_run_status not in {"cancelled", "confirmed"}:
                                        target_run_status = "queued"
                                    connection.execute(
                                        update(workflow_run)
                                        .where(
                                            workflow_run.c.id == run_id,
                                            workflow_run.c.revision == new_revision,
                                        )
                                        .values(status=target_run_status, updated_at=utc_now())
                                    )
                                    updated_run = {
                                        **run,
                                        "status": target_run_status,
                                        "current_stage": current_stage,
                                        "revision": new_revision,
                                    }
                                    if (
                                        materialization is not None
                                        and stage_key == "1-character-bios"
                                    ):
                                        updated_run["asset_mapping"] = materialization["mapping"]
                                    reference = self.job_bridge.ensure_stage_job_in_transaction(
                                        connection,
                                        updated_run,
                                        updated_stage,
                                        retry_failed=True,
                                    )
                                    if reference.status == "queued":
                                        job_ids.append(reference.job_id)
        except Exception as exc:
            self._raise_service_error(exc)
        if gate_rejected:
            raise conflict(f"阶段 {stage_key} 尚未满足确认门禁")
        if prompt_queue_request is not None:
            (
                queue_run_id,
                queue_stage_id,
                provider_id,
                provider_model,
                confirmed_revision,
            ) = prompt_queue_request
            try:
                assert media_queue_service is not None
                queue_result = media_queue_service.queue_confirmed_prompt_stage(
                    workflow_run_id=queue_run_id,
                    workflow_stage_id=queue_stage_id,
                    expected_revision=confirmed_revision,
                    provider_id=provider_id,
                    provider_model=provider_model,
                )
                if isinstance(queue_result, Mapping):
                    self._refresh_delivery_dependencies(queue_run_id)
                queue_status = (
                    queue_result.get("status") if isinstance(queue_result, Mapping) else "error"
                )
                queue_diagnostics = (
                    queue_result.get("diagnostics", []) if isinstance(queue_result, Mapping) else []
                )
                enqueue_warning = any(
                    isinstance(item, Mapping)
                    and item.get("code")
                    in {"media_queue_enqueue_failed", "media_queue_enqueue_rejected"}
                    for item in queue_diagnostics
                )
                if queue_status in {"blocked", "error"} or enqueue_warning:
                    self._record_prompt_queue_warning(
                        run_id,
                        queue_stage_id,
                        stage_key,
                        code=(
                            "dynamic_media_queue_enqueue_failed"
                            if enqueue_warning
                            else "dynamic_media_queue_blocked"
                        ),
                    )
                elif queue_status not in {"ready", "warning"}:
                    self._record_prompt_queue_warning(
                        run_id,
                        queue_stage_id,
                        stage_key,
                        code="dynamic_media_queue_invalid_result",
                    )
                else:
                    self._clear_prompt_queue_warning(run_id, queue_stage_id)
            except Exception:
                self._record_prompt_queue_warning(
                    run_id,
                    queue_stage_id,
                    stage_key,
                    code="dynamic_media_queue_failed",
                )
        self._enqueue_after_commit(job_ids)
        return self._get_action_record(run_id)

    def regenerate_stage(
        self,
        run_id: str,
        stage_key: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._validate_stage_key(stage_key)
        expected_revision = self._revision_value(expected_revision)
        self._require_runner()
        job_ids: list[str] = []
        try:
            with self.database.engine.begin() as connection:
                run = self._run_from_connection(connection, run_id)
                if run is None:
                    raise not_found("运行")
                require_revision(run.get("revision"), expected_revision)
                if run.get("status") in {"cancelled", "confirmed"}:
                    raise conflict("当前运行已结束，不能重新生成阶段")
                current_stage = self._stage_from_connection(connection, run_id, stage_key)
                if current_stage is None:
                    raise not_found("阶段")
                if current_stage["status"] in {"queued", "running"}:
                    raise conflict(f"阶段 {stage_key} 正在处理中，不能重复生成")
                if current_stage["status"] == "cancelled":
                    raise conflict("已取消的阶段不能重新生成")
                stages = self._stages_from_connection(connection, run_id)
                confirmed_keys = self._confirmed_keys(stages)
                ensure_stage_prerequisites_confirmed(stage_key, confirmed_keys)
                max_attempt = connection.execute(
                    select(func.max(workflow_stage.c.attempt)).where(
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.stage_key == stage_key,
                    )
                ).scalar()
                attempt = int(max_attempt or current_stage.get("attempt") or 1) + 1
                self.repository.mark_descendants_stale(connection, run_id, stage_key)
                now = utc_now()
                target_run_status = run.get("status")
                # ready 不能直接转 queued，必须落入下方分支经
                # ready→stale→queued 两步合法转移（见 _queue_status）。
                if target_run_status in {
                    "pending",
                    "blocked",
                    "stale",
                    "failed",
                    "interrupted",
                    "partial_failed",
                }:
                    target_run_status = "queued"
                elif target_run_status == "ready":
                    try:
                        self._queue_status(str(target_run_status))
                    except ValueError as exc:
                        raise conflict(str(exc)) from exc
                    target_run_status = "queued"
                new_revision = self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {
                        "status": target_run_status,
                        "current_stage": self._current_stage_for(stage_key),
                    },
                )
                new_stage = {
                    "id": new_id("stage"),
                    "workflow_run_id": run_id,
                    "stage_key": stage_key,
                    "ordinal": get_stage_ordinal(stage_key),
                    "status": "queued",
                    "input_signature": current_stage.get("input_signature")
                    or run.get("input_signature", ""),
                    "raw_output": {},
                    "normalized_output": {},
                    "draft_output": {},
                    "output_refs": [],
                    "gate_result": {},
                    "attempt": attempt,
                    "error": "",
                    "created_at": now,
                    "updated_at": now,
                    "started_at": None,
                    "finished_at": None,
                    "confirmed_at": None,
                    "confirmed_revision": None,
                }
                connection.execute(
                    insert(workflow_stage).values(
                        id=new_stage["id"],
                        workflow_run_id=new_stage["workflow_run_id"],
                        stage_key=new_stage["stage_key"],
                        ordinal=new_stage["ordinal"],
                        status=new_stage["status"],
                        input_signature=new_stage["input_signature"],
                        raw_output_json=json_dumps({}),
                        normalized_output_json=json_dumps({}),
                        draft_output_json=json_dumps({}),
                        output_refs_json=json_dumps([]),
                        gate_result_json=json_dumps({}),
                        attempt=new_stage["attempt"],
                        error="",
                        created_at=new_stage["created_at"],
                        updated_at=new_stage["updated_at"],
                        started_at=None,
                        finished_at=None,
                        confirmed_at=None,
                        confirmed_revision=None,
                    )
                )
                updated_run = {
                    **run,
                    "status": target_run_status,
                    "current_stage": self._current_stage_for(stage_key),
                    "revision": new_revision,
                }
                reference = self.job_bridge.ensure_stage_job_in_transaction(
                    connection,
                    updated_run,
                    new_stage,
                    retry_failed=False,
                )
                job_ids.append(reference.job_id)
        except Exception as exc:
            self._raise_service_error(exc)
        self._enqueue_after_commit(job_ids)
        return self._get_action_record(run_id)


__all__ = ["DynamicWorkflowService"]
