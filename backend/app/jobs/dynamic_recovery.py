from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from app.db import utc_now
from app.dynamic_pipeline.stage_catalog import get_stage_prerequisites
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository, has_download_source
from app.models.tables import job, workflow_run, workflow_stage

_PROTECTED_WORKFLOW_STATUSES = {"blocked", "cancelled", "stale", "confirmed"}
_PROTECTED_STAGE_STATUSES = {"blocked", "cancelled", "stale", "confirmed"}
_PROTECTED_STAGE_RUN_STATUSES = _PROTECTED_WORKFLOW_STATUSES
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
_SAFE_INTERRUPTED_RUN_STATUSES = {"queued", "running"}
_SAFE_PARTIAL_FAILED_RUN_STATUSES = {"queued", "running"}

_UPSTREAM_NON_EXECUTABLE_STATUSES = {"blocked", "cancelled", "stale"}

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


def _latest_stage_record(
    repository: JobRepository,
    run_id: str,
    stage_key: str,
) -> dict[str, Any] | None:
    with repository.engine.connect() as connection:
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
    return _row_dict(row)


def _dependency_failure(
    repository: JobRepository,
    record: Mapping[str, Any],
) -> tuple[str, str] | None:
    dependencies = record.get("depends_on")
    if not isinstance(dependencies, list):
        return None
    for dependency_id in dependencies:
        dependency = repository.get(str(dependency_id))
        if dependency is None:
            return str(dependency_id), "依赖 Job 不存在"
        status = str(dependency.get("status") or "")
        if status in {"failed", "cancelled", "interrupted", "stale"}:
            return str(dependency_id), f"依赖 Job 状态为 {status}"
    return None


def _dependency_pending(repository: JobRepository, record: Mapping[str, Any]) -> bool:
    dependencies = record.get("depends_on")
    if not isinstance(dependencies, list):
        return False
    return any(
        (dependency := repository.get(str(dependency_id))) is not None
        and dependency.get("status") not in _TERMINAL_JOB_STATUSES
        for dependency_id in dependencies
    )


def _stage_input_signature(record: Mapping[str, Any]) -> str:
    """读取 Job 对应的 stage 输入签名；媒体 Job 自身使用 media signature。"""
    if str(record.get("job_type") or "") == "dynamic_stage_generate":
        return str(record.get("input_signature") or "")
    input_data = record.get("input_data")
    if isinstance(input_data, Mapping):
        direct = str(input_data.get("workflow_stage_input_signature") or "")
        if direct:
            return direct
        provenance = input_data.get("provenance_json") or input_data.get("provenance")
        if isinstance(provenance, Mapping):
            return str(provenance.get("upstream_input_signature") or "")
    return ""


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


def _has_non_executable_upstream(
    repository: JobRepository,
    stage_record: dict[str, Any],
) -> bool:
    try:
        prerequisites = get_stage_prerequisites(str(stage_record.get("stage_key") or ""))
    except ValueError:
        return True
    if not prerequisites:
        return False
    with repository.engine.connect() as connection:
        statuses = connection.scalars(
            select(workflow_stage.c.status).where(
                workflow_stage.c.workflow_run_id == stage_record.get("workflow_run_id"),
                workflow_stage.c.stage_key.in_(prerequisites),
            )
        ).all()
    return any(status in _UPSTREAM_NON_EXECUTABLE_STATUSES for status in statuses)


def _stage_dependency_state(
    repository: JobRepository,
    stage_record: Mapping[str, Any],
) -> tuple[str, str | None]:
    """按最新 attempt 检查动态阶段的隐式前置阶段。"""
    run_id = str(stage_record.get("workflow_run_id") or "")
    stage_key = str(stage_record.get("stage_key") or "")
    try:
        prerequisites = get_stage_prerequisites(stage_key)
    except ValueError as exc:
        return "failed", str(exc)
    pending = False
    for prerequisite_key in prerequisites:
        prerequisite = _latest_stage_record(repository, run_id, prerequisite_key)
        if prerequisite is None:
            return "failed", f"动态任务前置阶段不存在：{prerequisite_key}"
        status = str(prerequisite.get("status") or "")
        if status in {
            "failed",
            "partial_failed",
            "interrupted",
            "cancelled",
            "stale",
            "blocked",
        }:
            return (
                "failed",
                f"动态任务前置阶段未成功完成：{prerequisite_key}（{status}）",
            )
        if status != "confirmed":
            pending = True
    return ("pending", None) if pending else ("ready", None)


def _reconcile_queued_stage_jobs(repository: JobRepository) -> list[str]:
    """只为当前 attempt 且前置阶段已确认的 queued stage 补建 Job。"""
    bridge = DynamicJobBridge(repository)
    with repository.engine.connect() as connection:
        candidates = connection.execute(
            select(workflow_run.c.id.label("run_id"), workflow_stage.c.id.label("stage_id"))
            .select_from(
                workflow_run.join(
                    workflow_stage,
                    workflow_stage.c.workflow_run_id == workflow_run.c.id,
                )
            )
            .where(
                workflow_run.c.status.not_in(_PROTECTED_WORKFLOW_STATUSES),
                workflow_stage.c.status == "queued",
            )
            .order_by(workflow_run.c.id, workflow_stage.c.ordinal, workflow_stage.c.id)
        ).all()

    enqueue_ids: list[str] = []
    for candidate in candidates:
        run_id = str(candidate.run_id)
        stage_id = str(candidate.stage_id)
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
            if run_record is None or stage_record is None:
                continue
            latest = _latest_stage_record(repository, run_id, str(stage_record.get("stage_key")))
            if latest is None or latest.get("id") != stage_id:
                continue
            dependency_state, dependency_error = _stage_dependency_state(repository, stage_record)
        if dependency_error is not None:
            _update_workflow_state(
                repository,
                run_id=run_id,
                stage_id=stage_id,
                stage_target="partial_failed",
                stage_error=dependency_error,
                run_target="partial_failed",
                run_error=dependency_error,
                run_allowed_statuses=_SAFE_PARTIAL_FAILED_RUN_STATUSES,
            )
            continue
        if dependency_state != "ready":
            continue
        with repository.engine.begin() as connection:
            run_record = _row_dict(
                connection.execute(select(workflow_run).where(workflow_run.c.id == run_id)).first()
            )
            stage_record = _row_dict(
                connection.execute(
                    select(workflow_stage).where(
                        workflow_stage.c.id == stage_id,
                        workflow_stage.c.workflow_run_id == run_id,
                        workflow_stage.c.status == "queued",
                    )
                ).first()
            )
            if run_record is None or stage_record is None:
                continue
            existing = bridge._find_by_stage_signature(
                connection,
                stage_id,
                str(stage_record.get("input_signature") or ""),
            )
            if existing is not None:
                reference = bridge._existing_reference(
                    connection,
                    existing,
                    str(stage_record.get("status") or ""),
                    retry_failed=False,
                )
            else:
                reference = bridge.ensure_stage_job_in_transaction(
                    connection,
                    run_record,
                    stage_record,
                )
            if reference.status == "queued" and reference.job_id not in enqueue_ids:
                enqueue_ids.append(reference.job_id)
    return enqueue_ids


def recover_dynamic_jobs(repository: JobRepository) -> DynamicRecoveryReport:
    report = DynamicRecoveryReport()
    reconciled_ids = _reconcile_queued_stage_jobs(repository)
    for job_id in reconciled_ids:
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

        latest_stage = _latest_stage_record(
            repository,
            str(run_id),
            str(stage_record.get("stage_key") or ""),
        )
        if latest_stage is None or latest_stage.get("id") != stage_record.get("id"):
            _mark_interrupted(
                repository,
                report,
                record,
                "动态任务关联的 stage attempt 已过期，禁止自动恢复旧 Job",
                stage_target=None,
                run_target=None,
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

        stage_dependency_state, stage_dependency_error = _stage_dependency_state(
            repository,
            stage_record,
        )
        if stage_dependency_error is not None:
            _mark_failed(
                repository,
                report,
                record,
                stage_dependency_error,
            )
            continue
        if stage_dependency_state == "pending":
            if record["status"] != "queued":
                if repository.requeue_for_recovery(
                    job_id,
                    expected_status=record["status"],
                    message="动态任务前置阶段尚未确认，保持 queued",
                ):
                    _add_skipped_id(report, job_id)
            else:
                _add_skipped_id(report, job_id)
            continue

        dependency_failure = _dependency_failure(repository, record)
        if dependency_failure is not None:
            dependency_id, dependency_reason = dependency_failure
            _mark_failed(
                repository,
                report,
                record,
                f"动态任务依赖 {dependency_id} 未成功完成：{dependency_reason}",
            )
            continue

        job_signature = _stage_input_signature(record)
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
            if _dependency_pending(repository, record):
                _add_skipped_id(report, job_id)
            else:
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
