from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update

from app.db import Database, db
from app.jobs.repository import (
    NON_TERMINAL_STATUSES,
    JobNotFoundError,
    JobRepository,
    JobStateError,
    job_repository,
)
from app.jobs.runner import JobRunner, job_runner
from app.models.tables import (
    asset_image,
    job,
    keyframe_version,
    novel_analysis,
    screenplay,
    video_version,
)
from app.schemas import JobCreate
from app.services.errors import conflict, not_found
from app.services.utils import new_id, sanitize_error_message, utc_now

_HIDDEN_RESULT_KEYS = {
    "authorization",
    "content_base64",
    "cookie",
    "download_url",
    "local_file",
    "raw",
    "result_url",
    "token",
}
_CANCELLED_TARGET_ERROR = "关联任务已取消"
_MEDIA_TARGET_TABLES = {
    "asset_image": asset_image,
    "keyframe": keyframe_version,
    "video": video_version,
}


def _hide_sensitive_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[已隐藏]" if key.lower() in _HIDDEN_RESULT_KEYS else _hide_sensitive_result(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_hide_sensitive_result(item) for item in value]
    return value


class JobService:
    def __init__(
        self,
        database: Database | None = None,
        *,
        repository: JobRepository | None = None,
        runner: JobRunner | None = None,
    ) -> None:
        if repository is not None:
            self.repository = repository
        elif database is not None and database is not db:
            self.repository = JobRepository(database)
        else:
            self.repository = job_repository
        if runner is not None:
            self.runner = runner
        elif self.repository is job_repository:
            self.runner = job_runner
        else:
            self.runner = JobRunner(self.repository)

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result_data = result.pop("result_data", {})
        result.pop("input_data", None)
        result.pop("input_json", None)
        result["result_json"] = _hide_sensitive_result(result_data)
        result["error"] = sanitize_error_message(result.get("error", ""))
        return result

    def create(
        self,
        *,
        project_id: str,
        job_type: str,
        chapter_id: str | None = None,
        target_type: str = "",
        target_id: str | None = None,
        provider_kind: str = "",
        provider_id: str | None = None,
        input_data: dict[str, Any] | None = None,
        batch_id: str | None = None,
        progress_total: int = 1,
        workflow_run_id: str | None = None,
        workflow_stage_id: str | None = None,
        idempotency_key: str | None = None,
        input_signature: str | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        record = self.repository.create_job(
            JobCreate(
                project_id=project_id,
                chapter_id=chapter_id,
                workflow_run_id=workflow_run_id,
                workflow_stage_id=workflow_stage_id,
                idempotency_key=idempotency_key,
                job_type=job_type,
                target_type=target_type,
                target_id=target_id,
                provider_kind=provider_kind,
                provider_id=provider_id,
                input_data=input_data or {},
                batch_id=batch_id,
                input_signature=input_signature,
                depends_on=depends_on or [],
                progress_total=progress_total,
            )
        )
        if record["status"] in NON_TERMINAL_STATUSES:
            self.runner.enqueue(record["id"])
        return self._public_record(record)

    def create_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        batch_id = new_id("batch")
        records = [self.create(**item, batch_id=batch_id) for item in items]
        return {"batch_id": batch_id, "jobs": records}

    def get(self, job_id: str) -> dict[str, Any]:
        record = self.repository.get(job_id)
        if record is None:
            raise not_found("任务")
        return self._public_record(record)

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        job_type: str | None = None,
        batch_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            self._public_record(record)
            for record in self.repository.list(
                project_id=project_id,
                status=status,
                job_type=job_type,
                batch_id=batch_id,
                limit=limit,
                offset=offset,
            )
        ]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        records = self.repository.list_batch(batch_id)
        return {
            "batch_id": batch_id,
            "jobs": [self._public_record(record) for record in records],
        }

    def retry(self, job_id: str) -> dict[str, Any]:
        try:
            record = self.repository.retry(job_id)
        except JobNotFoundError as exc:
            raise not_found("任务") from exc
        except JobStateError as exc:
            raise conflict(str(exc)) from exc
        self.runner.enqueue(record["id"])
        return self._public_record(record)

    def cancel(self, job_id: str) -> dict[str, Any]:
        try:
            with self.repository.engine.begin() as connection:
                record = self.repository.cancel(job_id, connection=connection)
                self._sync_cancelled_target(record, connection=connection)
        except JobNotFoundError as exc:
            raise not_found("任务") from exc
        except JobStateError as exc:
            raise conflict(str(exc)) from exc
        return self._public_record(record)

    def update_progress(
        self,
        job_id: str,
        current: int,
        total: int | None = None,
        message: str = "",
    ) -> None:
        self.repository.update_progress(job_id, current, total=total, message=message)

    def _sync_cancelled_target(
        self,
        record: dict[str, Any],
        *,
        connection: Any,
    ) -> None:
        target_type = record["target_type"]
        target_id = record["target_id"]
        media_table = _MEDIA_TARGET_TABLES.get(target_type)
        if media_table is not None and target_id:
            connection.execute(
                update(media_table)
                .where(
                    media_table.c.id == target_id,
                    media_table.c.status.in_(NON_TERMINAL_STATUSES),
                )
                .values(status="failed", error=_CANCELLED_TARGET_ERROR)
            )
            return

        if record["job_type"] == "analyze_novel":
            targets = [(novel_analysis, ("analyze_novel", "generate_screenplay"))]
        elif record["job_type"] == "generate_screenplay":
            targets = [
                (
                    screenplay,
                    ("generate_screenplay", "generate_screenplay_scene"),
                ),
                (
                    novel_analysis,
                    ("analyze_novel", "generate_screenplay"),
                ),
            ]
        elif record["job_type"] == "generate_screenplay_scene":
            targets = [
                (
                    screenplay,
                    ("generate_screenplay", "generate_screenplay_scene"),
                )
            ]
        else:
            return

        # 写作类目标（分析/剧本）自多章节改造起归属章节，
        # 并发检查与状态回写都必须限定在同一章节内，否则会误伤其他章节。
        chapter_id = record.get("chapter_id")
        if not chapter_id:
            return
        for table, related_job_types in targets:
            other_active = connection.execute(
                select(job.c.id)
                .where(
                    job.c.id != record["id"],
                    job.c.chapter_id == chapter_id,
                    job.c.job_type.in_(related_job_types),
                    job.c.status.in_(NON_TERMINAL_STATUSES),
                )
                .limit(1)
            ).first()
            if other_active:
                continue
            connection.execute(
                update(table)
                .where(
                    table.c.chapter_id == chapter_id,
                    table.c.status == "generating",
                )
                .values(
                    status="failed",
                    error=_CANCELLED_TARGET_ERROR,
                    updated_at=utc_now(),
                )
            )

    def cleanup_old_jobs(
        self,
        project_id: str,
        days: int = 7,
        keep_failed: bool = True,
    ) -> int:
        """
        清理旧的已完成任务记录，防止内存泄漏。

        Args:
            project_id: 项目 ID
            days: 保留最近 N 天的记录（默认 7 天）
            keep_failed: 是否保留失败的任务（默认 True）

        Returns:
            删除的任务数量
        """
        cutoff_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        # count 与 delete 在同一事务中执行，保持 engine.begin() 统一事务风格
        with self.repository.engine.begin() as connection:
            # 构建删除条件
            conditions = [
                job.c.project_id == project_id,
                job.c.finished_at.isnot(None),
                job.c.finished_at < cutoff_date,
            ]

            if keep_failed:
                # 只删除成功的任务，保留失败的供调试
                conditions.append(job.c.status == "completed")
            else:
                # 删除所有已完成的任务（成功 + 失败）
                conditions.append(job.c.status.in_(["completed", "failed", "cancelled"]))

            # 先查询要删除的任务数量
            delete_count = len(
                connection.scalars(select(job.c.id).where(*conditions)).all()
            )

            if delete_count > 0:
                # 执行删除（同事务提交）
                connection.execute(delete(job).where(*conditions))

        return delete_count

    def cleanup_all_projects(self, days: int = 7, keep_failed: bool = True) -> dict[str, int]:
        """
        清理所有项目的旧任务记录。

        Args:
            days: 保留最近 N 天的记录（默认 7 天）
            keep_failed: 是否保留失败的任务（默认 True）

        Returns:
            每个项目删除的任务数量
        """
        from app.models.tables import project as project_table

        results = {}
        with self.repository.engine.connect() as connection:
            project_ids = [
                row.id for row in connection.execute(select(project_table.c.id))
            ]

        for project_id in project_ids:
            deleted = self.cleanup_old_jobs(project_id, days, keep_failed)
            if deleted > 0:
                results[project_id] = deleted

        return results
