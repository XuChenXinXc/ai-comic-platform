"""统一的启动期恢复入口。

现在只保留普通任务恢复路径：扫描非终态 Job 并决定哪些 ID 需要重新入队，
由调用方统一 enqueue。
"""

from __future__ import annotations

from app.jobs.recovery import recover_jobs
from app.jobs.repository import JobRepository


def recover_all_once(repository: JobRepository) -> list[str]:
    """启动期恢复一次：执行普通恢复路径的状态机副作用并返回入队 ID。"""
    return recover_jobs(repository)


__all__ = ["recover_all_once"]
