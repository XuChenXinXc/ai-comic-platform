from __future__ import annotations

from app.jobs.repository import JobRepository, has_download_source


def recover_jobs(repository: JobRepository) -> list[str]:
    enqueue_ids: list[str] = []
    for record in repository.list_non_terminal():
        job_id = record["id"]
        status = record["status"]
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
