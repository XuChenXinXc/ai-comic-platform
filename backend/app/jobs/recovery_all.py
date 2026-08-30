"""统一的启动期恢复入口。

`recover_dynamic_jobs` 与 `recover_jobs` 各自会扫描非终态 Job 并决定哪些 ID 需要重新入队。
直接先后调用两条路径会让同一组 Job 在两个事务里各被读到一次，存在重复入队的窗口。

本模块只做外层包装：先后调用两条恢复路径的"读 + 副作用"逻辑（保留
fail / interrupt / requeue 等状态机副作用），把"应当入队"的 ID 集合做 union + dedupe，
由调用方统一 enqueue，避免两条路径重复入队。
"""

from __future__ import annotations

from app.jobs.dynamic_recovery import recover_dynamic_jobs
from app.jobs.recovery import recover_jobs
from app.jobs.repository import JobRepository


def recover_all_once(repository: JobRepository) -> list[str]:
    """启动期恢复一次：合并 dynamic + 普通路径的入队 ID，去重后返回。

    内部仍会执行 `recover_dynamic_jobs` 与 `recover_jobs` 各自的状态机副作用
    （fail / interrupt / requeue / reconcile），保证启动恢复语义不变；
    仅把"决定要 enqueue 哪些 Job"统一到调用方，避免重复入队。
    """
    dynamic_report = recover_dynamic_jobs(repository)
    normal_ids = recover_jobs(repository)
    combined: dict[str, None] = {}
    for job_id in (*dynamic_report.enqueue_ids, *normal_ids):
        if job_id:
            combined.setdefault(job_id, None)
    return list(combined)


__all__ = ["recover_all_once"]
