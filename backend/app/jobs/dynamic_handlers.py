from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select, update

from app.db import Database, utc_now
from app.jobs.repository import NON_TERMINAL_STATUSES, JobRepository
from app.models.tables import job, workflow_run, workflow_stage

_EXECUTABLE_WORKFLOW_STATUSES = frozenset({"queued", "running"})
_PROTECTED_WORKFLOW_STATUSES = frozenset({"blocked", "cancelled", "stale", "confirmed"})
_UNIMPLEMENTED_ERROR = (
    "Phase 2 动态 Stage handler 尚未实现：当前仅执行安全占位处理，未调用 Provider"
)


class _ConcurrentStateChange(Exception):
    """工作流或 Job 在占位处理期间发生了并发状态变化。"""


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


def make_dynamic_stage_generate_handler(
    database: Database,
    repository: JobRepository | None = None,
) -> Callable:
    """创建 Phase 1C-C 的动态阶段安全占位处理器。"""

    current_repository = repository or JobRepository(database)

    def handle(record: dict[str, Any]) -> None:
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
