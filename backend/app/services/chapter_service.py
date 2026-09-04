from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db
from app.models.tables import (
    chapter,
    chapter_composition,
    job,
    keyframe_version,
    project,
    shot,
    shot_audio,
    video_version,
)
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import FileService, FileServiceError, derive_file_service
from app.services.signature_service import source_signature
from app.services.utils import new_id, row_dict, utc_now

logger = logging.getLogger(__name__)

_ACTIVE_JOB_STATUSES = {"queued", "running", "waiting_provider", "downloading"}


class ChapterService:
    """章节：一个项目（一本小说）下的一章，持有正文、分析、剧本与分镜。

    素材库仍属于项目层级，全书共用，不随章节创建或删除而变化。
    """

    def __init__(
        self,
        database: Database | None = None,
        storage: FileService | None = None,
    ) -> None:
        self.database = database or db
        if storage is not None:
            # 不改写已绑定数据库的实例（如全局 file_service 单例），
            # 避免跨实例污染；未绑定 database 的注入实例仍绑定当前数据库，
            # 保持既有调用方（自建 FileService(workspace)）的兼容行为。
            if storage.database is None:
                storage.database = self.database
            self.storage = storage
        else:
            # 默认派生绑定当前 database 的独立实例，不改写全局单例
            self.storage = derive_file_service(self.database)

    def _project(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return record

    def get(self, chapter_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(chapter).where(chapter.c.id == chapter_id)).first()
            )
        if record is None:
            raise not_found("章节")
        return record

    def list(self, project_id: str) -> list[dict[str, Any]]:
        self._project(project_id)
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(chapter)
                .where(chapter.c.project_id == project_id)
                .order_by(chapter.c.chapter_no)
            ).all()
            counts = dict(
                connection.execute(
                    select(shot.c.chapter_id, func.count())
                    .where(shot.c.project_id == project_id)
                    .group_by(shot.c.chapter_id)
                ).all()
            )
        records = []
        for row in rows:
            record = dict(row._mapping)
            record["shot_count"] = counts.get(record["id"], 0)
            records.append(record)
        return records

    def _next_chapter_no(self, connection: Any, project_id: str) -> int:
        current = connection.execute(
            select(func.max(chapter.c.chapter_no)).where(chapter.c.project_id == project_id)
        ).scalar()
        return (current or 0) + 1

    def create(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        project_record = self._project(project_id)
        now = utc_now()
        text = str(values.get("source_text") or "")
        if not text.strip() and str(project_record.get("source_text") or "").strip():
            # 首章继承项目创建时录入的小说正文：项目正文在分章模型下不再被
            # 任何生成链路读取，不继承会导致用户录入的小说直接丢失。
            with self.database.engine.connect() as connection:
                existing = connection.execute(
                    select(chapter.c.id).where(chapter.c.project_id == project_id).limit(1)
                ).first()
            if existing is None:
                text = str(project_record["source_text"])
        record: dict[str, Any] = {
            "id": new_id("chapter"),
            "project_id": project_id,
            "title": str(values.get("title") or ""),
            "source_text": text,
            "source_signature": source_signature(text),
            "source_revision": 1,
            "status": values.get("status") or "draft",
            "created_at": now,
            "updated_at": now,
        }
        with self.database.engine.begin() as connection:
            record["chapter_no"] = values.get("chapter_no") or self._next_chapter_no(
                connection, project_id
            )
            try:
                connection.execute(insert(chapter).values(**record))
            except IntegrityError as exc:
                raise conflict("章节编号已存在") from exc
        return self.get(record["id"])

    def update(self, chapter_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self.get(chapter_id)
        changes = {key: value for key, value in values.items() if value is not None}
        if not changes:
            return current
        if "source_text" in changes:
            signature = source_signature(changes["source_text"])
            changes["source_signature"] = signature
            # 正文变化才推进修订号，避免仅改标题也让下游内容标记为过期
            if signature != current["source_signature"]:
                changes["source_revision"] = current["source_revision"] + 1
        changes["updated_at"] = utc_now()
        with self.database.engine.begin() as connection:
            try:
                connection.execute(
                    update(chapter).where(chapter.c.id == chapter_id).values(**changes)
                )
            except IntegrityError as exc:
                raise conflict("章节编号已存在") from exc
        return self.get(chapter_id)

    def _ensure_no_active_jobs(self, connection: Any, chapter_id: str) -> None:
        shot_ids = set(
            connection.execute(select(shot.c.id).where(shot.c.chapter_id == chapter_id)).scalars()
        )
        keyframe_ids = (
            set(
                connection.execute(
                    select(keyframe_version.c.id).where(keyframe_version.c.shot_id.in_(shot_ids))
                ).scalars()
            )
            if shot_ids
            else set()
        )
        video_ids = (
            set(
                connection.execute(
                    select(video_version.c.id).where(video_version.c.shot_id.in_(shot_ids))
                ).scalars()
            )
            if shot_ids
            else set()
        )
        active = connection.execute(
            select(job.c.id).where(
                job.c.status.in_(_ACTIVE_JOB_STATUSES),
                (
                    (job.c.chapter_id == chapter_id)
                    | ((job.c.target_type == "shot") & job.c.target_id.in_(shot_ids or {""}))
                    | (
                        (job.c.target_type == "keyframe")
                        & job.c.target_id.in_(keyframe_ids or {""})
                    )
                    | ((job.c.target_type == "video") & job.c.target_id.in_(video_ids or {""}))
                ),
            )
        ).first()
        if active:
            raise conflict("章节仍有进行中的任务，请等待完成后再删除")

    def delete_preview(self, chapter_id: str) -> dict[str, Any]:
        """删除前的影响面预览，供前端二次确认展示。"""
        current = self.get(chapter_id)
        with self.database.engine.connect() as connection:
            shot_ids = list(
                connection.execute(
                    select(shot.c.id).where(shot.c.chapter_id == chapter_id)
                ).scalars()
            )
            keyframes = (
                connection.execute(
                    select(func.count()).where(keyframe_version.c.shot_id.in_(shot_ids))
                ).scalar_one()
                if shot_ids
                else 0
            )
            videos = (
                connection.execute(
                    select(func.count()).where(video_version.c.shot_id.in_(shot_ids))
                ).scalar_one()
                if shot_ids
                else 0
            )
        return {
            "chapter_id": chapter_id,
            "chapter_no": current["chapter_no"],
            "title": current["title"],
            "shot_count": len(shot_ids),
            "keyframe_count": keyframes,
            "video_count": videos,
            "note": "素材库属于全书，不会被删除",
        }

    def delete(self, chapter_id: str) -> None:
        """级联删除章节及其分镜、关键帧、视频、配音、成片与媒体文件；素材库不受影响。"""
        current = self.get(chapter_id)
        with self.database.engine.begin() as connection:
            self._ensure_no_active_jobs(connection, chapter_id)
            shot_ids = list(
                connection.execute(
                    select(shot.c.id).where(shot.c.chapter_id == chapter_id)
                ).scalars()
            )
            media_paths: list[str] = []
            if shot_ids:
                for table in (keyframe_version, video_version, shot_audio):
                    media_paths.extend(
                        connection.execute(
                            select(table.c.file_path).where(
                                table.c.shot_id.in_(shot_ids),
                                table.c.file_path.is_not(None),
                            )
                        ).scalars()
                    )
                    if table is video_version:
                        media_paths.extend(
                            connection.execute(
                                select(table.c.end_frame_path).where(
                                    table.c.shot_id.in_(shot_ids),
                                    table.c.end_frame_path.is_not(None),
                                )
                            ).scalars()
                        )
            # 本章成片文件（CASCADE 删除行之前收集）
            media_paths.extend(
                connection.execute(
                    select(chapter_composition.c.file_path).where(
                        chapter_composition.c.chapter_id == chapter_id,
                        chapter_composition.c.file_path.is_not(None),
                    )
                ).scalars()
            )
            # 清理本章遗留的已完成任务（进行中任务已在 _ensure_no_active_jobs 拦截）
            keyframe_ids = (
                set(
                    connection.execute(
                        select(keyframe_version.c.id).where(
                            keyframe_version.c.shot_id.in_(shot_ids)
                        )
                    ).scalars()
                )
                if shot_ids
                else set()
            )
            video_ids = (
                set(
                    connection.execute(
                        select(video_version.c.id).where(video_version.c.shot_id.in_(shot_ids))
                    ).scalars()
                )
                if shot_ids
                else set()
            )
            connection.execute(
                delete(job).where(
                    (job.c.chapter_id == chapter_id)
                    | ((job.c.target_type == "shot") & job.c.target_id.in_(shot_ids or {""}))
                    | (
                        (job.c.target_type == "keyframe")
                        & job.c.target_id.in_(keyframe_ids or {""})
                    )
                    | ((job.c.target_type == "video") & job.c.target_id.in_(video_ids or {""}))
                )
            )
            # novel_analysis / screenplay / shot / 配音 / 成片行 由外键 CASCADE 清理
            connection.execute(delete(chapter).where(chapter.c.id == chapter_id))
            remaining = list(
                connection.execute(
                    select(chapter.c.id)
                    .where(chapter.c.project_id == current["project_id"])
                    .order_by(chapter.c.chapter_no)
                ).scalars()
            )
            self._renumber(connection, remaining)
        # 文件删除失败只记录日志继续清理，不让单个失败中断后续删除或导致 500
        failed_paths: list[str] = []
        for path in media_paths:
            try:
                self.storage.delete_file(current["project_id"], path)
            except (FileServiceError, OSError) as exc:
                logger.warning("删除章节媒体文件失败：%s（%s）", path, exc)
                failed_paths.append(path)
        if failed_paths:
            logger.warning(
                "章节删除完成，但有 %d 个媒体文件删除失败：%s",
                len(failed_paths),
                failed_paths,
            )

    @staticmethod
    def _renumber(connection: Any, ordered_ids: list[str]) -> None:
        """按给定顺序重排章节号；先移到临时区间避免唯一约束冲突。"""
        if not ordered_ids:
            return
        temporary_start = 100000
        for offset, chapter_id in enumerate(ordered_ids):
            connection.execute(
                update(chapter)
                .where(chapter.c.id == chapter_id)
                .values(chapter_no=temporary_start + offset)
            )
        now = utc_now()
        for index, chapter_id in enumerate(ordered_ids, start=1):
            connection.execute(
                update(chapter)
                .where(chapter.c.id == chapter_id)
                .values(chapter_no=index, updated_at=now)
            )

    def reorder(self, project_id: str, ids: list[str]) -> list[dict[str, Any]]:
        self._project(project_id)
        with self.database.engine.begin() as connection:
            actual = list(
                connection.execute(
                    select(chapter.c.id).where(chapter.c.project_id == project_id)
                ).scalars()
            )
            if len(ids) != len(actual) or set(ids) != set(actual):
                raise bad_request("ids 必须包含当前项目的全部章节且不能包含其他章节")
            self._renumber(connection, ids)
        return self.list(project_id)
