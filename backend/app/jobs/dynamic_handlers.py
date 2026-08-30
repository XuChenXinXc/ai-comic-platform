from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update

from app.db import Database, utc_now
from app.dynamic_pipeline.delivery import DeliveryExecutionError, DynamicDeliveryJobService
from app.dynamic_pipeline.prompt_stage_service import (
    DynamicPromptStageService,
    PromptStageExecutionError,
)
from app.dynamic_pipeline.stage_service import DynamicStageService, StageExecutionError
from app.jobs.repository import NON_TERMINAL_STATUSES, JobRepository
from app.models.tables import job, workflow_run, workflow_stage
from app.services.utils import json_loads

_EXECUTABLE_WORKFLOW_STATUSES = frozenset({"queued", "running"})
_PROTECTED_WORKFLOW_STATUSES = frozenset({"blocked", "cancelled", "stale", "confirmed"})
_DYNAMIC_STAGE_KEYS = frozenset(
    {"0-source", "1-character-bios", "2-rhythm", "3-script", "4-storyboard"}
)
_PROMPT_STAGE_KEYS = frozenset({"5a-image-prompts", "5b-video-prompts"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})
_UNIMPLEMENTED_ERROR = (
    "Phase 2 动态 Stage handler 尚未实现：当前仅执行安全占位处理，未调用 Provider"
)


class _ConcurrentStateChange(Exception):
    """工作流或 Job 在处理期间发生了并发状态变化。"""


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _update_job(
    connection: Any,
    current_job: Mapping[str, Any],
    *,
    status: str,
    error: str,
    message: str,
) -> bool:
    current_status = current_job.get("status")
    if current_status not in NON_TERMINAL_STATUSES:
        return False
    now = utc_now()
    result = connection.execute(
        update(job)
        .where(
            job.c.id == current_job["id"],
            job.c.status == current_status,
            job.c.updated_at == current_job["updated_at"],
        )
        .values(
            status=status,
            error=error,
            message=message,
            finished_at=now,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def _mark_missing_association(
    connection: Any,
    current_job: Mapping[str, Any],
    error: str,
) -> None:
    _update_job(
        connection,
        current_job,
        status="failed",
        error=error,
        message="任务失败",
    )


def _mark_non_executable(
    connection: Any,
    current_job: Mapping[str, Any],
    run_status: Any,
    stage_status: Any,
) -> None:
    error = (
        "动态任务关联的工作流当前不可执行，"
        f"run.status={run_status!r}, stage.status={stage_status!r}；"
        "未调用 Provider，任务已中断并保留现有 Workflow 状态"
    )
    _update_job(
        connection,
        current_job,
        status="interrupted",
        error=error,
        message="任务因工作流状态不可执行而中断",
    )


def _mark_obsolete_dynamic_job(
    connection: Any,
    current_job: Mapping[str, Any],
    message: str,
) -> None:
    _update_job(
        connection,
        current_job,
        status="interrupted",
        error=message,
        message="任务因 attempt 或 input_signature 过期而中断",
    )
def _block_workflow_and_fail_job(
    connection: Any,
    current_job: Mapping[str, Any],
    run_record: Mapping[str, Any],
    stage_record: Mapping[str, Any],
) -> None:
    run_id = run_record["id"]
    stage_id = stage_record["id"]
    stage_status = stage_record["status"]
    run_status = run_record["status"]
    if (
        run_status not in _EXECUTABLE_WORKFLOW_STATUSES
        or stage_status not in _EXECUTABLE_WORKFLOW_STATUSES
    ):
        _mark_non_executable(connection, current_job, run_status, stage_status)
        return

    if current_job.get("status") not in NON_TERMINAL_STATUSES:
        return

    now = utc_now()
    stage_result = connection.execute(
        update(workflow_stage)
        .where(
            workflow_stage.c.id == stage_id,
            workflow_stage.c.workflow_run_id == run_id,
            workflow_stage.c.status == stage_status,
            workflow_stage.c.updated_at == stage_record["updated_at"],
        )
        .values(
            status="blocked",
            error=_UNIMPLEMENTED_ERROR,
            updated_at=now,
        )
    )
    if stage_result.rowcount != 1:
        raise _ConcurrentStateChange

    run_result = connection.execute(
        update(workflow_run)
        .where(
            workflow_run.c.id == run_id,
            workflow_run.c.status == run_status,
            workflow_run.c.revision == run_record["revision"],
            workflow_run.c.updated_at == run_record["updated_at"],
        )
        .values(
            status="blocked",
            revision=workflow_run.c.revision + 1,
            error=_UNIMPLEMENTED_ERROR,
            updated_at=now,
        )
    )
    if run_result.rowcount != 1:
        raise _ConcurrentStateChange

    if not _update_job(
        connection,
        current_job,
        status="failed",
        error=_UNIMPLEMENTED_ERROR,
        message="任务失败",
    ):
        raise _ConcurrentStateChange


def _input_data(record: Mapping[str, Any], current_job: Mapping[str, Any]) -> dict[str, Any]:
    stored = current_job.get("input_data")
    if not isinstance(stored, Mapping):
        stored = json_loads(current_job.get("input_json"), {})
    incoming = record.get("input_data")
    if not isinstance(incoming, Mapping):
        incoming = {}
    stored_data = dict(stored) if isinstance(stored, Mapping) else {}
    return {**stored_data, **dict(incoming)}


def _stage_key(
    record: Mapping[str, Any],
    current_job: Mapping[str, Any],
    stage_record: Mapping[str, Any],
) -> str:
    associated_key = stage_record.get("stage_key")
    if associated_key:
        return str(associated_key)
    input_stage_key = _input_data(record, current_job).get("stage_key")
    return str(input_stage_key or "")


def _current_stage_status(
    current_repository: JobRepository,
    current_job: Mapping[str, Any],
    fallback: Any,
) -> Any:
    stage_id = current_job.get("workflow_stage_id")
    if not stage_id:
        return fallback
    with current_repository.engine.connect() as connection:
        status = connection.scalar(
            select(workflow_stage.c.status).where(workflow_stage.c.id == stage_id)
        )
    return fallback if status is None else status


def _phase2_error_result(
    current_repository: JobRepository,
    job_id: str,
    error: StageExecutionError | PromptStageExecutionError,
    fallback_stage_status: Any,
) -> dict[str, Any]:
    current_job = current_repository.get(job_id)
    if current_job is None:
        raise error

    current_status = current_job.get("status")
    if current_status in NON_TERMINAL_STATUSES:
        current_job = current_repository.interrupt(job_id, str(error))
    elif current_status not in _TERMINAL_JOB_STATUSES:
        raise error

    result_data = current_job.get("result_data")
    if isinstance(result_data, Mapping) and result_data:
        return dict(result_data)
    return {
        "stage_status": _current_stage_status(
            current_repository,
            current_job,
            fallback_stage_status,
        ),
        "error": str(error),
    }


async def _execute_phase2_stage(
    stage_service: Any,
    current_repository: JobRepository,
    record: Mapping[str, Any],
    fallback_stage_status: Any,
) -> Any:
    job_id = str(record["id"])
    try:
        result = stage_service.execute_job(record)
        if inspect.isawaitable(result):
            return await result
        return result
    except StageExecutionError as error:
        return _phase2_error_result(
            current_repository,
            job_id,
            error,
            fallback_stage_status,
        )
    except PromptStageExecutionError as error:
        return _phase2_error_result(
            current_repository,
            job_id,
            error,
            fallback_stage_status,
        )


async def _execute_delivery_stage(
    delivery_service: Any,
    current_repository: JobRepository,
    record: Mapping[str, Any],
) -> Any:
    job_id = str(record["id"])
    try:
        result = delivery_service.execute_job(record)
        if inspect.isawaitable(result):
            return await result
        return result
    except DeliveryExecutionError as error:
        current_job = current_repository.get(job_id)
        if current_job is not None and current_job.get("status") in NON_TERMINAL_STATUSES:
            current_repository.fail(job_id, str(error))
        return {"status": "failed", "job_id": job_id, "error": str(error)}
def make_dynamic_stage_generate_handler(
    database: Database,
    repository: JobRepository | None = None,
    stage_service: Any | None = None,
    prompt_stage_service: Any | None = None,
    delivery_service: Any | None = None,
) -> Callable:
    """创建动态阶段处理器，并执行 6-delivery 子任务编排。"""

    current_repository = repository or JobRepository(database)
    current_stage_service = (
        stage_service if stage_service is not None else DynamicStageService(database)
    )
    current_prompt_stage_service = (
        prompt_stage_service
        if prompt_stage_service is not None
        else DynamicPromptStageService(database)
    )
    current_delivery_service = (
        delivery_service
        if delivery_service is not None
        else DynamicDeliveryJobService(database, repository=current_repository)
    )

    def handle(record: dict[str, Any]) -> Any:
        job_id = record.get("id")
        if not job_id:
            return

        try:
            with current_repository.engine.begin() as connection:
                current_job = _row_dict(
                    connection.execute(select(job).where(job.c.id == job_id)).first()
                )
                if current_job is None:
                    return

                run_id = current_job.get("workflow_run_id")
                stage_id = current_job.get("workflow_stage_id")
                if not run_id:
                    _mark_missing_association(
                        connection,
                        current_job,
                        "动态任务缺少关联的工作流运行，无法安全执行；未伪造 Workflow Stage",
                    )
                    return
                if not stage_id:
                    _mark_missing_association(
                        connection,
                        current_job,
                        "动态任务缺少关联的工作流阶段，无法安全执行；未伪造 Workflow Stage",
                    )
                    return

                run_record = _row_dict(
                    connection.execute(
                        select(workflow_run).where(workflow_run.c.id == run_id)
                    ).first()
                )
                if run_record is None:
                    _mark_missing_association(
                        connection,
                        current_job,
                        "动态任务关联的工作流运行不存在，无法安全执行；未伪造 Workflow Stage",
                    )
                    return

                stage_record = _row_dict(
                    connection.execute(
                        select(workflow_stage).where(
                            workflow_stage.c.id == stage_id,
                            workflow_stage.c.workflow_run_id == run_id,
                        )
                    ).first()
                )
                if stage_record is None:
                    _mark_missing_association(
                        connection,
                        current_job,
                        "动态任务关联的工作流阶段不存在或不属于该运行，"
                        "无法安全执行；未伪造 Workflow Stage",
                    )
                    return

                latest_stage = _row_dict(
                    connection.execute(
                        select(workflow_stage)
                        .where(
                            workflow_stage.c.workflow_run_id == run_id,
                            workflow_stage.c.stage_key == stage_record.get("stage_key"),
                        )
                        .order_by(
                            workflow_stage.c.attempt.desc(),
                            workflow_stage.c.updated_at.desc(),
                            workflow_stage.c.id.desc(),
                        )
                        .limit(1)
                    ).first()
                )
                if latest_stage is None or latest_stage.get("id") != stage_id:
                    _mark_obsolete_dynamic_job(
                        connection,
                        current_job,
                        "动态 Job 关联的 stage attempt 已过期，禁止覆盖新结果",
                    )
                    return
                job_signature = str(current_job.get("input_signature") or "")
                stage_signature = str(stage_record.get("input_signature") or "")
                if job_signature and stage_signature and job_signature != stage_signature:
                    _mark_obsolete_dynamic_job(
                        connection,
                        current_job,
                        "动态 Job 与当前 stage input_signature 不一致，禁止覆盖新结果",
                    )
                    return

                stage_key = _stage_key(record, current_job, stage_record)
                if stage_key in _DYNAMIC_STAGE_KEYS:
                    return _execute_phase2_stage(
                        current_stage_service,
                        current_repository,
                        record,
                        stage_record.get("status"),
                    )
                if stage_key in _PROMPT_STAGE_KEYS:
                    return _execute_phase2_stage(
                        current_prompt_stage_service,
                        current_repository,
                        record,
                        stage_record.get("status"),
                    )
                if stage_key == "6-delivery":
                    return _execute_delivery_stage(
                        current_delivery_service,
                        current_repository,
                        record,
                    )

                run_status = run_record["status"]
                stage_status = stage_record["status"]
                if (
                    run_status in _PROTECTED_WORKFLOW_STATUSES
                    or stage_status in _PROTECTED_WORKFLOW_STATUSES
                    or run_status not in _EXECUTABLE_WORKFLOW_STATUSES
                    or stage_status not in _EXECUTABLE_WORKFLOW_STATUSES
                ):
                    _mark_non_executable(connection, current_job, run_status, stage_status)
                    return

                _block_workflow_and_fail_job(
                    connection,
                    current_job,
                    run_record,
                    stage_record,
                )
        except _ConcurrentStateChange:
            # 条件更新失败时回滚本次事务，不覆盖并发取消或 stale。
            return

    return handle


__all__ = ["make_dynamic_stage_generate_handler"]
