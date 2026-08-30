from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update

from app.db import Database
from app.dynamic_pipeline import normalizers, script_stage, stage_gates, storyboard_stage
from app.dynamic_pipeline.schemas import (
    ArtifactMeta,
    CharacterBiosOutput,
    RhythmOutput,
    ScriptOutput,
    SourceAnalysisOutput,
    StageGateResult,
)
from app.dynamic_pipeline.stage_catalog import get_stage_prerequisites
from app.models.tables import job, workflow_run, workflow_stage
from app.prompts import load_prompt
from app.services.provider_service import ProviderService
from app.services.signature_service import calculate_signature, source_signature
from app.services.utils import json_dumps, json_loads, sanitize_error_message, utc_now

DYNAMIC_STAGE_KEYS = (
    "0-source",
    "1-character-bios",
    "2-rhythm",
    "3-script",
    "4-storyboard",
)

_EXECUTABLE_JOB_STATUSES = frozenset({"queued", "running"})
_EXECUTABLE_WORKFLOW_STATUSES = frozenset({"queued", "running"})
_PROTECTED_WORKFLOW_STATUSES = frozenset({"cancelled", "stale", "confirmed", "blocked"})
_PROTECTED_JOB_STATUSES = frozenset({"completed", "cancelled", "interrupted"})

_STAGE_SCHEMAS: dict[str, type[Any]] = {
    "0-source": SourceAnalysisOutput,
    "1-character-bios": CharacterBiosOutput,
    "2-rhythm": RhythmOutput,
    "3-script": ScriptOutput,
    "4-storyboard": storyboard_stage.StoryboardOutput,
}
_PROMPT_NAMES = {
    "0-source": "dynamic_source",
    "1-character-bios": "dynamic_character_bios",
    "2-rhythm": "dynamic_rhythm",
    "3-script": "dynamic_script",
    "4-storyboard": "dynamic_storyboard",
}

# 分镜单批最大生成尝试次数；批间衔接与覆盖校验失败只重试本批
_STORYBOARD_BATCH_MAX_ATTEMPTS = 3


class StageExecutionError(RuntimeError):
    """动态阶段执行前后状态不满足并发安全条件。"""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        stage_key: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.stage_key = stage_key
        super().__init__(message)


class _ResultWriteConflict(RuntimeError):
    """结果提交时条件更新未命中。"""


@dataclass(slots=True)
class _ExecutionContext:
    job_record: dict[str, Any]
    run_record: dict[str, Any]
    stage_record: dict[str, Any]
    upstream_outputs: dict[str, dict[str, Any]]

    @property
    def job_id(self) -> str:
        return str(self.job_record["id"])

    @property
    def run_id(self) -> str:
        return str(self.run_record["id"])

    @property
    def stage_id(self) -> str:
        return str(self.stage_record["id"])

    @property
    def stage_key(self) -> str:
        return str(self.stage_record["stage_key"])


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _decode_json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    return json_loads(value, default)


def _decode_job(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    record["input_data"] = _decode_json(record.get("input_json"), {})
    record["result_data"] = _decode_json(record.get("result_json"), {})
    return record


def _decode_run(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    record["source_snapshot"] = _decode_json(record.get("source_snapshot_json"), {})
    record["config_snapshot"] = _decode_json(record.get("config_snapshot_json"), {})
    return record


def _decode_stage(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    for database_name, public_name, default in (
        ("raw_output_json", "raw_output", {}),
        ("normalized_output_json", "normalized_output", {}),
        ("draft_output_json", "draft_output", {}),
        ("output_refs_json", "output_refs", []),
        ("gate_result_json", "gate_result", {}),
    ):
        record[public_name] = _decode_json(record.get(database_name), default)
    return record


def _non_empty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _signature_condition(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _error_text(value: Any) -> str:
    return sanitize_error_message(value) or "动态阶段执行失败"


class DynamicStageService:
    """执行动态工作流前五个文本阶段，不负责后续 legacy 物化。"""

    def __init__(self, database: Database, provider_service: Any | None = None) -> None:
        self.database = database
        self.provider_service = (
            provider_service if provider_service is not None else ProviderService(database)
        )
        # 分镜批次结果内存缓存：同一 run+签名+批次规划的后续 attempt 复用已成功批，
        # 避免「某批反复失败 → 整章重跑」的全量浪费。仅进程内有效，重启即失。
        self._storyboard_batch_cache: dict[
            tuple[str, str, tuple[tuple[tuple[str, ...], float, float], ...]],
            list[storyboard_stage.StoryboardOutput | None],
        ] = {}

    def _load_job(self, connection: Any, job_id: str) -> dict[str, Any] | None:
        return _decode_job(connection.execute(select(job).where(job.c.id == job_id)).first())

    def _load_run(self, connection: Any, run_id: str) -> dict[str, Any] | None:
        return _decode_run(
            connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
        )

    def _load_stage_by_id(
        self,
        connection: Any,
        stage_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        statement = select(workflow_stage).where(workflow_stage.c.id == stage_id)
        if run_id is not None:
            statement = statement.where(workflow_stage.c.workflow_run_id == run_id)
        return _decode_stage(connection.execute(statement).first())

    def _latest_stage(
        self,
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
        return _decode_stage(row)

    def _read_associated_records(
        self,
        connection: Any,
        job_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        current_job = self._load_job(connection, job_id)
        if current_job is None:
            raise StageExecutionError(f"Job 不存在：{job_id}", job_id=job_id)

        run_id = current_job.get("workflow_run_id")
        stage_id = current_job.get("workflow_stage_id")
        if not run_id or not stage_id:
            raise StageExecutionError(
                "动态 Job 缺少 workflow_run_id 或 workflow_stage_id",
                job_id=job_id,
            )

        current_run = self._load_run(connection, str(run_id))
        if current_run is None:
            raise StageExecutionError(
                f"动态 Job 关联的 run 不存在：{run_id}",
                job_id=job_id,
            )

        current_stage = self._load_stage_by_id(connection, str(stage_id), str(run_id))
        if current_stage is None:
            raise StageExecutionError(
                f"动态 Job 关联的 stage 不存在或不属于该 run：{stage_id}",
                job_id=job_id,
            )

        if current_job.get("project_id") != current_run.get("project_id"):
            raise StageExecutionError("动态 Job 与 run 的 project 关联不一致", job_id=job_id)
        if current_job.get("chapter_id") != current_run.get("chapter_id"):
            raise StageExecutionError("动态 Job 与 run 的 chapter 关联不一致", job_id=job_id)
        if current_stage.get("workflow_run_id") != current_run.get("id"):
            raise StageExecutionError("stage 不属于该 run", job_id=job_id)

        stage_key = str(current_stage.get("stage_key") or "")
        if stage_key not in DYNAMIC_STAGE_KEYS:
            raise StageExecutionError(
                f"当前阶段不在最小可执行范围内：{stage_key!r}",
                job_id=job_id,
                stage_key=stage_key,
            )

        latest = self._latest_stage(connection, str(run_id), stage_key)
        if latest is None or latest.get("id") != current_stage.get("id"):
            raise StageExecutionError(
                f"stage 不是当前最新 attempt：{stage_key}",
                job_id=job_id,
                stage_key=stage_key,
            )
        return current_job, current_run, current_stage

    @staticmethod
    def _validate_statuses(
        current_job: Mapping[str, Any],
        current_run: Mapping[str, Any],
        current_stage: Mapping[str, Any],
    ) -> None:
        job_status = str(current_job.get("status") or "")
        run_status = str(current_run.get("status") or "")
        stage_status = str(current_stage.get("status") or "")
        if job_status in _PROTECTED_JOB_STATUSES or job_status not in _EXECUTABLE_JOB_STATUSES:
            raise StageExecutionError(
                f"Job 当前状态不可执行：{job_status!r}",
                job_id=str(current_job.get("id")),
                stage_key=str(current_stage.get("stage_key")),
            )
        if (
            run_status in _PROTECTED_WORKFLOW_STATUSES
            or run_status not in _EXECUTABLE_WORKFLOW_STATUSES
        ):
            raise StageExecutionError(
                f"run 当前状态不可执行：{run_status!r}",
                job_id=str(current_job.get("id")),
                stage_key=str(current_stage.get("stage_key")),
            )
        if (
            stage_status in _PROTECTED_WORKFLOW_STATUSES
            or stage_status not in _EXECUTABLE_WORKFLOW_STATUSES
        ):
            raise StageExecutionError(
                f"stage 当前状态不可执行：{stage_status!r}",
                job_id=str(current_job.get("id")),
                stage_key=str(current_stage.get("stage_key")),
            )

    @staticmethod
    def _signature_mismatch(
        current_job: Mapping[str, Any],
        current_stage: Mapping[str, Any],
    ) -> bool:
        job_signature = current_job.get("input_signature")
        stage_signature = current_stage.get("input_signature")
        return bool(
            _non_empty(job_signature)
            and _non_empty(stage_signature)
            and job_signature != stage_signature
        )

    def _mark_signature_mismatch(
        self,
        connection: Any,
        current_job: Mapping[str, Any],
        current_run: Mapping[str, Any],
        current_stage: Mapping[str, Any],
    ) -> None:
        now = utc_now()
        message = _error_text(
            "动态 Job 与 stage 的 input_signature 不一致；未调用 Provider，结果已作废"
        )
        connection.execute(
            update(job)
            .where(
                job.c.id == current_job["id"],
                job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                job.c.updated_at == current_job["updated_at"],
            )
            .values(
                status="interrupted",
                error=message,
                message="任务因输入签名变化而中断",
                finished_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.id == current_stage["id"],
                workflow_stage.c.workflow_run_id == current_run["id"],
                workflow_stage.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                workflow_stage.c.updated_at == current_stage["updated_at"],
            )
            .values(status="stale", error=message, finished_at=now, updated_at=now)
        )
        connection.execute(
            update(workflow_run)
            .where(
                workflow_run.c.id == current_run["id"],
                workflow_run.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                workflow_run.c.revision == current_run["revision"],
                workflow_run.c.updated_at == current_run["updated_at"],
            )
            .values(
                status="interrupted",
                revision=workflow_run.c.revision + 1,
                error=message,
                finished_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _mark_prerequisite_failure(
        connection: Any,
        current_job: Mapping[str, Any],
        missing_keys: tuple[str, ...],
    ) -> None:
        message = _error_text(f"阶段前置确认未满足：{', '.join(missing_keys)}；未调用 Provider")
        now = utc_now()
        connection.execute(
            update(job)
            .where(
                job.c.id == current_job["id"],
                job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                job.c.updated_at == current_job["updated_at"],
            )
            .values(
                status="failed",
                error=message,
                message="任务因前置阶段未确认而失败",
                finished_at=now,
                updated_at=now,
            )
        )

    def _claim_for_execution(
        self,
        connection: Any,
        current_job: Mapping[str, Any],
        current_run: Mapping[str, Any],
        current_stage: Mapping[str, Any],
    ) -> None:
        now = utc_now()
        if current_job.get("status") == "queued":
            result = connection.execute(
                update(job)
                .where(
                    job.c.id == current_job["id"],
                    job.c.status == "queued",
                    job.c.updated_at == current_job["updated_at"],
                )
                .values(
                    status="running",
                    error="",
                    message="动态阶段执行中",
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise StageExecutionError("Job 在执行前发生并发状态变化")

        if current_stage.get("status") == "queued":
            result = connection.execute(
                update(workflow_stage)
                .where(
                    workflow_stage.c.id == current_stage["id"],
                    workflow_stage.c.workflow_run_id == current_run["id"],
                    workflow_stage.c.status == "queued",
                    workflow_stage.c.attempt == current_stage["attempt"],
                    workflow_stage.c.updated_at == current_stage["updated_at"],
                )
                .values(
                    status="running",
                    error="",
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise StageExecutionError("stage 在执行前发生并发状态变化")

        if current_run.get("status") == "queued":
            result = connection.execute(
                update(workflow_run)
                .where(
                    workflow_run.c.id == current_run["id"],
                    workflow_run.c.status == "queued",
                    workflow_run.c.revision == current_run["revision"],
                    workflow_run.c.updated_at == current_run["updated_at"],
                )
                .values(
                    status="running",
                    revision=workflow_run.c.revision + 1,
                    error="",
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise StageExecutionError("run 在执行前发生并发状态变化")

    def _confirmed_upstream(
        self,
        connection: Any,
        run_id: str,
        stage_key: str,
    ) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
        missing: list[str] = []
        outputs: dict[str, dict[str, Any]] = {}
        current_ordinal = DYNAMIC_STAGE_KEYS.index(stage_key)
        for candidate_key in DYNAMIC_STAGE_KEYS[:current_ordinal]:
            candidate = self._latest_stage(connection, run_id, candidate_key)
            if candidate is None or candidate.get("status") != "confirmed":
                if candidate_key in get_stage_prerequisites(stage_key):
                    missing.append(candidate_key)
                continue
            value = candidate.get("draft_output") or candidate.get("normalized_output")
            outputs[candidate_key] = value if isinstance(value, dict) else {}
        return tuple(missing), outputs

    def _preflight(self, job_record: Mapping[str, Any]) -> _ExecutionContext:
        job_id = str(job_record.get("id") or "")
        if not job_id:
            raise StageExecutionError("Job 缺少 id")
        if job_record.get("status") == "completed":
            raise StageExecutionError("已完成的 Job 不能重复执行", job_id=job_id)

        rejection: StageExecutionError | None = None
        context: _ExecutionContext | None = None
        with self.database.engine.begin() as connection:
            current_job, current_run, current_stage = self._read_associated_records(
                connection,
                job_id,
            )
            self._validate_statuses(current_job, current_run, current_stage)

            if self._signature_mismatch(current_job, current_stage):
                self._mark_signature_mismatch(connection, current_job, current_run, current_stage)
                rejection = StageExecutionError(
                    "Job 与 stage 的 input_signature 不一致，已中断 Job 并标记 stage stale",
                    job_id=job_id,
                    stage_key=str(current_stage["stage_key"]),
                )
            else:
                missing_keys, upstream_outputs = self._confirmed_upstream(
                    connection,
                    str(current_run["id"]),
                    str(current_stage["stage_key"]),
                )
                if missing_keys:
                    self._mark_prerequisite_failure(connection, current_job, missing_keys)
                    rejection = StageExecutionError(
                        f"阶段前置确认未满足：{', '.join(missing_keys)}",
                        job_id=job_id,
                        stage_key=str(current_stage["stage_key"]),
                    )
                else:
                    self._claim_for_execution(
                        connection,
                        current_job,
                        current_run,
                        current_stage,
                    )
                    claimed_job = self._load_job(connection, job_id)
                    claimed_run = self._load_run(connection, str(current_run["id"]))
                    claimed_stage = self._load_stage_by_id(
                        connection,
                        str(current_stage["id"]),
                        str(current_run["id"]),
                    )
                    if claimed_job is None or claimed_run is None or claimed_stage is None:
                        raise StageExecutionError(
                            "动态阶段执行记录在预检提交后不存在",
                            job_id=job_id,
                        )
                    context = _ExecutionContext(
                        job_record=claimed_job,
                        run_record=claimed_run,
                        stage_record=claimed_stage,
                        upstream_outputs=upstream_outputs,
                    )

        if rejection is not None:
            raise rejection
        if context is None:
            raise StageExecutionError("动态阶段预检未生成执行上下文", job_id=job_id)
        return context

    @staticmethod
    def _prompt(stage_key: str) -> str:
        try:
            prompt = load_prompt(_PROMPT_NAMES[stage_key])
        except FileNotFoundError as exc:
            raise StageExecutionError(
                f"阶段 {stage_key} 缺少权威 dynamic Prompt 模板，已停止生成",
                stage_key=stage_key,
            ) from exc
        if not prompt.strip():
            raise StageExecutionError(
                f"阶段 {stage_key} 的 dynamic Prompt 模板为空，已停止生成",
                stage_key=stage_key,
            )
        return prompt

    @staticmethod
    def _target_duration(config_snapshot: Mapping[str, Any]) -> Any:
        value = config_snapshot.get("target_duration_sec", 30)
        return 30 if value is None else value

    @staticmethod
    def _source_signature(source_snapshot: Mapping[str, Any], fallback: Any) -> str:
        value = source_snapshot.get("source_signature")
        if _non_empty(value):
            return str(value)
        text = source_snapshot.get("text")
        if not _non_empty(text):
            text = source_snapshot.get("source_text")
        if _non_empty(text):
            return source_signature(str(text))
        if _non_empty(fallback):
            return str(fallback)
        return "dynamic-source-unknown"

    @staticmethod
    def _stage_input(
        context: _ExecutionContext,
        target_duration_sec: Any,
        job_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        upstream = context.upstream_outputs
        if context.stage_key == "3-script":
            required = {
                "source_analysis": upstream.get("0-source", {}),
                "character_bios": upstream.get("1-character-bios", {}),
                "rhythm": upstream.get("2-rhythm", {}),
            }
        elif context.stage_key == "4-storyboard":
            required = {
                "script": upstream.get("3-script", {}),
                "rhythm": upstream.get("2-rhythm", {}),
            }
        else:
            required = {}
        return {
            "stage_key": context.stage_key,
            "target_duration_sec": target_duration_sec,
            "required_upstream_inputs": required,
            "params": dict(job_input.get("params", {}))
            if isinstance(job_input.get("params"), Mapping)
            else {},
        }

    def _build_user_prompt(
        self,
        context: _ExecutionContext,
        incoming_job: Mapping[str, Any],
        target_duration_sec: Any,
    ) -> str:
        source_snapshot = context.run_record.get("source_snapshot")
        config_snapshot = context.run_record.get("config_snapshot")
        job_input = incoming_job.get("input_data")
        if not isinstance(job_input, Mapping):
            job_input = context.job_record.get("input_data")
        if not isinstance(job_input, Mapping):
            job_input = {}
        if context.stage_key == "4-storyboard" and isinstance(source_snapshot, Mapping):
            # 分镜阶段禁止读取章节正文补事实，快照只保留元信息，减少输入体积
            source_snapshot = {
                key: value for key, value in source_snapshot.items() if key != "text"
            }
        payload = {
            "source_snapshot": source_snapshot if isinstance(source_snapshot, dict) else {},
            "config_snapshot": config_snapshot if isinstance(config_snapshot, dict) else {},
            "confirmed_upstream_stages": context.upstream_outputs,
            "current_stage_input": self._stage_input(
                context,
                target_duration_sec,
                job_input,
            ),
        }
        return (
            "请严格依据以下快照和已确认上游产物输出 JSON，不要补充快照之外的事实：\n"
            + json_dumps(payload)
        )

    @staticmethod
    def _stage_schema_version(stage_key: str) -> str:
        return {
            "3-script": "dynamic-script-v2",
            "4-storyboard": "dynamic-storyboard-v2",
        }[stage_key]

    @staticmethod
    def _rebuild_stage_artifact_meta(
        normalized: Any,
        *,
        stage_key: str,
        input_signature: str,
        source_attempt: int,
        provider_id: str | None,
        provider_model: str | None,
    ) -> Any:
        payload = DynamicStageService._model_dump(normalized)
        canonical_content = {key: value for key, value in payload.items() if key != "artifact_meta"}
        metadata = ArtifactMeta(
            schema_version=DynamicStageService._stage_schema_version(stage_key),
            normalizer_version=normalizers.NORMALIZER_VERSION,
            input_signature=input_signature,
            content_signature=calculate_signature(canonical_content, input_signature),
            source_attempt=source_attempt,
            provider_id=provider_id,
            provider_model=provider_model,
        )
        payload["artifact_meta"] = metadata.model_dump(mode="json")
        return type(normalized).model_validate(payload)

    @staticmethod
    def _preserve_character_behavior_evidence(
        normalized: Any,
        raw_payload: Mapping[str, Any],
    ) -> Any:
        """归一化旧人物别名时保留新契约的结构化行为证据。"""

        if not isinstance(normalized, CharacterBiosOutput):
            return normalized
        raw_characters = raw_payload.get("characters")
        if not isinstance(raw_characters, list):
            return normalized
        evidence_by_key: dict[str, Any] = {}
        for item in raw_characters:
            if not isinstance(item, Mapping):
                continue
            basic_info = item.get("basic_info")
            if not isinstance(basic_info, Mapping):
                continue
            key = basic_info.get("character_key")
            evidence = item.get("behavior_evidence")
            if isinstance(key, str) and isinstance(evidence, Mapping):
                evidence_by_key[key] = dict(evidence)
        if not evidence_by_key:
            return normalized
        payload = normalized.model_dump(mode="python")
        characters = payload.get("characters")
        if not isinstance(characters, list):
            return normalized
        for index, item in enumerate(characters):
            if not isinstance(item, Mapping):
                continue
            basic_info = item.get("basic_info")
            key = basic_info.get("character_key") if isinstance(basic_info, Mapping) else None
            if not isinstance(key, str):
                continue
            evidence = evidence_by_key.get(key)
            if evidence is not None:
                characters[index] = {**dict(item), "behavior_evidence": evidence}
        return CharacterBiosOutput.model_validate(payload)

    @staticmethod
    def _persist_server_rhythm_budget(
        normalized: Any,
        config_snapshot: Mapping[str, Any],
    ) -> Any:
        if not isinstance(normalized, RhythmOutput):
            return normalized
        payload = stage_gates.server_bound_rhythm_content(normalized, config_snapshot)
        artifact_meta = payload.get("artifact_meta") if isinstance(payload, Mapping) else None
        if isinstance(payload, Mapping) and isinstance(artifact_meta, Mapping):
            canonical_content = {
                key: value for key, value in payload.items() if key != "artifact_meta"
            }
            rebuilt_meta = dict(artifact_meta)
            rebuilt_meta["content_signature"] = calculate_signature(
                canonical_content,
                rebuilt_meta.get("input_signature", ""),
            )
            payload = {**payload, "artifact_meta": rebuilt_meta}
        return RhythmOutput.model_validate(payload)

    @staticmethod
    def _stage_gate(
        diagnostics: list[Any],
        *,
        now: str,
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
        status = "blocked" if has_blocking else "warning" if ordered else "ready"
        return StageGateResult(
            status=status,
            passed=not has_blocking,
            diagnostics=ordered,
            checked_at=now,
            can_confirm=not has_blocking,
        )

    async def _generate_and_normalize(
        self,
        context: _ExecutionContext,
        incoming_job: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any], stage_gates.StageGateResult, dict[str, Any]]:
        requested_provider_id = incoming_job.get("provider_id") or None
        provider, resolved_config = self.provider_service.resolve(
            kind="text",
            provider_id=requested_provider_id or None,
        )
        if isinstance(resolved_config, BaseModel):
            resolved_config = resolved_config.model_dump(mode="json")
        if not isinstance(resolved_config, Mapping):
            raise TypeError("Provider resolve 未返回配置对象")
        config = dict(resolved_config)
        schema = _STAGE_SCHEMAS[context.stage_key]
        config_params = config.get("params")
        if not isinstance(config_params, Mapping):
            config_params = {}
        job_input = incoming_job.get("input_data") or context.job_record.get("input_data")
        job_params = job_input.get("params") if isinstance(job_input, Mapping) else {}
        if not isinstance(job_params, Mapping):
            job_params = {}
        params = {**dict(config_params), **dict(job_params)}

        source_snapshot = context.run_record.get("source_snapshot")
        if not isinstance(source_snapshot, Mapping):
            source_snapshot = {}
        config_snapshot = context.run_record.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = {}
        target_duration = self._target_duration(config_snapshot)

        if context.stage_key == "4-storyboard":
            # 阶段 4 按场景分批串行生成（见 _generate_storyboard_batches），
            # 跳过这里的单次全量生成；raw_payload 由批次流程归档。
            output = None
            raw_payload = {}
        else:
            output = await provider.generate_json(
                system_prompt=self._prompt(context.stage_key),
                user_prompt=self._build_user_prompt(context, incoming_job, target_duration),
                schema=schema,
                params=params,
            )
            model_dump = getattr(output, "model_dump", None)
            if not callable(model_dump):
                raise TypeError("Provider 返回值必须是 Pydantic 模型")
            raw_payload = model_dump(mode="json")
            if not isinstance(raw_payload, dict):
                raise TypeError("Provider model_dump 必须返回对象")
            # Provider 层和本服务层都要以目标阶段的严格模型为边界，不能把不合法的
            # Script/Storyboard 结果降级成可确认的占位产物。
            output = schema.model_validate(raw_payload)
            raw_payload = output.model_dump(mode="json")

        provider_id = config.get("id") or config.get("provider_id") or None
        provider_model = config.get("model") or config.get("provider_model") or None
        input_signature = (
            context.stage_record.get("input_signature")
            or context.job_record.get("input_signature")
            or context.run_record.get("input_signature")
            or "dynamic-input-unknown"
        )
        normalize_kwargs: dict[str, Any] = {
            "input_signature": input_signature,
            "source_attempt": int(context.stage_record.get("attempt") or 1),
            "provider_id": provider_id,
            "provider_model": provider_model,
        }
        normalized: Any = None
        gate: StageGateResult | None = None
        if context.stage_key == "0-source":
            normalize_kwargs.update(
                {
                    "source_kind": source_snapshot.get(
                        "source_kind", context.run_record.get("source_kind", "full_text")
                    ),
                    "title": source_snapshot.get("title", "动态故事"),
                    "chapter_title": source_snapshot.get("chapter_title", "当前章节"),
                    "target_duration_sec": target_duration,
                    "aspect_ratio": source_snapshot.get("aspect_ratio")
                    or config_snapshot.get("aspect_ratio", "9:16"),
                    "source_signature": self._source_signature(
                        source_snapshot,
                        context.run_record.get("input_signature"),
                    ),
                }
            )
        elif context.stage_key == "2-rhythm":
            normalize_kwargs["target_duration_sec"] = target_duration

        if context.stage_key == "3-script":
            normalized = script_stage.normalize_script_output(
                raw_payload,
                artifact_meta={
                    "schema_version": self._stage_schema_version(context.stage_key),
                    "normalizer_version": normalizers.NORMALIZER_VERSION,
                    "input_signature": str(input_signature),
                    "content_signature": "pending",
                    "source_attempt": int(context.stage_record.get("attempt") or 1),
                    "provider_id": provider_id,
                    "provider_model": provider_model,
                },
                target_duration_sec=target_duration,
                source_refs=source_snapshot.get("source_refs", [])
                if isinstance(source_snapshot.get("source_refs"), list)
                else (),
            )
            normalized = self._rebuild_stage_artifact_meta(
                normalized,
                stage_key=context.stage_key,
                input_signature=str(input_signature),
                source_attempt=int(context.stage_record.get("attempt") or 1),
                provider_id=provider_id,
                provider_model=provider_model,
            )
            rhythm = context.upstream_outputs.get("2-rhythm")
            gate = self._stage_gate(
                script_stage.validate_script_output(
                    normalized,
                    rhythm=rhythm,
                    target_duration_sec=target_duration,
                ),
                now=utc_now(),
            )
        elif context.stage_key == "4-storyboard":
            try:
                script = ScriptOutput.model_validate(context.upstream_outputs.get("3-script", {}))
            except (TypeError, ValueError) as exc:
                raise TypeError("阶段 3 ScriptOutput 严格解析失败") from exc
            rhythm = stage_gates.server_bound_rhythm_content(
                context.upstream_outputs.get("2-rhythm"),
                config_snapshot,
            )
            normalized = await self._generate_storyboard_batches(
                context,
                provider,
                params,
                schema,
                script,
                rhythm,
                target_duration,
                config,
                str(input_signature),
            )
            raw_payload = {
                "capture_mode": "storyboard_batches",
                "merged_shot_count": len(normalized.shots),
            }
            normalized = self._rebuild_stage_artifact_meta(
                normalized,
                stage_key=context.stage_key,
                input_signature=str(input_signature),
                source_attempt=int(context.stage_record.get("attempt") or 1),
                provider_id=provider_id,
                provider_model=provider_model,
            )
            gate = self._stage_gate(
                storyboard_stage.validate_storyboard_output(
                    normalized,
                    script=script,
                    rhythm=rhythm,
                    target_duration_sec=target_duration,
                ),
                now=utc_now(),
            )
        else:
            normalized = normalizers.normalize_stage_output(
                context.stage_key,
                raw_payload,
                **normalize_kwargs,
            )
            normalized = self._preserve_character_behavior_evidence(normalized, raw_payload)
            normalized = self._persist_server_rhythm_budget(normalized, config_snapshot)
            gate = stage_gates.evaluate_stage_gate(
                context.stage_key,
                normalized,
                target_duration_sec=target_duration,
                config_snapshot=config_snapshot,
                now=utc_now(),
            )
        if normalized is None or gate is None:
            raise TypeError(f"阶段 {context.stage_key} 未生成合法结果")
        return normalized, config, gate, raw_payload

    async def _generate_storyboard_batches(
        self,
        context: _ExecutionContext,
        provider: Any,
        params: Mapping[str, Any],
        schema: type[Any],
        script: ScriptOutput,
        rhythm: Any,
        target_duration: Any,
        config: Mapping[str, Any],
        input_signature: str,
    ) -> storyboard_stage.StoryboardOutput:
        """按剧本场景分批串行生成标准分镜，批间传递上一批末镜状态。

        每批输入只含本批场景的剧本、重叠的节奏段、人物小传与上一批末镜，
        不携带章节正文；每批生成后立即做覆盖校验（剧情事件、对白原文与
        顺序、强动态事件、continuous 衔接），失败只重试本批。全部批通过
        后合并为全局 StoryboardOutput，由调用方继续走全局门禁。
        """

        system_prompt = self._prompt(context.stage_key)
        source_snapshot = context.run_record.get("source_snapshot")
        if not isinstance(source_snapshot, Mapping):
            source_snapshot = {}
        config_snapshot = context.run_record.get("config_snapshot")
        if not isinstance(config_snapshot, Mapping):
            config_snapshot = {}
        bios = context.upstream_outputs.get("1-character-bios")
        if isinstance(bios, BaseModel):
            bios_payload = bios.model_dump(mode="json")
        elif isinstance(bios, Mapping):
            bios_payload = dict(bios)
        else:
            bios_payload = {}
        source_meta = {key: value for key, value in source_snapshot.items() if key != "text"}

        plans = storyboard_stage.plan_storyboard_batches(script, max_batch_duration_sec=120.0)
        if not plans:
            raise RuntimeError("阶段 3 剧本没有场景，无法拆解分镜批次")
        provider_id = config.get("id") or config.get("provider_id") or None
        provider_model = config.get("model") or config.get("provider_model") or None
        attempt = int(context.stage_record.get("attempt") or 1)
        asset_keys_by_scene = storyboard_stage.source_asset_keys_by_scene(
            context.upstream_outputs.get("0-source")
        )

        collected: list[storyboard_stage.StoryboardOutput] = []
        previous_shot: storyboard_stage.StoryboardShot | None = None
        plan_fingerprint = tuple(
            (tuple(plan.scene_ids), plan.start_sec, plan.end_sec) for plan in plans
        )
        cache_key = (context.run_id, input_signature, plan_fingerprint)
        cached_batches = self._storyboard_batch_cache.get(cache_key)
        for plan_index, plan in enumerate(plans):
            cached = (
                cached_batches[plan_index]
                if cached_batches is not None and plan_index < len(cached_batches)
                else None
            )
            if cached is not None:
                collected.append(cached)
                if cached.shots:
                    previous_shot = cached.shots[-1]
                continue
            batch_data = storyboard_stage.build_storyboard_batch_payload(
                plan,
                script=script,
                rhythm=rhythm,
                previous_shot=previous_shot,
                target_duration_sec=target_duration,
                batch_count=len(plans),
                asset_keys_by_scene=asset_keys_by_scene,
            )
            base_prompt = (
                "请严格依据以下快照和已确认上游产物输出 JSON，不要补充快照之外的事实：\n"
                + json_dumps(
                    {
                        "source_snapshot": source_meta,
                        "config_snapshot": config_snapshot,
                        "confirmed_upstream_stages": (
                            {"1-character-bios": bios_payload} if bios_payload else {}
                        ),
                        "current_stage_input": batch_data,
                    }
                )
            )
            batch_output: storyboard_stage.StoryboardOutput | None = None
            repair_feedback: list[str] = []
            for _attempt_no in range(1, _STORYBOARD_BATCH_MAX_ATTEMPTS + 1):
                user_prompt = base_prompt
                if repair_feedback:
                    user_prompt += (
                        "\n\n上一次批次输出未通过覆盖校验，请修正后重新输出完整 JSON：\n- "
                        + "\n- ".join(repair_feedback)
                    )
                raw_model = await provider.generate_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                    params=params,
                )
                raw_payload = raw_model.model_dump(mode="json")
                batch_normalized = storyboard_stage.normalize_storyboard_output(
                    raw_payload,
                    artifact_meta={
                        "schema_version": self._stage_schema_version(context.stage_key),
                        "normalizer_version": normalizers.NORMALIZER_VERSION,
                        "input_signature": str(input_signature),
                        "content_signature": "pending",
                        "source_attempt": attempt,
                        "provider_id": provider_id,
                        "provider_model": provider_model,
                    },
                    target_duration_sec=target_duration,
                    run_id=context.run_id,
                    chapter_id=str(context.run_record.get("chapter_id") or ""),
                    script=script,
                )
                diagnostics = storyboard_stage.validate_storyboard_batch(
                    batch_normalized,
                    plan=plan,
                    script=script,
                    rhythm=rhythm,
                    previous_shot=previous_shot,
                )
                blocking = [item for item in diagnostics if item.level == "blocking"]
                if not blocking:
                    batch_output = batch_normalized
                    break
                repair_feedback = [f"{item.code}：{item.message}" for item in blocking]
            if batch_output is None:
                raise RuntimeError(
                    f"分镜批次 {plan.batch_index}/{len(plans)} 在 "
                    f"{_STORYBOARD_BATCH_MAX_ATTEMPTS} 次尝试后仍未通过覆盖校验："
                    + "；".join(repair_feedback)
                )
            collected.append(batch_output)
            if batch_output.shots:
                previous_shot = batch_output.shots[-1]
            batch_cache = self._storyboard_batch_cache.setdefault(cache_key, [None] * len(plans))
            batch_cache[plan_index] = batch_output
            if len(self._storyboard_batch_cache) > 16:
                self._storyboard_batch_cache.clear()

        return storyboard_stage.merge_storyboard_batches(
            collected,
            artifact_meta={
                "schema_version": self._stage_schema_version(context.stage_key),
                "normalizer_version": normalizers.NORMALIZER_VERSION,
                "input_signature": str(input_signature),
                "content_signature": "pending",
                "source_attempt": attempt,
                "provider_id": provider_id,
                "provider_model": provider_model,
            },
            target_duration_sec=target_duration,
            run_id=context.run_id,
            chapter_id=str(context.run_record.get("chapter_id") or ""),
        )

    @staticmethod
    def _has_blocking(gate: Any) -> bool:
        diagnostics = getattr(gate, "diagnostics", None)
        if diagnostics is None and isinstance(gate, Mapping):
            diagnostics = gate.get("diagnostics")
        return any(
            (item.get("level") if isinstance(item, Mapping) else getattr(item, "level", None))
            == "blocking"
            for item in (diagnostics or [])
        )

    @staticmethod
    def _model_dump(value: Any) -> dict[str, Any]:
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            raise TypeError("阶段结果必须是 Pydantic 模型")
        dumped = model_dump(mode="json")
        if not isinstance(dumped, dict):
            raise TypeError("阶段模型 dump 必须返回对象")
        return dumped

    def _mark_generation_failure(self, context: _ExecutionContext, exc: Exception) -> None:
        message = _error_text(exc)
        try:
            with self.database.engine.begin() as connection:
                current_job = self._load_job(connection, context.job_id)
                current_run = self._load_run(connection, context.run_id)
                current_stage = self._load_stage_by_id(
                    connection,
                    context.stage_id,
                    context.run_id,
                )
                if current_job is None or current_run is None or current_stage is None:
                    return
                now = utc_now()
                connection.execute(
                    update(job)
                    .where(
                        job.c.id == context.job_id,
                        job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                        job.c.updated_at == current_job["updated_at"],
                    )
                    .values(
                        status="failed",
                        error=message,
                        message="任务失败",
                        finished_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == context.stage_id,
                        workflow_stage.c.workflow_run_id == context.run_id,
                        workflow_stage.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                        workflow_stage.c.updated_at == current_stage["updated_at"],
                    )
                    .values(status="failed", error=message, finished_at=now, updated_at=now)
                )
                connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == context.run_id,
                        workflow_run.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                        workflow_run.c.revision == current_run["revision"],
                        workflow_run.c.updated_at == current_run["updated_at"],
                    )
                    .values(
                        status="failed",
                        revision=workflow_run.c.revision + 1,
                        error=message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
        except Exception:
            # 原始 Provider/归一化/门禁异常必须继续向调用方抛出。
            return

    @staticmethod
    def _result_conflict_message(reason: str) -> str:
        return _error_text(f"动态阶段结果提交被并发状态或签名变化阻止：{reason}")

    def _mark_result_conflict_in_connection(
        self,
        connection: Any,
        current_job: Mapping[str, Any] | None,
        current_stage: Mapping[str, Any] | None,
        reason: str,
    ) -> None:
        message = self._result_conflict_message(reason)
        now = utc_now()
        if current_job is not None:
            connection.execute(
                update(job)
                .where(
                    job.c.id == current_job["id"],
                    job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                    job.c.updated_at == current_job["updated_at"],
                )
                .values(
                    status="interrupted",
                    error=message,
                    message="任务因结果提交冲突而中断",
                    finished_at=now,
                    updated_at=now,
                )
            )
        if current_stage is not None and current_stage.get("status") not in {
            "confirmed",
            "cancelled",
        }:
            connection.execute(
                update(workflow_stage)
                .where(
                    workflow_stage.c.id == current_stage["id"],
                    workflow_stage.c.status == current_stage.get("status"),
                    workflow_stage.c.updated_at == current_stage["updated_at"],
                )
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

    def _mark_result_conflict(self, context: _ExecutionContext, reason: str) -> None:
        try:
            with self.database.engine.begin() as connection:
                current_job = self._load_job(connection, context.job_id)
                current_stage = self._load_stage_by_id(
                    connection,
                    context.stage_id,
                    context.run_id,
                )
                self._mark_result_conflict_in_connection(
                    connection,
                    current_job,
                    current_stage,
                    reason,
                )
        except Exception:
            return

    def _result_reason(
        self,
        connection: Any,
        context: _ExecutionContext,
        current_job: Mapping[str, Any] | None,
        current_run: Mapping[str, Any] | None,
        current_stage: Mapping[str, Any] | None,
    ) -> str | None:
        if current_job is None or current_run is None or current_stage is None:
            return "关联记录已不存在"
        if current_job.get("status") in _PROTECTED_JOB_STATUSES:
            return f"Job 当前状态为 {current_job.get('status')!r}"
        if current_job.get("status") not in _EXECUTABLE_JOB_STATUSES:
            return f"Job 当前状态为 {current_job.get('status')!r}"
        if current_run.get("status") in _PROTECTED_WORKFLOW_STATUSES:
            return f"run 当前状态为 {current_run.get('status')!r}"
        if current_run.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES:
            return f"run 当前状态为 {current_run.get('status')!r}"
        if current_stage.get("status") in {"confirmed", "cancelled"}:
            return f"stage 当前状态为 {current_stage.get('status')!r}"
        if current_stage.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES:
            return f"stage 当前状态为 {current_stage.get('status')!r}"
        if current_job.get("workflow_run_id") != context.run_id:
            return "Job 的 run 关联已变化"
        if current_job.get("workflow_stage_id") != context.stage_id:
            return "Job 的 stage 关联已变化"
        if current_stage.get("workflow_run_id") != context.run_id:
            return "stage 的 run 关联已变化"
        latest = self._latest_stage(connection, context.run_id, context.stage_key)
        if latest is None or latest.get("id") != context.stage_id:
            return "stage 已不是当前最新 attempt"
        for prerequisite_key in get_stage_prerequisites(context.stage_key):
            prerequisite = self._latest_stage(connection, context.run_id, prerequisite_key)
            if prerequisite is None or prerequisite.get("status") != "confirmed":
                return f"阶段前置确认已变化：{prerequisite_key}"
        if current_job.get("input_signature") != context.job_record.get("input_signature"):
            return "Job input_signature 已变化"
        if current_stage.get("input_signature") != context.stage_record.get("input_signature"):
            return "stage input_signature 已变化"
        if current_run.get("input_signature") != context.run_record.get("input_signature"):
            return "run input_signature 已变化"
        if self._signature_mismatch(current_job, current_stage):
            return "Job 与 stage 的 input_signature 已不一致"
        return None

    async def _persist_result(
        self,
        context: _ExecutionContext,
        normalized: Any,
        config: Mapping[str, Any],
        gate: Any,
        raw_payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_payload = self._model_dump(normalized)
        gate_payload = self._model_dump(gate)
        provider_id = config.get("id") or config.get("provider_id") or None
        provider_model = config.get("model") or config.get("provider_model") or None
        artifact_meta = normalized_payload.get("artifact_meta")
        if not isinstance(artifact_meta, Mapping):
            raise TypeError("归一化产物缺少 artifact_meta")
        output_refs = [
            {
                "provider_id": provider_id,
                "provider_model": provider_model,
                "capture_mode": "normalized_provider_model",
                "schema_version": artifact_meta.get("schema_version"),
                "normalizer_version": artifact_meta.get("normalizer_version"),
                "gate_version": stage_gates.GATE_VERSION,
            }
        ]
        if context.stage_key in {"3-script", "4-storyboard"}:
            gate_provenance = {
                "stage_key": context.stage_key,
                "gate_version": stage_gates.GATE_VERSION,
                "input_signature": artifact_meta.get("input_signature"),
                "source_attempt": artifact_meta.get("source_attempt"),
                "provider_id": artifact_meta.get("provider_id"),
                "provider_model": artifact_meta.get("provider_model"),
                "checked_at": gate_payload.get("checked_at"),
                "diagnostics": gate_payload.get("diagnostics", []),
                "diagnostic_codes": [
                    item.get("code")
                    for item in gate_payload.get("diagnostics", [])
                    if isinstance(item, Mapping)
                ],
            }
            output_refs[0]["gate_provenance"] = gate_provenance
        target_status = "blocked" if self._has_blocking(gate) else "ready"
        result_data = {
            "stage_id": context.stage_id,
            "stage_key": context.stage_key,
            "stage_status": target_status,
            "gate_status": gate_payload.get("status"),
        }
        raw_output = {
            "capture_mode": "normalized_provider_model",
            "provider_output": raw_payload,
        }
        if context.stage_key in {"3-script", "4-storyboard"}:
            raw_output["gate_version"] = stage_gates.GATE_VERSION
            raw_output["gate_provenance"] = output_refs[0].get("gate_provenance", {})
        conflict_message: str | None = None
        try:
            with self.database.engine.begin() as connection:
                current_job = self._load_job(connection, context.job_id)
                current_run = self._load_run(connection, context.run_id)
                current_stage = self._load_stage_by_id(
                    connection,
                    context.stage_id,
                    context.run_id,
                )
                reason = self._result_reason(
                    connection,
                    context,
                    current_job,
                    current_run,
                    current_stage,
                )
                if reason is not None:
                    self._mark_result_conflict_in_connection(
                        connection,
                        current_job,
                        current_stage,
                        reason,
                    )
                    conflict_message = self._result_conflict_message(reason)
                else:
                    assert current_job is not None
                    assert current_run is not None
                    assert current_stage is not None
                    now = utc_now()
                    stage_update = connection.execute(
                        update(workflow_stage)
                        .where(
                            workflow_stage.c.id == context.stage_id,
                            workflow_stage.c.workflow_run_id == context.run_id,
                            workflow_stage.c.attempt == context.stage_record["attempt"],
                            workflow_stage.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                            workflow_stage.c.updated_at == current_stage["updated_at"],
                            _signature_condition(
                                workflow_stage.c.input_signature,
                                context.stage_record.get("input_signature"),
                            ),
                        )
                        .values(
                            status=target_status,
                            raw_output_json=json_dumps(raw_output),
                            normalized_output_json=json_dumps(normalized_payload),
                            draft_output_json=json_dumps(normalized_payload),
                            gate_result_json=json_dumps(gate_payload),
                            output_refs_json=json_dumps(output_refs),
                            error="",
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                    if stage_update.rowcount != 1:
                        raise _ResultWriteConflict("stage 条件更新未命中")

                    run_update = connection.execute(
                        update(workflow_run)
                        .where(
                            workflow_run.c.id == context.run_id,
                            workflow_run.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                            workflow_run.c.revision == current_run["revision"],
                            workflow_run.c.updated_at == current_run["updated_at"],
                            _signature_condition(
                                workflow_run.c.input_signature,
                                context.run_record.get("input_signature"),
                            ),
                        )
                        .values(
                            status=target_status,
                            revision=workflow_run.c.revision + 1,
                            error="",
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                    if run_update.rowcount != 1:
                        raise _ResultWriteConflict("run 条件更新未命中")

                    job_update = connection.execute(
                        update(job)
                        .where(
                            job.c.id == context.job_id,
                            job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                            job.c.workflow_run_id == context.run_id,
                            job.c.workflow_stage_id == context.stage_id,
                            job.c.updated_at == current_job["updated_at"],
                            _signature_condition(
                                job.c.input_signature,
                                context.job_record.get("input_signature"),
                            ),
                        )
                        .values(
                            status="completed",
                            progress_current=job.c.progress_total,
                            message="任务完成",
                            result_json=json_dumps(result_data),
                            error="",
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                    if job_update.rowcount != 1:
                        raise _ResultWriteConflict("Job 条件更新未命中")
        except _ResultWriteConflict as exc:
            reason = str(exc)
            self._mark_result_conflict(context, reason)
            raise StageExecutionError(
                self._result_conflict_message(reason),
                job_id=context.job_id,
                stage_key=context.stage_key,
            ) from exc

        if conflict_message is not None:
            raise StageExecutionError(
                conflict_message,
                job_id=context.job_id,
                stage_key=context.stage_key,
            )
        return result_data

    async def execute_job(self, job_record: Mapping[str, Any]) -> dict[str, Any]:
        """预检、调用文本 Provider、归一化并原子提交阶段结果。"""

        if not isinstance(job_record, Mapping):
            raise StageExecutionError("Job 记录必须是对象")
        context = self._preflight(job_record)
        try:
            normalized, config, gate, raw_payload = await self._generate_and_normalize(
                context,
                job_record,
            )
        except Exception as exc:
            self._mark_generation_failure(context, exc)
            raise
        return await self._persist_result(
            context,
            normalized,
            config,
            gate,
            raw_payload,
        )


__all__ = ["DYNAMIC_STAGE_KEYS", "DynamicStageService", "StageExecutionError"]
