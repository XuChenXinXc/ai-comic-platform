from __future__ import annotations

from app.jobs.repository import JobRepository, has_download_source


def _dependency_ids(record: dict) -> list[str]:
    dependencies = record.get("depends_on")
    if not isinstance(dependencies, list):
        input_data = record.get("input_data")
        dependencies = input_data.get("depends_on", []) if isinstance(input_data, dict) else []
    return [str(value).strip() for value in dependencies if str(value).strip()]


def _dependency_error(repository: JobRepository, record: dict) -> str | None:
    for dependency_id in _dependency_ids(record):
        dependency = repository.get(dependency_id)
        if dependency is None:
            return f"任务依赖不存在：{dependency_id}"
        status = str(dependency.get("status") or "")
        if status in {"failed", "interrupted", "cancelled", "stale"}:
            return f"任务依赖未成功完成：{dependency_id}（{status}）"
    return None


def _dependencies_ready(repository: JobRepository, record: dict) -> bool:
    return all(
        (dependency := repository.get(dependency_id)) is not None
        and dependency.get("status") == "completed"
        for dependency_id in _dependency_ids(record)
    )


def recover_jobs(repository: JobRepository) -> list[str]:
    enqueue_ids: list[str] = []
    for record in repository.list_non_terminal():
        job_id = record["id"]
        status = record["status"]
        dependency_error = _dependency_error(repository, record)
        if dependency_error is not None:
            repository.fail(job_id, dependency_error)
            continue
        if not _dependencies_ready(repository, record):
            if _dependency_ids(record) and status != "queued":
                repository.requeue_for_recovery(
                    job_id,
                    expected_status=status,
                    message="任务依赖尚未完成，保持 queued",
                )
            continue
        if status == "queued":
            enqueue_ids.append(job_id)
            continue
        if status == "running":
            if has_download_source(record):
                if repository.requeue_for_recovery(
                    job_id,
                    expected_status="running",
                    message="服务重启后继续下载已保存的 Provider 结果",
                ):
                    enqueue_ids.append(job_id)
            elif record["provider_task_id"]:
                if repository.requeue_for_recovery(
                    job_id,
                    expected_status="running",
                    message="服务重启后继续轮询原 Provider 任务",
                ):
                    enqueue_ids.append(job_id)
            else:
                repository.interrupt(
                    job_id,
                    "服务重启时任务处于 running，且没有 Provider task_id，未自动重新提交",
                )
            continue
        if status == "waiting_provider":
            if record["provider_task_id"]:
                if repository.requeue_for_recovery(
                    job_id,
                    expected_status="waiting_provider",
                    message="服务重启后继续轮询原 Provider 任务",
                ):
                    enqueue_ids.append(job_id)
            else:
                repository.interrupt(
                    job_id,
                    "等待 Provider 的任务缺少 task_id，无法安全恢复",
                )
            continue
        if status == "downloading":
            if has_download_source(record):
                if repository.requeue_for_recovery(
                    job_id,
                    expected_status="downloading",
                    message="服务重启后继续下载已保存的 Provider 结果",
                ):
                    enqueue_ids.append(job_id)
            else:
                repository.fail(job_id, "下载中的任务缺少已保存结果地址，无法恢复")
    return enqueue_ids
