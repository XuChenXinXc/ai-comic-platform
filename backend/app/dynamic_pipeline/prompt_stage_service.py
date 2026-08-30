"""动态 Prompt 阶段 5a/5b 的纯编译执行服务。

5a/5b 只把已经确认的阶段产物编译成 Prompt bundle，并将 bundle 保存到
``workflow_stage``。本服务不解析章节正文、不调用 Provider，也不创建图片或视频
任务；媒体入队由后续阶段服务负责。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update

from app.db import Database
from app.dynamic_pipeline import materializer as dynamic_materializer
from app.dynamic_pipeline import normalizers
from app.dynamic_pipeline.diagnostics import diagnostic, diagnostics_status
from app.dynamic_pipeline.prompt_compiler import COMPILER_VERSION
from app.dynamic_pipeline.prompt_ir import AssetVisualAnchor
from app.dynamic_pipeline.prompt_stage import (
    PromptStageError,
    PromptStageOutput,
    compile_image_prompt_stage,
    compile_video_prompt_stage,
    prompt_stage_override_hash,
    stage_output_to_stage_json,
)
from app.dynamic_pipeline.provider_capabilities import normalize_capabilities
from app.dynamic_pipeline.reference_plan import (
    build_reference_plan,
    calculate_reference_plan_snapshot_hash,
)
from app.dynamic_pipeline.reference_resolver import adapt_reference_bindings
from app.dynamic_pipeline.schemas import CharacterBiosOutput, StageGateResult
from app.dynamic_pipeline.storyboard_stage import StoryboardOutput
from app.dynamic_pipeline.visual_anchor import (
    anchor_version,
    asset_profile_to_anchor,
    build_visual_style_spec,
    character_bio_to_anchor,
)
from app.models.tables import (
    asset,
    asset_image,
    job,
    keyframe_version,
    project,
    shot,
    video_version,
    workflow_run,
    workflow_stage,
)
from app.services.utils import json_dumps, json_loads, sanitize_error_message, utc_now

PROMPT_STAGE_KEYS = ("5a-image-prompts", "5b-video-prompts")

_EXECUTABLE_JOB_STATUSES = frozenset({"queued", "running"})
_EXECUTABLE_WORKFLOW_STATUSES = frozenset({"queued", "running"})
_PROTECTED_WORKFLOW_STATUSES = frozenset({"cancelled", "stale", "confirmed", "blocked"})
_PROTECTED_JOB_STATUSES = frozenset({"completed", "cancelled", "interrupted"})
_PROMPT_RESULT_STAGE_STATUSES = frozenset(
    {"ready", "blocked", "confirmed", "failed", "interrupted", "partial_failed"}
)

_DEFAULT_PROVIDER_IDS = {
    "5a-image-prompts": ("dynamic-default-image-provider", "dynamic-default-image-model"),
    "5b-video-prompts": ("dynamic-default-video-provider", "dynamic-default-video-model"),
}


class PromptStageExecutionError(RuntimeError):
    """Prompt 阶段预检、编译或结果提交不满足并发安全条件。"""

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


def _copy(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return copy.deepcopy(value.model_dump(mode="python"))
    return copy.deepcopy(value)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _decode(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return _copy(value)
    return json_loads(value, _copy(default))


def _decode_job(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    record["input_data"] = _decode(record.get("input_json"), {})
    record["result_data"] = _decode(record.get("result_json"), {})
    return record


def _decode_run(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    record["source_snapshot"] = _decode(record.get("source_snapshot_json"), {})
    record["config_snapshot"] = _decode(record.get("config_snapshot_json"), {})
    record["asset_mapping"] = _decode(record.get("asset_mapping_json"), [])
    return record


def _decode_stage(row: Any) -> dict[str, Any] | None:
    record = _row_dict(row)
    if record is None:
        return None
    for column, public_name, default in (
        ("raw_output_json", "raw_output", {}),
        ("normalized_output_json", "normalized_output", {}),
        ("draft_output_json", "draft_output", {}),
        ("output_refs_json", "output_refs", []),
        ("gate_result_json", "gate_result", {}),
    ):
        record[public_name] = _decode(record.get(column), default)
    return record


def _non_empty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _signature_condition(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _error_text(value: Any) -> str:
    return sanitize_error_message(value) or "动态 Prompt 阶段执行失败"


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _dedupe_diagnostics(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        try:
            key = json_dumps(value.model_dump(mode="json"))
        except AttributeError:
            key = json_dumps(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


class DynamicPromptStageService:
    """执行 5a/5b 的数据库读取、纯编译和阶段结果提交。"""

    def __init__(self, database: Database, provider_service: Any | None = None) -> None:
        self.database = database
        # 保留构造参数以便调用方统一注入服务，但 Prompt 阶段绝不使用它。
        self.provider_service = provider_service

    # ------------------------------------------------------------------
    # 数据库读取和状态预检
    # ------------------------------------------------------------------
    @staticmethod
    def _job(connection: Any, job_id: str) -> dict[str, Any] | None:
        return _decode_job(connection.execute(select(job).where(job.c.id == job_id)).first())

    @staticmethod
    def _run(connection: Any, run_id: str) -> dict[str, Any] | None:
        return _decode_run(
            connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
        )

    @staticmethod
    def _stage_by_id(
        connection: Any,
        stage_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        statement = select(workflow_stage).where(workflow_stage.c.id == stage_id)
        if run_id is not None:
            statement = statement.where(workflow_stage.c.workflow_run_id == run_id)
        return _decode_stage(connection.execute(statement).first())

    @staticmethod
    def _latest_stage(
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

    @staticmethod
    def _prompt_barrier_status(connection: Any, run_id: str) -> str | None:
        statuses: list[str] = []
        for stage_key in PROMPT_STAGE_KEYS:
            row = connection.execute(
                select(workflow_stage.c.status)
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
            if row is None:
                return None
            statuses.append(str(row.status))

        if any(status not in _PROMPT_RESULT_STAGE_STATUSES for status in statuses):
            return None
        if "blocked" in statuses:
            return "blocked"
        if any(status in {"failed", "partial_failed"} for status in statuses):
            return "partial_failed"
        if "interrupted" in statuses:
            return "interrupted"
        return "ready"

    @staticmethod
    def _mark_job_only(
        connection: Any,
        current_job: Mapping[str, Any],
        *,
        status: str,
        error: str,
        message: str,
    ) -> None:
        now = utc_now()
        connection.execute(
            update(job)
            .where(
                job.c.id == current_job["id"],
                job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                job.c.updated_at == current_job["updated_at"],
            )
            .values(
                status=status,
                error=_error_text(error),
                message=message,
                finished_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _mark_signature_mismatch(
        connection: Any,
        current_job: Mapping[str, Any],
        current_run: Mapping[str, Any],
        current_stage: Mapping[str, Any],
    ) -> None:
        message = _error_text(
            "动态 Prompt Job 与 stage 的分支 input_signature 不一致；"
            "run input_signature 仅表示根输入，未调用 Provider，结果已作废"
        )
        now = utc_now()
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
        DynamicPromptStageService._mark_delivery_stale(connection, current_run, message)

    @staticmethod
    def _mark_delivery_stale(
        connection: Any,
        current_run: Mapping[str, Any],
        message: str,
    ) -> None:
        delivery = connection.execute(
            select(workflow_stage).where(
                workflow_stage.c.workflow_run_id == current_run["id"],
                workflow_stage.c.stage_key == "6-delivery",
            )
        ).first()
        if delivery is None:
            return
        delivery_record = dict(delivery._mapping)
        now = utc_now()
        connection.execute(
            update(job)
            .where(
                job.c.workflow_stage_id == delivery_record["id"],
                job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
            )
            .values(
                status="interrupted",
                error=message,
                message="任务因 Prompt 输入快照变化而中断",
                finished_at=now,
                updated_at=now,
            )
        )
        if delivery_record.get("status") not in {"confirmed", "cancelled", "stale"}:
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == delivery_record["id"])
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

    @staticmethod
    def _claim(
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
                    message="动态 Prompt 阶段执行中",
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise PromptStageExecutionError(
                    "Prompt Job 在执行前发生并发状态变化",
                    job_id=str(current_job.get("id")),
                    stage_key=str(current_stage.get("stage_key")),
                )

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
                raise PromptStageExecutionError(
                    "Prompt stage 在执行前发生并发状态变化",
                    job_id=str(current_job.get("id")),
                    stage_key=str(current_stage.get("stage_key")),
                )

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
                current_status = connection.scalar(
                    select(workflow_run.c.status).where(workflow_run.c.id == current_run["id"])
                )
                if current_status != "running":
                    raise PromptStageExecutionError(
                        "Prompt run 在执行前发生并发状态变化",
                        job_id=str(current_job.get("id")),
                        stage_key=str(current_stage.get("stage_key")),
                    )

    def _preflight(self, job_record: Mapping[str, Any]) -> _ExecutionContext:
        job_id = _text(job_record.get("id"))
        if not job_id:
            raise PromptStageExecutionError("Job 记录缺少 id")

        rejection: PromptStageExecutionError | None = None
        context: _ExecutionContext | None = None
        with self.database.engine.begin() as connection:
            current_job = self._job(connection, job_id)
            if current_job is None:
                raise PromptStageExecutionError(f"Job 不存在：{job_id}", job_id=job_id)
            run_id = _text(current_job.get("workflow_run_id"))
            stage_id = _text(current_job.get("workflow_stage_id"))
            if not run_id or not stage_id:
                message = "动态 Prompt Job 缺少 workflow_run_id 或 workflow_stage_id"
                self._mark_job_only(
                    connection,
                    current_job,
                    status="failed",
                    error=message,
                    message="任务因动态关联缺失而失败",
                )
                rejection = PromptStageExecutionError(message, job_id=job_id)
            else:
                current_run = self._run(connection, run_id)
                current_stage = self._stage_by_id(connection, stage_id, run_id)
                if current_run is None:
                    message = f"动态 Job 关联的 run 不存在：{run_id}"
                    self._mark_job_only(
                        connection,
                        current_job,
                        status="failed",
                        error=message,
                        message="任务因动态关联缺失而失败",
                    )
                    rejection = PromptStageExecutionError(message, job_id=job_id)
                elif current_stage is None:
                    message = f"动态 Job 关联的 stage 不存在或不属于该 run：{stage_id}"
                    self._mark_job_only(
                        connection,
                        current_job,
                        status="failed",
                        error=message,
                        message="任务因动态关联缺失而失败",
                    )
                    rejection = PromptStageExecutionError(message, job_id=job_id)
                elif (
                    current_job.get("project_id") != current_run.get("project_id")
                    or current_job.get("chapter_id") != current_run.get("chapter_id")
                    or current_stage.get("workflow_run_id") != current_run.get("id")
                ):
                    message = "动态 Prompt Job、run 与 stage 的关联不一致"
                    self._mark_job_only(
                        connection,
                        current_job,
                        status="failed",
                        error=message,
                        message="任务因动态关联不一致而失败",
                    )
                    rejection = PromptStageExecutionError(message, job_id=job_id)
                else:
                    stage_key = _text(current_stage.get("stage_key"))
                    if stage_key not in PROMPT_STAGE_KEYS:
                        message = f"当前阶段不在 Prompt 阶段范围内：{stage_key!r}"
                        self._mark_job_only(
                            connection,
                            current_job,
                            status="failed",
                            error=message,
                            message="任务因阶段类型不受支持而失败",
                        )
                        rejection = PromptStageExecutionError(
                            message,
                            job_id=job_id,
                            stage_key=stage_key,
                        )
                    elif (
                        current_job.get("status") in _PROTECTED_JOB_STATUSES
                        or current_job.get("status") not in _EXECUTABLE_JOB_STATUSES
                    ):
                        message = f"Job 当前状态不可执行：{current_job.get('status')!r}"
                        rejection = PromptStageExecutionError(
                            message,
                            job_id=job_id,
                            stage_key=stage_key,
                        )
                    elif (
                        current_run.get("status") in _PROTECTED_WORKFLOW_STATUSES
                        or current_run.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES
                    ):
                        message = f"run 当前状态不可执行：{current_run.get('status')!r}"
                        self._mark_job_only(
                            connection,
                            current_job,
                            status="interrupted",
                            error=message,
                            message="任务因 run 状态不可执行而中断",
                        )
                        rejection = PromptStageExecutionError(
                            message,
                            job_id=job_id,
                            stage_key=stage_key,
                        )
                    elif (
                        current_stage.get("status") in _PROTECTED_WORKFLOW_STATUSES
                        or current_stage.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES
                    ):
                        message = f"stage 当前状态不可执行：{current_stage.get('status')!r}"
                        self._mark_job_only(
                            connection,
                            current_job,
                            status="interrupted",
                            error=message,
                            message="任务因 stage 状态不可执行而中断",
                        )
                        rejection = PromptStageExecutionError(
                            message,
                            job_id=job_id,
                            stage_key=stage_key,
                        )
                    else:
                        latest_stage = self._latest_stage(connection, run_id, stage_key)
                        if latest_stage is None or latest_stage.get("id") != current_stage.get(
                            "id"
                        ):
                            message = f"stage 不是当前最新 attempt：{stage_key}"
                            self._mark_job_only(
                                connection,
                                current_job,
                                status="interrupted",
                                error=message,
                                message="任务因阶段 attempt 过期而中断",
                            )
                            rejection = PromptStageExecutionError(
                                message,
                                job_id=job_id,
                                stage_key=stage_key,
                            )
                        else:
                            job_signature = _text(current_job.get("input_signature"))
                            stage_signature = _text(current_stage.get("input_signature"))
                            snapshot_marker = any(
                                isinstance(item, Mapping)
                                and item.get("capture_mode") == "prompt_input_snapshot"
                                for item in _as_list(current_stage.get("output_refs"))
                            )
                            snapshot_mismatch = False
                            if snapshot_marker and job_signature and stage_signature:
                                try:
                                    current_signature = self._current_prompt_input_signature(
                                        connection,
                                        run=current_run,
                                        stage=current_stage,
                                        job_record=current_job,
                                    )
                                    snapshot_mismatch = current_signature != stage_signature
                                except (PromptStageExecutionError, TypeError, ValueError):
                                    snapshot_mismatch = True
                            if (
                                not job_signature
                                or not stage_signature
                                or job_signature != stage_signature
                                or snapshot_mismatch
                            ):
                                self._mark_signature_mismatch(
                                    connection,
                                    current_job,
                                    current_run,
                                    current_stage,
                                )
                                message = (
                                    "Job 与 stage 的分支 input_signature 不一致，"
                                    "已中断 Job 并标记 stage stale；run 根签名不参与分支等值比较"
                                )
                                rejection = PromptStageExecutionError(
                                    message,
                                    job_id=job_id,
                                    stage_key=stage_key,
                                )
                            else:
                                storyboard_stage = self._latest_stage(
                                    connection,
                                    run_id,
                                    "4-storyboard",
                                )
                                if (
                                    storyboard_stage is None
                                    or storyboard_stage.get("status") != "confirmed"
                                ):
                                    message = "阶段前置确认未满足：4-storyboard；未调用 Provider"
                                    self._mark_job_only(
                                        connection,
                                        current_job,
                                        status="failed",
                                        error=message,
                                        message="任务因阶段 4 未确认而失败",
                                    )
                                    rejection = PromptStageExecutionError(
                                        "阶段前置确认未满足：4-storyboard",
                                        job_id=job_id,
                                        stage_key=stage_key,
                                    )
                                else:
                                    self._claim(
                                        connection,
                                        current_job,
                                        current_run,
                                        current_stage,
                                    )
                                    claimed_job = self._job(connection, job_id)
                                    claimed_run = self._run(connection, run_id)
                                    claimed_stage = self._stage_by_id(connection, stage_id, run_id)
                                    if (
                                        claimed_job is None
                                        or claimed_run is None
                                        or claimed_stage is None
                                    ):
                                        raise PromptStageExecutionError(
                                            "Prompt 阶段执行记录在预检提交后不存在",
                                            job_id=job_id,
                                            stage_key=stage_key,
                                        )
                                    context = _ExecutionContext(
                                        job_record=claimed_job,
                                        run_record=claimed_run,
                                        stage_record=claimed_stage,
                                    )

        if rejection is not None:
            raise rejection
        if context is None:
            raise PromptStageExecutionError("Prompt 阶段预检未生成执行上下文", job_id=job_id)
        return context

    # ------------------------------------------------------------------
    # Prompt 输入快照
    # ------------------------------------------------------------------
    @staticmethod
    def _stage_content(stage: Mapping[str, Any]) -> Mapping[str, Any]:
        draft = stage.get("draft_output")
        if isinstance(draft, Mapping) and draft:
            return draft
        normalized = stage.get("normalized_output")
        return normalized if isinstance(normalized, Mapping) else {}

    @staticmethod
    def _parse_storyboard(stage: Mapping[str, Any]) -> StoryboardOutput:
        try:
            return StoryboardOutput.model_validate(
                _copy(DynamicPromptStageService._stage_content(stage))
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise PromptStageExecutionError("阶段 4 输出不符合 StoryboardOutput 合同") from exc

    @staticmethod
    def _parse_character_bios(stage: Mapping[str, Any] | None) -> CharacterBiosOutput | None:
        if stage is None or stage.get("status") != "confirmed":
            return None
        content = DynamicPromptStageService._stage_content(stage)
        if not content:
            return None
        try:
            return CharacterBiosOutput.model_validate(_copy(content))
        except (TypeError, ValueError, ValidationError) as exc:
            raise PromptStageExecutionError("阶段 1 输出不符合 CharacterBiosOutput 合同") from exc

    @staticmethod
    def _asset_mapping(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, Mapping):
            if "asset_id" in value or "workflow_asset_id" in value:
                values = (value,)
            else:
                values = (
                    {
                        "workflow_asset_id": key,
                        "asset_id": item,
                    }
                    if not isinstance(item, Mapping)
                    else {
                        **dict(item),
                        "workflow_asset_id": dict(item).get("workflow_asset_id") or key,
                    }
                    for key, item in value.items()
                )
        elif isinstance(value, list | tuple):
            values = value
        else:
            return []
        return [dict(item) for item in values if isinstance(item, Mapping)]

    @staticmethod
    def _json_mapping(value: Any) -> dict[str, Any]:
        parsed = _decode(value, {})
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _merge_character_anchor(
        base: AssetVisualAnchor,
        character: Any,
        *,
        asset_id: str,
        asset_revision: Any,
    ) -> AssetVisualAnchor:
        try:
            bio_anchor = character_bio_to_anchor(
                character,
                asset_id=asset_id,
                asset_revision=asset_revision,
                approved_references=base.approved_references,
            )
        except (TypeError, ValueError, ValidationError):
            return base

        updates: dict[str, Any] = {}
        for field in (
            "visual_facts",
            "immutable_features",
            "forbidden_changes",
            "accessories",
            "variants",
        ):
            values = list(getattr(base, field))
            for item in getattr(bio_anchor, field):
                if item not in values:
                    values.append(item)
            updates[field] = values
        for field in (
            "makeup_keyword_string",
            "face",
            "hair",
            "body",
            "costume",
            "current_variant",
        ):
            value = getattr(bio_anchor, field)
            if _non_empty(value):
                updates[field] = value
        details = dict(base.details)
        details["character_bio"] = _copy(bio_anchor.details)
        updates["details"] = details
        updates["character_details"] = _copy(bio_anchor.character_details)
        return base.model_copy(update=updates, deep=True)

    @classmethod
    def _build_anchors(
        cls,
        connection: Any,
        *,
        run: Mapping[str, Any],
        storyboard: StoryboardOutput,
        character_bios: CharacterBiosOutput | None,
    ) -> dict[str, AssetVisualAnchor]:
        explicit_ids: set[str] = set()
        for storyboard_shot in storyboard.shots:
            explicit_ids.update(
                _text(value)
                for value in (
                    storyboard_shot.scene_asset_id,
                    *storyboard_shot.character_asset_ids,
                    *storyboard_shot.prop_asset_ids,
                )
                if _text(value)
            )
        mappings = cls._asset_mapping(run.get("asset_mapping"))
        mapping_by_workflow_id: dict[str, dict[str, Any]] = {}
        for item in mappings:
            asset_id = _text(item.get("asset_id"))
            workflow_id = _text(item.get("workflow_asset_id"))
            if asset_id:
                explicit_ids.add(asset_id)
            if workflow_id:
                mapping_by_workflow_id[workflow_id] = item

        rows = connection.execute(
            select(asset).where(asset.c.project_id == run.get("project_id"))
        ).all()
        assets_by_id = {
            _text(row.id): dict(row._mapping) for row in rows if _text(row.id) in explicit_ids
        }

        bios_by_asset_id: dict[str, Any] = {}
        if character_bios is not None:
            for character in character_bios.characters:
                character_key = _text(character.basic_info.character_key)
                if not character_key:
                    continue
                workflow_ids = {
                    character_key,
                    f"dynamic-asset:{run.get('id')}:{character_key}",
                }
                mapping = next(
                    (
                        item
                        for workflow_id, item in mapping_by_workflow_id.items()
                        if workflow_id in workflow_ids and _text(item.get("asset_id"))
                    ),
                    None,
                )
                if mapping is not None:
                    bios_by_asset_id[_text(mapping.get("asset_id"))] = character

        anchors: dict[str, AssetVisualAnchor] = {}
        for asset_id in sorted(explicit_ids):
            row = assets_by_id.get(asset_id)
            if row is None:
                # 不按资产名称或工作流资产键补造 anchor；编译器会返回 blocking。
                continue
            profile = cls._json_mapping(row.get("profile_json"))
            visual_anchor = cls._json_mapping(row.get("visual_anchor_json"))
            raw_profile = {
                "asset_id": asset_id,
                "asset_type": row.get("asset_type"),
                "name": row.get("name"),
                "profile_json": profile,
                "visual_anchor_json": visual_anchor,
            }
            try:
                asset_type_value: Any = _text(row.get("asset_type")) or None
                anchor = asset_profile_to_anchor(
                    raw_profile,
                    asset_id=asset_id,
                    asset_revision=row.get("revision"),
                    asset_type=asset_type_value,
                )
            except (TypeError, ValueError, ValidationError):
                continue
            character = bios_by_asset_id.get(asset_id)
            if character is not None:
                anchor = cls._merge_character_anchor(
                    anchor,
                    character,
                    asset_id=asset_id,
                    asset_revision=row.get("revision"),
                )
            anchors[asset_id] = anchor
        return anchors

    @staticmethod
    def _project_root(connection: Any, project_id: str) -> Path | None:
        row = connection.execute(
            select(project.c.storage_root, project.c.storage_name, project.c.storage_layout).where(
                project.c.id == project_id
            )
        ).first()
        if row is None or not _text(row.storage_root):
            return None
        root = Path(str(row.storage_root))
        storage_name = str(row.storage_name or project_id)
        return root / storage_name if row.storage_layout == "categorized" else root / project_id

    @staticmethod
    def _reference_expected_asset_type(purpose: str) -> str | None:
        return {
            "clean_identity": "character",
            "layout_reference": "scene",
            "prop_identity": "prop",
        }.get(purpose)

    @staticmethod
    def _reference_diagnostic(
        code: str,
        message: str,
        path: str,
        details: Mapping[str, Any] | None = None,
    ) -> Any:
        return diagnostic("blocking", code, message, path=path, details=details)

    @classmethod
    def _resolve_reference_plan(
        cls,
        connection: Any,
        value: Any,
        *,
        project_id: str,
        chapter_id: str,
        provider_capabilities: Mapping[str, Any],
        video_stage: bool,
    ) -> dict[str, Any] | None:
        raw = _decode(value, [])
        if raw in (None, [], {}):
            return None
        if isinstance(raw, Mapping):
            bindings = raw.get("slots", raw.get("bindings", raw))
        else:
            bindings = raw
        adapted = adapt_reference_bindings(bindings)
        diagnostics: list[Any] = []
        project_root = cls._project_root(connection, project_id)
        enriched: list[dict[str, Any]] = []
        for index, slot in enumerate(adapted):
            item = dict(slot)
            path = f"slots[{index}]"
            purpose = _text(item.get("purpose"))
            expected_type = cls._reference_expected_asset_type(purpose)
            raw_binding = (
                bindings[index]
                if isinstance(bindings, list)
                and index < len(bindings)
                and isinstance(bindings[index], Mapping)
                else {}
            )
            declared_type = _text(
                raw_binding.get("asset_type")
                or raw_binding.get("reference_type")
                or raw_binding.get("type")
            )
            if declared_type and expected_type and declared_type != expected_type:
                diagnostics.append(
                    cls._reference_diagnostic(
                        "reference_wrong_type",
                        "raw reference binding 类型与用途不匹配",
                        f"{path}.type",
                        {"expected_asset_type": expected_type, "actual_asset_type": declared_type},
                    )
                )
            asset_id = _text(item.get("asset_id"))
            if asset_id:
                asset_row = connection.execute(
                    select(asset).where(asset.c.id == asset_id)
                ).first()
                if asset_row is None:
                    diagnostics.append(
                        cls._reference_diagnostic(
                            "reference_asset_missing",
                            f"参考图绑定的 asset 不存在：{asset_id}",
                            f"{path}.asset_id",
                            {"asset_id": asset_id},
                        )
                    )
                else:
                    asset_record = dict(asset_row._mapping)
                    if asset_record.get("project_id") != project_id:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_project_mismatch",
                                f"参考图 asset 不属于当前项目：{asset_id}",
                                f"{path}.asset_id",
                            )
                        )
                    if expected_type and asset_record.get("asset_type") != expected_type:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_wrong_type",
                                f"参考图 asset 类型不匹配：{asset_id}",
                                f"{path}.asset_id",
                                {
                                    "expected_asset_type": expected_type,
                                    "actual_asset_type": asset_record.get("asset_type"),
                                },
                            )
                        )

            image_id = _text(item.get("image_version_id"))
            if image_id:
                image_row = connection.execute(
                    select(asset_image).where(asset_image.c.id == image_id)
                ).first()
                if image_row is None:
                    diagnostics.append(
                        cls._reference_diagnostic(
                            "reference_image_missing",
                            f"asset_image 不存在：{image_id}",
                            f"{path}.image_version_id",
                        )
                    )
                else:
                    image = dict(image_row._mapping)
                    image_asset = connection.execute(
                        select(asset).where(asset.c.id == image.get("asset_id"))
                    ).first()
                    image_asset_record = dict(image_asset._mapping) if image_asset else {}
                    if image_asset_record.get("project_id") != project_id:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_project_mismatch",
                                f"asset_image 不属于当前项目：{image_id}",
                                f"{path}.image_version_id",
                            )
                        )
                    if not bool(image.get("approved")):
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_unapproved",
                                f"asset_image 未获批准：{image_id}",
                                f"{path}.image_version_id",
                            )
                        )
                    if expected_type and image_asset_record.get("asset_type") != expected_type:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_wrong_type",
                                f"asset_image 类型不匹配：{image_id}",
                                f"{path}.image_version_id",
                            )
                        )
                    image_purpose = _text(image.get("reference_purpose"))
                    if image_purpose and image_purpose != purpose:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_wrong_purpose",
                                f"asset_image 用途不匹配：{image_id}",
                                f"{path}.purpose",
                                {"expected": purpose, "actual": image_purpose},
                            )
                        )
                    if (
                        not _text(image.get("file_path"))
                        and not _text(image.get("provider_url"))
                        and not _text(item.get("relative_path"))
                        and not _text(item.get("provider_url"))
                    ):
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_source_missing",
                                f"asset_image 缺少可访问来源：{image_id}",
                                f"{path}.image_version_id",
                            )
                        )
                    if _text(image.get("file_path")):
                        item["relative_path"] = _text(image.get("file_path"))
                    if _text(image.get("provider_url")):
                        item["provider_url"] = _text(image.get("provider_url"))

            keyframe_id = _text(item.get("keyframe_id"))
            if keyframe_id:
                keyframe_row = connection.execute(
                    select(keyframe_version).where(keyframe_version.c.id == keyframe_id)
                ).first()
                if keyframe_row is None:
                    diagnostics.append(
                        cls._reference_diagnostic(
                            "reference_keyframe_missing",
                            f"keyframe_version 不存在：{keyframe_id}",
                            f"{path}.keyframe_id",
                        )
                    )
                else:
                    keyframe = dict(keyframe_row._mapping)
                    shot_row = connection.execute(
                        select(shot).where(shot.c.id == keyframe.get("shot_id"))
                    ).first()
                    shot_record = dict(shot_row._mapping) if shot_row else {}
                    if (
                        shot_record.get("project_id") != project_id
                        or shot_record.get("chapter_id") != chapter_id
                    ):
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_project_or_chapter_mismatch",
                                f"keyframe 不属于当前项目/章节：{keyframe_id}",
                                f"{path}.keyframe_id",
                            )
                        )
                    if purpose not in {"keyframe_reference", "previous_end_frame"}:
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_wrong_purpose",
                                "keyframe 只能用于 keyframe_reference 或 previous_end_frame",
                                f"{path}.purpose",
                            )
                        )
                    if (
                        not _text(keyframe.get("file_path"))
                        and not _text(keyframe.get("provider_url"))
                        and not _text(item.get("relative_path"))
                        and not _text(item.get("provider_url"))
                    ):
                        diagnostics.append(
                            cls._reference_diagnostic(
                                "reference_source_missing",
                                f"keyframe 缺少可访问来源：{keyframe_id}",
                                f"{path}.keyframe_id",
                            )
                        )
                    if _text(keyframe.get("file_path")):
                        item["relative_path"] = _text(keyframe.get("file_path"))
                    if _text(keyframe.get("provider_url")):
                        item["provider_url"] = _text(keyframe.get("provider_url"))

            if "approved" in raw_binding:
                raw_approved = raw_binding.get("approved")
                if raw_approved in (False, 0, "0", "false", "False"):
                    diagnostics.append(
                        cls._reference_diagnostic(
                            "reference_unapproved",
                            "raw reference binding 未获批准",
                            f"{path}.approved",
                        )
                    )
            enriched.append(item)

        plan = build_reference_plan(
            enriched,
            project_root=project_root,
            provider_capabilities=provider_capabilities,
            video=video_stage,
        )
        diagnostics.extend(plan.diagnostics)
        gate = diagnostics_status(diagnostics)
        return plan.model_copy(
            update={
                "status": gate.status,
                "diagnostics": list(gate.diagnostics),
                "provider_capability_snapshot": plan.provider_capability_snapshot,
                "snapshot_hash": calculate_reference_plan_snapshot_hash(
                    {
                        **plan.model_dump(mode="python"),
                        "status": gate.status,
                        "diagnostics": gate.diagnostics,
                        "provider_capability_snapshot": plan.provider_capability_snapshot,
                    },
                    video=video_stage,
                ),
            }
        ).model_dump(mode="json")

    @staticmethod
    def _transition(row: Mapping[str, Any]) -> str:
        value = _text(row.get("transition_from_prev"))
        continuity = _decode(row.get("continuity_json"), {})
        if not value or value == "scene_change":
            continuity_mapping = _mapping(continuity)
            value = (
                _text(
                    continuity_mapping.get("transition_from_prev")
                    or continuity_mapping.get("transition_type")
                )
                or value
            )
        return value or "scene_change"

    @classmethod
    def _build_references_and_continuity(
        cls,
        connection: Any,
        *,
        project_id: str,
        chapter_id: str,
        provider_capabilities: Mapping[str, Any],
        video_stage: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        rows = connection.execute(
            select(shot)
            .where(shot.c.chapter_id == chapter_id)
            .order_by(shot.c.shot_order, shot.c.shot_no, shot.c.id)
        ).all()
        shot_records = [dict(row._mapping) for row in rows]
        references: dict[str, Any] = {}
        for row in shot_records:
            external_key = _text(row.get("external_key"))
            if not external_key:
                continue
            reference_plan = cls._resolve_reference_plan(
                connection,
                row.get("reference_bindings_json"),
                project_id=project_id,
                chapter_id=chapter_id,
                provider_capabilities=provider_capabilities,
                video_stage=video_stage,
            )
            if reference_plan is not None:
                references[external_key] = reference_plan

        shot_ids = [_text(row.get("id")) for row in shot_records if _text(row.get("id"))]
        video_by_shot_id: dict[str, dict[str, Any]] = {}
        if shot_ids:
            video_rows = connection.execute(
                select(video_version).where(
                    video_version.c.shot_id.in_(shot_ids),
                    video_version.c.is_current == 1,
                    video_version.c.status == "completed",
                )
            ).all()
            video_by_shot_id = {
                _text(row.shot_id): dict(row._mapping) for row in video_rows if _text(row.shot_id)
            }

        previous_end_frames: dict[str, Any] = {}
        previous_reference_info: dict[str, Any] = {}
        for index, current in enumerate(shot_records):
            external_key = _text(current.get("external_key"))
            if not external_key or cls._transition(current) != "continuous" or index == 0:
                continue
            previous = shot_records[index - 1]
            previous_id = _text(previous.get("id"))
            previous_external_key = _text(previous.get("external_key"))
            video = video_by_shot_id.get(previous_id)
            previous_end_frames[external_key] = (
                _text(video.get("end_frame_path")) if video is not None else ""
            )
            continuity = _decode(current.get("continuity_json"), {})
            continuity_mapping = _mapping(continuity)
            current_reference_plan = cls._resolve_reference_plan(
                connection,
                current.get("reference_bindings_json"),
                project_id=project_id,
                chapter_id=chapter_id,
                provider_capabilities=provider_capabilities,
                video_stage=video_stage,
            )
            info: dict[str, Any] = {
                "previous_shot_id": previous_external_key or previous_id,
                "previous_shot_row_id": previous_id,
                "previous_end_state": _text(previous.get("end_state")) or None,
            }
            previous_continuity = _mapping(_decode(previous.get("continuity_json"), {}))
            if previous_continuity.get("continuity_state"):
                info["previous_continuity_state"] = _copy(
                    previous_continuity["continuity_state"]
                )
            if current_reference_plan is not None:
                previous_slots = [
                    slot
                    for slot in _as_list(current_reference_plan.get("slots"))
                    if isinstance(slot, Mapping)
                    and (
                        "previous" in _text(slot.get("role")).casefold()
                        or _text(slot.get("purpose")) == "previous_end_frame"
                    )
                ]
                if previous_slots:
                    info["reference_plan"] = current_reference_plan
                    info["reference_bindings"] = previous_slots
                    first_previous = previous_slots[0]
                    for key in ("keyframe_id", "image_version_id"):
                        if _text(first_previous.get(key)):
                            info[key] = first_previous[key]
            for key in ("axis", "positioning", "props", "previous_continuity_state"):
                if key in continuity_mapping:
                    info[key] = _copy(continuity_mapping[key])
            previous_reference_info[external_key] = info
        return references, previous_end_frames, previous_reference_info

    # ------------------------------------------------------------------
    # Provider-neutral capability 快照和编译
    # ------------------------------------------------------------------
    @staticmethod
    def _input_data(job_record: Mapping[str, Any]) -> dict[str, Any]:
        value = job_record.get("input_data")
        if isinstance(value, Mapping):
            return dict(value)
        value = _decode(job_record.get("input_json"), {})
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _channel(stage_key: str) -> str:
        return "image" if stage_key == "5a-image-prompts" else "video"

    @classmethod
    def _capability_snapshot(
        cls,
        source: Mapping[str, Any],
        *,
        stage_key: str,
    ) -> Mapping[str, Any] | None:
        channel = cls._channel(stage_key)
        for key in (
            "provider_capability_snapshot",
            "provider_capabilities",
            "capability_snapshot",
            "capabilities",
            f"{stage_key}_provider_capabilities",
            f"{channel}_provider_capabilities",
            f"{stage_key}_capabilities",
            f"{channel}_capabilities",
        ):
            candidate = source.get(key)
            if not isinstance(candidate, Mapping):
                continue
            selected = candidate.get(stage_key) or candidate.get(channel)
            if isinstance(selected, Mapping):
                return selected
            return candidate
        provider = source.get("provider")
        if isinstance(provider, Mapping):
            for key in ("provider_capability_snapshot", "provider_capabilities", "capabilities"):
                candidate = provider.get(key)
                if isinstance(candidate, Mapping):
                    return candidate
        return None

    @staticmethod
    def _snapshot_text(
        source: Mapping[str, Any],
        names: Sequence[str],
    ) -> str:
        for name in names:
            value = source.get(name)
            if isinstance(value, Mapping):
                value = value.get("id") or value.get("provider_id") or value.get("model")
            text = _text(value)
            if text:
                return text
        return ""

    @classmethod
    def _provider_snapshot(
        cls,
        context: _ExecutionContext,
    ) -> tuple[str, str, dict[str, Any], list[Any]]:
        job_input = cls._input_data(context.job_record)
        config = _mapping(context.run_record.get("config_snapshot"))
        channel = cls._channel(context.stage_key)
        job_config = _mapping(job_input.get("config_snapshot"))
        source_values = (job_input, job_config, config)
        raw_capabilities: Mapping[str, Any] | None = None
        for source in source_values:
            raw_capabilities = cls._capability_snapshot(source, stage_key=context.stage_key)
            if raw_capabilities is not None:
                break

        provider_id = ""
        model = ""
        id_names = (
            f"{context.stage_key}_provider_id",
            f"{channel}_provider_id",
            "provider_id",
            "selected_provider_id",
        )
        model_names = (
            f"{context.stage_key}_provider_model",
            f"{channel}_provider_model",
            f"{channel}_model",
            "provider_model",
            "model",
        )
        for source in source_values:
            provider_id = provider_id or cls._snapshot_text(source, id_names)
            model = model or cls._snapshot_text(source, model_names)
        if raw_capabilities is not None:
            provider_id = provider_id or _text(raw_capabilities.get("provider_id"))
            model = model or _text(raw_capabilities.get("model"))
        if not provider_id or not model:
            default_provider_id, default_model = _DEFAULT_PROVIDER_IDS[context.stage_key]
            provider_id = provider_id or default_provider_id
            model = model or default_model

        diagnostics: list[Any] = []
        if raw_capabilities is None or not raw_capabilities:
            raw_capabilities = {}
            diagnostics.append(
                diagnostic(
                    "warning",
                    "provider_capability_snapshot_missing",
                    "未找到 Provider 能力快照，已使用保守默认能力；Provider 仅作为 provenance 保存",
                    path="provider_capabilities",
                    details={"stage_key": context.stage_key},
                )
            )
        try:
            capabilities = normalize_capabilities(raw_capabilities).model_dump(mode="json")
        except (TypeError, ValueError, ValidationError) as exc:
            capabilities = normalize_capabilities({}).model_dump(mode="json")
            diagnostics.append(
                diagnostic(
                    "blocking",
                    "provider_capability_snapshot_invalid",
                    "Provider 能力快照无效，已使用保守默认能力并阻断本次编译",
                    path="provider_capabilities",
                    details={"error_type": type(exc).__name__},
                )
            )
        capabilities["provider_id"] = provider_id
        capabilities["model"] = model
        return provider_id, model, capabilities, diagnostics

    def _current_prompt_input_signature(
        self,
        connection: Any,
        *,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        job_record: Mapping[str, Any],
    ) -> str:
        storyboard_stage = self._latest_stage(connection, str(run["id"]), "4-storyboard")
        if storyboard_stage is None or storyboard_stage.get("status") != "confirmed":
            raise PromptStageExecutionError("阶段前置确认未满足：4-storyboard")
        storyboard = self._parse_storyboard(storyboard_stage)
        character_stage = self._latest_stage(connection, str(run["id"]), "1-character-bios")
        character_bios = self._parse_character_bios(character_stage)
        anchors = self._build_anchors(
            connection,
            run=run,
            storyboard=storyboard,
            character_bios=character_bios,
        )
        _, _, capabilities, _ = self._provider_snapshot(
            _ExecutionContext(
                job_record=dict(job_record),
                run_record=dict(run),
                stage_record=dict(stage),
            )
        )
        references, _, _ = self._build_references_and_continuity(
            connection,
            project_id=str(run.get("project_id") or ""),
            chapter_id=str(run.get("chapter_id") or ""),
            provider_capabilities=capabilities,
            video_stage=str(stage.get("stage_key")) == "5b-video-prompts",
        )
        reference_hashes: dict[str, str] = {}
        for storyboard_shot in storyboard.shots:
            reference = references.get(storyboard_shot.external_key)
            reference_hashes[storyboard_shot.external_key] = (
                _text(reference.get("snapshot_hash"))
                if isinstance(reference, Mapping)
                else ""
            )
        asset_versions = [
            {
                "asset_id": asset_id,
                "asset_type": anchor.asset_type,
                "revision": anchor.asset_revision,
                "anchor_version": anchor_version(anchor),
            }
            for asset_id, anchor in sorted(anchors.items())
        ]
        shot_snapshots = [
            {
                "external_key": item.external_key,
                "shot_no": item.shot_no,
                "ordinal": item.ordinal,
                "scene_asset_id": item.scene_asset_id or "",
                "character_asset_ids": list(item.character_asset_ids),
                "prop_asset_ids": list(item.prop_asset_ids),
                "reference_plan_hash": reference_hashes.get(item.external_key, ""),
            }
            for item in storyboard.shots
        ]
        provider_id = _text(capabilities.get("provider_id"))
        provider_model = _text(capabilities.get("model"))
        payload = dynamic_materializer.build_prompt_stage_signature_payload(
            stage_key=str(stage.get("stage_key") or ""),
            run_input_signature=str(run.get("input_signature") or ""),
            shots=shot_snapshots,
            asset_versions=asset_versions,
            reference_plan_hashes=reference_hashes,
            provider_id=provider_id,
            provider_model=provider_model,
            provider_capabilities=capabilities,
            compiler_version=COMPILER_VERSION,
            normalizer_version=normalizers.NORMALIZER_VERSION,
            gate_version="dynamic-gate-v1",
        )
        override_snapshot = self._overrides(self._input_data(job_record))
        if override_snapshot is None:
            content = self._stage_content(stage)
            bundles = content.get("bundles") if isinstance(content, Mapping) else None
            if isinstance(bundles, Mapping):
                override_snapshot = {
                    str(key): value.get("human_override")
                    for key, value in bundles.items()
                    if isinstance(value, Mapping) and value.get("human_override") is not None
                }
        payload["human_override_hash"] = prompt_stage_override_hash(override_snapshot)
        return dynamic_materializer.calculate_prompt_stage_input_signature(payload)

    @staticmethod
    def _overrides(job_input: Mapping[str, Any]) -> Mapping[str, Any] | None:
        for key in ("overrides_by_external_key", "human_overrides", "overrides"):
            value = job_input.get(key)
            if isinstance(value, Mapping):
                return value
        return None

    def _compile(
        self,
        context: _ExecutionContext,
    ) -> PromptStageOutput:
        job_input = self._input_data(context.job_record)
        config = _mapping(context.run_record.get("config_snapshot"))
        provider_id, provider_model, capabilities, capability_diagnostics = self._provider_snapshot(
            context
        )
        with self.database.engine.connect() as connection:
            storyboard_stage = self._latest_stage(connection, context.run_id, "4-storyboard")
            if storyboard_stage is None or storyboard_stage.get("status") != "confirmed":
                raise PromptStageExecutionError(
                    "阶段前置确认未满足：4-storyboard",
                    job_id=context.job_id,
                    stage_key=context.stage_key,
                )
            storyboard = self._parse_storyboard(storyboard_stage)
            character_stage = self._latest_stage(connection, context.run_id, "1-character-bios")
            character_bios = self._parse_character_bios(character_stage)
            anchors = self._build_anchors(
                connection,
                run=context.run_record,
                storyboard=storyboard,
                character_bios=character_bios,
            )
            references, previous_end_frames, previous_reference_info = (
                self._build_references_and_continuity(
                    connection,
                    project_id=str(context.run_record.get("project_id") or ""),
                    chapter_id=str(context.run_record.get("chapter_id") or ""),
                    provider_capabilities=capabilities,
                    video_stage=context.stage_key == "5b-video-prompts",
                )
            )

        reference_blocking = [
            item
            for plan in references.values()
            for item in _as_list(plan.get("diagnostics"))
            if isinstance(item, Mapping) and item.get("level") == "blocking"
        ]
        if reference_blocking:
            raise PromptStageExecutionError(
                f"阶段 {context.stage_key} 参考图计划存在 blocking，未调用编译器",
                job_id=context.job_id,
                stage_key=context.stage_key,
            )

        style = build_visual_style_spec(style_prompt=_text(config.get("style_prompt")))
        signature = _text(context.stage_record.get("input_signature"))
        if not signature or signature != _text(context.job_record.get("input_signature")):
            raise PromptStageExecutionError(
                "Prompt 阶段缺少一致的 Job/stage 分支 input_signature",
                job_id=context.job_id,
                stage_key=context.stage_key,
            )
        try:
            if context.stage_key == "5a-image-prompts":
                output = compile_image_prompt_stage(
                    storyboard,
                    style=style,
                    anchors_by_asset_id=anchors,
                    references_by_external_key=references,
                    capabilities=capabilities,
                    workflow_run_id=context.run_id,
                    workflow_stage_id=context.stage_id,
                    input_signature=signature,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    overrides_by_external_key=self._overrides(job_input),
                )
            else:
                output = compile_video_prompt_stage(
                    storyboard,
                    style=style,
                    anchors_by_asset_id=anchors,
                    references_by_external_key=references,
                    capabilities=capabilities,
                    workflow_run_id=context.run_id,
                    workflow_stage_id=context.stage_id,
                    input_signature=signature,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    overrides_by_external_key=self._overrides(job_input),
                    previous_end_frames=previous_end_frames,
                    previous_reference_info_by_external_key=previous_reference_info,
                )
        except (PromptStageError, TypeError, ValueError, ValidationError) as exc:
            raise PromptStageExecutionError(
                f"阶段 {context.stage_key} Prompt 编译失败",
                job_id=context.job_id,
                stage_key=context.stage_key,
            ) from exc

        diagnostics = _dedupe_diagnostics([*output.diagnostics, *capability_diagnostics])
        gate = diagnostics_status(diagnostics)
        provenance = _copy(output.provenance)
        provenance["diagnostics"] = [item.model_dump(mode="json") for item in gate.diagnostics]
        artifact_meta = output.artifact_meta.model_copy(
            update={"source_attempt": int(context.stage_record.get("attempt") or 1)}
        )
        return output.model_copy(
            deep=True,
            update={
                "artifact_meta": artifact_meta,
                "provenance": provenance,
                "status": gate.status,
                "diagnostics": list(gate.diagnostics),
            },
        )

    # ------------------------------------------------------------------
    # 结果提交和冲突保护
    # ------------------------------------------------------------------
    @staticmethod
    def _result_reason(
        connection: Any,
        context: _ExecutionContext,
        current_job: Mapping[str, Any] | None,
        current_run: Mapping[str, Any] | None,
        current_stage: Mapping[str, Any] | None,
    ) -> str | None:
        if current_job is None or current_run is None or current_stage is None:
            return "关联记录已不存在"
        if current_job.get("workflow_run_id") != context.run_id:
            return "Job 的 run 关联已变化"
        if current_job.get("workflow_stage_id") != context.stage_id:
            return "Job 的 stage 关联已变化"
        if current_stage.get("workflow_run_id") != context.run_id:
            return "stage 的 run 关联已变化"
        if current_job.get("project_id") != current_run.get("project_id"):
            return "Job 与 run 的 project 关联已变化"
        if current_job.get("chapter_id") != current_run.get("chapter_id"):
            return "Job 与 run 的 chapter 关联已变化"
        if current_job.get("status") in _PROTECTED_JOB_STATUSES:
            return f"Job 当前状态为 {current_job.get('status')!r}"
        if current_job.get("status") not in _EXECUTABLE_JOB_STATUSES:
            return f"Job 当前状态为 {current_job.get('status')!r}"
        if current_run.get("status") in _PROTECTED_WORKFLOW_STATUSES:
            return f"run 当前状态为 {current_run.get('status')!r}"
        if current_run.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES:
            return f"run 当前状态为 {current_run.get('status')!r}"
        if current_stage.get("status") in _PROTECTED_WORKFLOW_STATUSES:
            return f"stage 当前状态为 {current_stage.get('status')!r}"
        if current_stage.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES:
            return f"stage 当前状态为 {current_stage.get('status')!r}"
        latest = DynamicPromptStageService._latest_stage(
            connection,
            context.run_id,
            context.stage_key,
        )
        if latest is None or latest.get("id") != context.stage_id:
            return "stage 已不是当前最新 attempt"
        storyboard = DynamicPromptStageService._latest_stage(
            connection,
            context.run_id,
            "4-storyboard",
        )
        if storyboard is None or storyboard.get("status") != "confirmed":
            return "阶段前置确认已变化：4-storyboard"
        if current_job.get("input_signature") != context.job_record.get("input_signature"):
            return "Job input_signature 已变化"
        if current_stage.get("input_signature") != context.stage_record.get("input_signature"):
            return "stage input_signature 已变化"
        if current_run.get("input_signature") != context.run_record.get("input_signature"):
            return "run input_signature 已变化"
        job_signature = _text(current_job.get("input_signature"))
        stage_signature = _text(current_stage.get("input_signature"))
        if not job_signature or not stage_signature or job_signature != stage_signature:
            return "Job 与 stage 的分支 input_signature 已不一致"
        return None

    @staticmethod
    def _conflict_message(reason: str) -> str:
        return _error_text(f"动态 Prompt 阶段结果提交冲突：{reason}")

    @classmethod
    def _mark_conflict_in_connection(
        cls,
        connection: Any,
        current_job: Mapping[str, Any] | None,
        current_stage: Mapping[str, Any] | None,
        reason: str,
    ) -> None:
        message = cls._conflict_message(reason)
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
                    message="任务因 Prompt 结果提交冲突而中断",
                    finished_at=now,
                    updated_at=now,
                )
            )
        if current_stage is not None and current_stage.get("status") not in {
            "confirmed",
            "cancelled",
            "stale",
        }:
            connection.execute(
                update(workflow_stage)
                .where(
                    workflow_stage.c.id == current_stage["id"],
                    workflow_stage.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                    workflow_stage.c.updated_at == current_stage["updated_at"],
                )
                .values(status="stale", error=message, finished_at=now, updated_at=now)
            )

    def _mark_result_conflict(self, context: _ExecutionContext, reason: str) -> None:
        try:
            with self.database.engine.begin() as connection:
                current_job = self._job(connection, context.job_id)
                current_stage = self._stage_by_id(connection, context.stage_id, context.run_id)
                self._mark_conflict_in_connection(
                    connection,
                    current_job,
                    current_stage,
                    reason,
                )
        except Exception:
            return

    def _mark_compile_failure(self, context: _ExecutionContext, exc: Exception) -> None:
        message = _error_text(exc)
        try:
            with self.database.engine.begin() as connection:
                current_job = self._job(connection, context.job_id)
                current_run = self._run(connection, context.run_id)
                current_stage = self._stage_by_id(connection, context.stage_id, context.run_id)
                if current_job is None or current_run is None or current_stage is None:
                    return
                if (
                    current_job.get("status") not in _EXECUTABLE_JOB_STATUSES
                    or current_run.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES
                    or current_stage.get("status") not in _EXECUTABLE_WORKFLOW_STATUSES
                ):
                    return
                now = utc_now()
                job_update = connection.execute(
                    update(job)
                    .where(
                        job.c.id == context.job_id,
                        job.c.status.in_(_EXECUTABLE_JOB_STATUSES),
                        job.c.updated_at == current_job["updated_at"],
                    )
                    .values(
                        status="failed",
                        error=message,
                        message="Prompt 阶段编译失败",
                        finished_at=now,
                        updated_at=now,
                    )
                )
                if job_update.rowcount != 1:
                    return
                stage_update = connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == context.stage_id,
                        workflow_stage.c.workflow_run_id == context.run_id,
                        workflow_stage.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                        workflow_stage.c.attempt == context.stage_record["attempt"],
                        workflow_stage.c.updated_at == current_stage["updated_at"],
                    )
                    .values(status="failed", error=message, finished_at=now, updated_at=now)
                )
                if stage_update.rowcount != 1:
                    return
                barrier_status = self._prompt_barrier_status(connection, context.run_id)
                run_status = barrier_status or str(current_run["status"])
                run_update = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == context.run_id,
                        workflow_run.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                        _signature_condition(
                            workflow_run.c.input_signature,
                            current_run.get("input_signature"),
                        ),
                    )
                    .values(
                        status=run_status,
                        revision=workflow_run.c.revision + 1,
                        error=(
                            message
                            if barrier_status in {"partial_failed", "interrupted"}
                            else ""
                        ),
                        finished_at=now if barrier_status is not None else None,
                        updated_at=now,
                    )
                )
                if run_update.rowcount != 1:
                    return
        except Exception:
            return

    async def _persist_result(
        self,
        context: _ExecutionContext,
        output: PromptStageOutput,
    ) -> dict[str, Any]:
        stage_json = stage_output_to_stage_json(output)
        now = utc_now()
        gate = StageGateResult(
            status=output.status,
            passed=output.status != "blocked",
            diagnostics=list(output.diagnostics),
            checked_at=now,
            can_confirm=output.status != "blocked",
        )
        gate_json = gate.model_dump(mode="json")
        target_status = "blocked" if output.status == "blocked" else "ready"
        output_refs = [
            {
                "capture_mode": "compiled_prompt_stage",
                "stage_key": context.stage_key,
                "workflow_run_id": context.run_id,
                "workflow_stage_id": context.stage_id,
                "input_signature": stage_json["artifact_meta"].get("input_signature", ""),
                "compiler_version": stage_json["artifact_meta"].get("normalizer_version", ""),
                "provider_id": stage_json["artifact_meta"].get("provider_id"),
                "provider_model": stage_json["artifact_meta"].get("provider_model"),
                "target_count": len(output.bundles),
                "status": output.status,
                "content_signature": stage_json["artifact_meta"].get("content_signature", ""),
                "diagnostic_codes": [item.code for item in output.diagnostics],
            }
        ]
        raw_output = {
            "capture_mode": "compiled_prompt_stage",
            "compiled_output": stage_json,
        }
        result_data = {
            "stage_id": context.stage_id,
            "stage_key": context.stage_key,
            "input_signature": _text(context.stage_record.get("input_signature")),
            "status": output.status,
            "target_count": len(output.bundles),
        }
        conflict_message: str | None = None
        try:
            with self.database.engine.begin() as connection:
                current_job = self._job(connection, context.job_id)
                current_run = self._run(connection, context.run_id)
                current_stage = self._stage_by_id(connection, context.stage_id, context.run_id)
                reason = self._result_reason(
                    connection,
                    context,
                    current_job,
                    current_run,
                    current_stage,
                )
                if reason is not None:
                    self._mark_conflict_in_connection(
                        connection,
                        current_job,
                        current_stage,
                        reason,
                    )
                    conflict_message = self._conflict_message(reason)
                else:
                    assert current_job is not None
                    assert current_run is not None
                    assert current_stage is not None
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
                            normalized_output_json=json_dumps(stage_json),
                            draft_output_json=json_dumps(stage_json),
                            output_refs_json=json_dumps(output_refs),
                            gate_result_json=json_dumps(gate_json),
                            error="",
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                    if stage_update.rowcount != 1:
                        raise _ResultWriteConflict("stage 条件更新未命中")

                    barrier_status = self._prompt_barrier_status(connection, context.run_id)
                    run_status = barrier_status or str(current_run["status"])
                    run_update = connection.execute(
                        update(workflow_run)
                        .where(
                            workflow_run.c.id == context.run_id,
                            workflow_run.c.status.in_(_EXECUTABLE_WORKFLOW_STATUSES),
                            _signature_condition(
                                workflow_run.c.input_signature,
                                context.run_record.get("input_signature"),
                            ),
                        )
                        .values(
                            status=run_status,
                            revision=workflow_run.c.revision + 1,
                            error="",
                            finished_at=now if barrier_status is not None else None,
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
                            message="Prompt 阶段编译完成",
                            result_json=json_dumps(result_data),
                            error="",
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                    if job_update.rowcount != 1:
                        raise _ResultWriteConflict("Job 条件更新未命中")
        except _ResultWriteConflict as exc:
            self._mark_result_conflict(context, str(exc))
            raise PromptStageExecutionError(
                self._conflict_message(str(exc)),
                job_id=context.job_id,
                stage_key=context.stage_key,
            ) from exc

        if conflict_message is not None:
            raise PromptStageExecutionError(
                conflict_message,
                job_id=context.job_id,
                stage_key=context.stage_key,
            )
        return result_data

    async def execute_job(self, job_record: Mapping[str, Any]) -> dict[str, Any]:
        """预检、编译 5a/5b 并原子保存 Prompt bundle。"""

        if not isinstance(job_record, Mapping):
            raise PromptStageExecutionError("Job 记录必须是对象")
        context = self._preflight(job_record)
        try:
            output = self._compile(context)
        except PromptStageExecutionError as exc:
            self._mark_compile_failure(context, exc)
            raise
        except Exception as exc:
            self._mark_compile_failure(context, exc)
            raise PromptStageExecutionError(
                f"阶段 {context.stage_key} Prompt 编译失败",
                job_id=context.job_id,
                stage_key=context.stage_key,
            ) from exc
        return await self._persist_result(context, output)


__all__ = [
    "PROMPT_STAGE_KEYS",
    "DynamicPromptStageService",
    "PromptStageExecutionError",
]
