from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from app.db import utc_now
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository, has_download_source
from app.models.tables import job, workflow_run, workflow_stage

_PROTECTED_WORKFLOW_STATUSES = {"blocked", "cancelled", "stale", "confirmed"}
_PROTECTED_STAGE_STATUSES = {"blocked", "cancelled", "stale", "confirmed"}
_PROTECTED_STAGE_RUN_STATUSES = _PROTECTED_WORKFLOW_STATUSES
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_SAFE_INTERRUPTED_RUN_STATUSES = {"queued", "running"}
_SAFE_PARTIAL_FAILED_RUN_STATUSES = {"queued", "running"}

_ALLOWED_STAGE_STATUSES_BY_JOB = {
    "queued": frozenset({"queued"}),
    "running": frozenset({"queued", "running"}),
    "waiting_provider": frozenset({"queued", "running"}),
    "downloading": frozenset({"queued", "running"}),
}

_POLL_MESSAGE = "服务重启后继续轮询原 Provider 任务"
_DOWNLOAD_MESSAGE = "服务重启后继续下载已保存的 Provider 结果"


@dataclass
class DynamicRecoveryReport:
    enqueue_ids: list[str] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)
    interrupted_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    partial_failed_stage_ids: list[str] = field(default_factory=list)


class _WorkflowConcurrentChange(Exception):
    """工作流关联记录在恢复写入期间发生了并发变化。"""


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _load_workflow_records(
    repository: JobRepository,
    record: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    run_id = record.get("workflow_run_id")
    stage_id = record.get("workflow_stage_id")
    with repository.engine.connect() as connection:
        run_row = connection.execute(
            select(workflow_run).where(workflow_run.c.id == run_id)
        ).first()
        stage_row = None
        if run_row is not None and stage_id:
            stage_row = connection.execute(
                select(workflow_stage).where(
                    workflow_stage.c.id == stage_id,
                    workflow_stage.c.workflow_run_id == run_id,
                )
            ).first()
    return _row_dict(run_row), _row_dict(stage_row)


def _update_workflow_state(
    repository: JobRepository,
    *,
    run_id: str,
    stage_id: str | None = None,
    stage_target: str | None = None,
    stage_error: str | None = None,
    run_target: str | None = None,
    run_error: str | None = None,
    run_allowed_statuses: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, bool]:
    """在一个短事务中条件更新阶段/运行，并为阶段写入递增 revision。"""

    try:
        with repository.engine.begin() as connection:
            run_record = _row_dict(
                connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
            )
            if run_record is None:
                return False, False

            stage_record = None
            if stage_id:
                stage_record = _row_dict(
                    connection.execute(
                        select(workflow_stage).where(
                            workflow_stage.c.id == stage_id,
                            workflow_stage.c.workflow_run_id == run_id,
                        )
                    ).first()
                )

            stage_changed = False
            current_run_status = str(run_record["status"])
            if (
                stage_target is not None
                and stage_record is not None
                and current_run_status not in _PROTECTED_STAGE_RUN_STATUSES
                and stage_record["status"] not in _PROTECTED_STAGE_STATUSES
                and stage_record["status"] != stage_target
            ):
                now = utc_now()
                stage_values: dict[str, Any] = {
                    "status": stage_target,
                    "updated_at": now,
                }
                if stage_error is not None:
                    stage_values["error"] = stage_error
                if stage_target in {
                    "ready",
                    "confirmed",
                    "partial_failed",
                    "failed",
                    "interrupted",
                    "cancelled",
                }:
                    stage_values["finished_at"] = now

                stage_result = connection.execute(
                    update(workflow_stage)
                    .where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.status == stage_record["status"],
                        workflow_stage.c.updated_at == stage_record["updated_at"],
                    )
                    .values(**stage_values)
                )
                if stage_result.rowcount != 1:
                    raise _WorkflowConcurrentChange
                stage_changed = True

            run_changed = (
                run_target is not None
                and current_run_status in run_allowed_statuses
                and current_run_status not in _PROTECTED_WORKFLOW_STATUSES
                and current_run_status != run_target
            )
            if run_changed:
                now = utc_now()
                run_values: dict[str, Any] = {
                    "status": run_target,
                    "revision": workflow_run.c.revision + 1,
                    "updated_at": now,
                }
                if run_error is not None:
                    run_values["error"] = run_error
                if run_target in {"failed", "interrupted", "cancelled"}:
                    run_values["finished_at"] = now
                run_result = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == run_id,
                        workflow_run.c.revision == run_record["revision"],
                        workflow_run.c.status == current_run_status,
                    )
                    .values(**run_values)
                )
                if run_result.rowcount != 1:
                    raise _WorkflowConcurrentChange
            elif stage_changed and not run_changed:
                revision_result = connection.execute(
                    update(workflow_run)
                    .where(
                        workflow_run.c.id == run_id,
                        workflow_run.c.revision == run_record["revision"],
                        workflow_run.c.status == current_run_status,
                    )
                    .values(
                        revision=workflow_run.c.revision + 1,
                        updated_at=utc_now(),
                    )
                )
                if revision_result.rowcount != 1:
                    raise _WorkflowConcurrentChange

            return stage_changed, run_changed
    except _WorkflowConcurrentChange:
        return False, False

    return stage_changed, run_changed


def _mark_interrupted(
    repository: JobRepository,
    report: DynamicRecoveryReport,
    record: dict[str, Any],
    error: str,
    *,
    stage_target: str | None = "interrupted",
    run_target: str | None = "interrupted",
    run_allowed_statuses: set[str] | frozenset[str] = _SAFE_INTERRUPTED_RUN_STATUSES,
) -> None:
    job_id = record["id"]
    _discard_enqueue_id(report, job_id)
    repository.interrupt(job_id, error)
    _update_workflow_state(
        repository,
        run_id=record["workflow_run_id"],
        stage_id=record.get("workflow_stage_id"),
        stage_target=stage_target,
        stage_error=error,
        run_target=run_target,
        run_error=error,
        run_allowed_statuses=run_allowed_statuses,
    )
    report.interrupted_ids.append(job_id)


def _mark_failed(
    repository: JobRepository,
    report: DynamicRecoveryReport,
    record: dict[str, Any],
    error: str,
) -> None:
    job_id = record["id"]
    _discard_enqueue_id(report, job_id)
    repository.fail(job_id, error)
    stage_changed, _run_changed = _update_workflow_state(
        repository,
        run_id=record["workflow_run_id"],
        stage_id=record.get("workflow_stage_id"),
        stage_target="partial_failed",
        stage_error=error,
        run_target="partial_failed",
        run_error=error,
        run_allowed_statuses=_SAFE_PARTIAL_FAILED_RUN_STATUSES,
    )
    report.failed_ids.append(job_id)
    if stage_changed and record.get("workflow_stage_id"):
        report.partial_failed_stage_ids.append(record["workflow_stage_id"])


def _requeue(
    repository: JobRepository,
    report: DynamicRecoveryReport,
    record: dict[str, Any],
    message: str,
) -> None:
    _discard_enqueue_id(report, record["id"])
    if repository.requeue_for_recovery(
        record["id"],
        expected_status=record["status"],
        message=message,
    ):
        _add_enqueue_id(report, record["id"])
    else:
        _add_skipped_id(report, record["id"])


def _add_enqueue_id(report: DynamicRecoveryReport, job_id: str) -> None:
    if job_id not in report.enqueue_ids:
        report.enqueue_ids.append(job_id)


def _discard_enqueue_id(report: DynamicRecoveryReport, job_id: str) -> None:
    if job_id in report.enqueue_ids:
        report.enqueue_ids.remove(job_id)


def _add_skipped_id(report: DynamicRecoveryReport, job_id: str) -> None:
    _discard_enqueue_id(report, job_id)
    if job_id not in report.skipped_ids:
        report.skipped_ids.append(job_id)


def recover_dynamic_jobs(repository: JobRepository) -> DynamicRecoveryReport:
    report = DynamicRecoveryReport()
    for job_id in DynamicJobBridge(repository).reconcile_queued_stage_jobs():
        _add_enqueue_id(report, job_id)

    for record in repository.list_non_terminal(include_dynamic=True):
        run_id = record.get("workflow_run_id")
        if not run_id:
            continue

        run_record, stage_record = _load_workflow_records(repository, record)
        job_id = record["id"]
        if run_record is None:
            error = "动态任务关联的工作流运行不存在，无法恢复"
            _discard_enqueue_id(report, job_id)
            repository.fail(job_id, error)
            report.failed_ids.append(job_id)
            continue

        if run_record["status"] in _PROTECTED_WORKFLOW_STATUSES:
            _add_skipped_id(report, job_id)
            continue

        if stage_record is None:
            error = "动态任务关联的工作流阶段不存在，无法安全恢复"
            _mark_interrupted(
                repository,
                report,
                record,
                error,
                stage_target=None,
            )
            continue

        stage_status = str(stage_record["status"])
        if stage_status in _PROTECTED_STAGE_STATUSES:
            if stage_status == "stale":
                _mark_interrupted(
                    repository,
                    report,
                    record,
                    "动态任务关联的工作流阶段已 stale，禁止自动恢复",
                    stage_target=None,
                )
            else:
                _add_skipped_id(report, job_id)
            continue

        job_signature = record.get("input_signature")
        stage_signature = stage_record.get("input_signature")
        if job_signature and stage_signature and job_signature != stage_signature:
            error = "输入签名已变化，禁止自动重新入队"
            _mark_interrupted(repository, report, record, error, stage_target="stale")
            continue

        status = record["status"]
        if stage_status not in _ALLOWED_STAGE_STATUSES_BY_JOB.get(status, frozenset()):
            _add_skipped_id(report, job_id)
            continue

        if status == "queued":
            _add_enqueue_id(report, job_id)
            continue

        if status == "running":
            if has_download_source(record):
                _requeue(repository, report, record, _DOWNLOAD_MESSAGE)
            elif record.get("provider_task_id"):
                _requeue(repository, report, record, _POLL_MESSAGE)
            else:
                error = (
                    "服务重启时动态任务处于运行中，且没有 Provider task_id 和可下载结果，"
                    "未自动重新提交"
                )
                _mark_interrupted(repository, report, record, error)
            continue

        if status == "waiting_provider":
            if record.get("provider_task_id"):
                _requeue(repository, report, record, _POLL_MESSAGE)
            elif has_download_source(record):
                _requeue(repository, report, record, _DOWNLOAD_MESSAGE)
            else:
                error = "等待 Provider 的动态任务缺少 task_id，且没有可下载结果，无法安全恢复"
                _mark_interrupted(repository, report, record, error)
            continue

        if status == "downloading":
            if has_download_source(record):
                _requeue(repository, report, record, _DOWNLOAD_MESSAGE)
            else:
                error = "下载中的动态任务缺少已保存结果地址，无法恢复"
                _mark_failed(repository, report, record, error)

    report.partial_failed_stage_ids.extend(
        stage_id
        for stage_id in reconcile_dynamic_workflow_runs(repository)
        if stage_id not in report.partial_failed_stage_ids
    )
    return report


def reconcile_dynamic_workflow_runs(repository: JobRepository) -> list[str]:
    partial_failed_stage_ids: list[str] = []
    with repository.engine.connect() as connection:
        stage_rows = connection.execute(
            select(job.c.workflow_run_id, job.c.workflow_stage_id)
            .where(
                job.c.workflow_run_id.is_not(None),
                job.c.workflow_stage_id.is_not(None),
            )
            .distinct()
        ).all()

    for stage_row in stage_rows:
        run_id = stage_row.workflow_run_id
        stage_id = stage_row.workflow_stage_id
        if not run_id or not stage_id:
            continue
        with repository.engine.connect() as connection:
            run_record = _row_dict(
                connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
            )
            stage_record = _row_dict(
                connection.execute(
                    select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                    )
                ).first()
            )
            statuses = list(
                connection.scalars(
                    select(job.c.status).where(
                        job.c.workflow_run_id == run_id,
                        job.c.workflow_stage_id == stage_id,
                    )
                )
            )
        if (
            run_record is None
            or stage_record is None
            or run_record["status"] in _PROTECTED_WORKFLOW_STATUSES
            or stage_record["status"] in _PROTECTED_STAGE_STATUSES
            or run_record["status"] not in _SAFE_PARTIAL_FAILED_RUN_STATUSES
            or stage_record["status"] not in {"queued", "running"}
        ):
            continue
        if not statuses or any(status not in _TERMINAL_JOB_STATUSES for status in statuses):
            continue
        if "failed" not in statuses:
            continue

        stage_changed, _run_changed = _update_workflow_state(
            repository,
            run_id=run_id,
            stage_id=stage_id,
            stage_target="partial_failed",
            stage_error="动态阶段存在失败任务",
            run_target="partial_failed",
            run_error="动态阶段存在失败任务",
            run_allowed_statuses=_SAFE_PARTIAL_FAILED_RUN_STATUSES,
        )
        if stage_changed:
            partial_failed_stage_ids.append(stage_id)
    return partial_failed_stage_ids


def enqueue_recovered_dynamic_jobs(
    repository: JobRepository,
    enqueue: Callable[[str], bool],
) -> DynamicRecoveryReport:
    report = recover_dynamic_jobs(repository)
    successful_ids: list[str] = []
    for job_id in report.enqueue_ids:
        if enqueue(job_id):
            successful_ids.append(job_id)
        else:
            report.skipped_ids.append(job_id)
    report.enqueue_ids = successful_ids
    return report


__all__ = [
    "DynamicRecoveryReport",
    "enqueue_recovered_dynamic_jobs",
    "reconcile_dynamic_workflow_runs",
    "recover_dynamic_jobs",
]
