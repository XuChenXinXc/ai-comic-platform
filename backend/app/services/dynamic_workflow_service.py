from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db import Database, db
from app.dynamic_pipeline.schemas import (
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
from app.dynamic_pipeline.state_machine import (
    RevisionConflict,
    StageBlocked,
    can_confirm_stage,
    can_transition_status,
    ensure_stage_prerequisites_confirmed,
    require_revision,
    transition_status,
)
from app.jobs.dynamic_bridge import DynamicJobBridge, DynamicJobBridgeError
from app.jobs.repository import JobRepository
from app.models.tables import api_provider, chapter, project, workflow_run, workflow_stage
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
_RESUMABLE_RUN_STATUSES = {"blocked", "stale", "failed", "interrupted"}
_RESUMABLE_STAGE_STATUSES = {
    "pending",
    "blocked",
    "stale",
    "failed",
    "interrupted",
}
_CANCELLABLE_STAGE_STATUSES = {"queued", "running", "blocked", "ready", "stale"}


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
    ) -> None:
        self.database = database or db
        self.repository = DynamicWorkflowRepository(self.database)
        self.storage = storage or FileService(database=self.database)
        self.job_bridge = job_bridge or DynamicJobBridge(JobRepository(self.database))

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
        if isinstance(exc, DynamicWorkflowNotFoundError):
            return not_found("动态运行")
        if isinstance(exc, IntegrityError):
            return conflict("动态工作流数据冲突，请刷新后重试")
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

    @staticmethod
    def _queue_status(current_status: str) -> str:
        """将可恢复状态按状态机规则转换为 queued。"""
        if current_status == "ready":
            transition_status(current_status, "stale")
            return transition_status("stale", "queued")
        return transition_status(current_status, "queued")

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

    def _enqueue_after_commit(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        try:
            self.job_bridge.enqueue_after_commit(job_ids)
        except Exception:
            # 入队失败时保留 queued 状态，由动态恢复流程后续补偿。
            return

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
                        "status": "pending",
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
                        "status": "pending",
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
                if stage["status"] in {"pending", "queued", "running"}:
                    transition_status(stage["status"], "blocked")
                    self._update_stage(
                        connection,
                        stage["id"],
                        {"status": "blocked", "updated_at": utc_now()},
                    )
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
        except Exception as exc:
            self._raise_service_error(exc)
        return self._get_action_record(run_id)

    def resume_run(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        expected_revision = self._revision_value(expected_revision)
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
                try:
                    self._queue_status(current_status)
                    self._queue_status(str(target_stage["status"]))
                except ValueError as exc:
                    raise conflict(str(exc)) from exc
                ensure_stage_prerequisites_confirmed(target_key, confirmed)
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
                updated_stage = {**target_stage, "status": "queued"}
                self._update_stage(
                    connection,
                    target_stage["id"],
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
        if payload.gate_result is None:
            raise conflict("保存阶段草稿必须提供门禁结果")
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
                values: dict[str, Any] = {
                    "draft_output_json": json_dumps(self._model_dump(payload.draft_output)),
                    "updated_at": utc_now(),
                }
                if payload.gate_result is not None:
                    values["gate_result_json"] = json_dumps(self._model_dump(payload.gate_result))
                if stage["status"] == "confirmed":
                    values["status"] = "stale"
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
                if not can_confirm_stage(stage["status"], stage.get("gate_result")):
                    raise conflict(f"阶段 {stage_key} 尚未满足确认门禁")
                confirmed_after = set(confirmed_keys)
                confirmed_after.add(stage_key)
                current_stage = self._next_current_stage(stage_key, confirmed_after)
                now = utc_now()
                new_revision = self._bump_run(
                    connection,
                    run_id,
                    expected_revision,
                    {"current_stage": current_stage},
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
        except Exception as exc:
            self._raise_service_error(exc)
        return self._get_action_record(run_id)

    def regenerate_stage(
        self,
        run_id: str,
        stage_key: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._validate_stage_key(stage_key)
        expected_revision = self._revision_value(expected_revision)
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
                if target_run_status in {
                    "pending",
                    "ready",
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
