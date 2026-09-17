from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select, update

from app.db import Database, db
from app.models.tables import api_provider, job, project
from app.services.errors import ServiceError, bad_request, conflict, not_found
from app.services.file_service import FileService, FileServiceError, derive_file_service
from app.services.setting_service import get_default_style_prompt
from app.services.signature_service import source_signature
from app.services.utils import new_id, row_dict, utc_now

logger = logging.getLogger(__name__)

# 项目列表为全量返回；给一个宽松防御上限避免无界扫描，达到上限时告警。
_MAX_PROJECT_LIST_LIMIT = 2000


class ProjectService:
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

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result.pop("storage_name", None)
        result["storage_path"] = str(self.storage.project_path(record["id"]))
        result["is_free_create"] = bool(record.get("is_free_create", 0))
        visual_bible_json = result.get("visual_bible_json")
        if isinstance(visual_bible_json, str) and visual_bible_json.strip():
            try:
                result["visual_bible"] = json.loads(visual_bible_json)
            except ValueError:
                result["visual_bible"] = None
        else:
            result["visual_bible"] = None
        return result

    @staticmethod
    def _validate_provider_defaults(connection: Any, values: dict[str, Any]) -> None:
        for kind in ("text", "image", "video"):
            field = f"default_{kind}_provider_id"
            provider_id = values.get(field)
            if not provider_id:
                continue
            valid = connection.execute(
                select(api_provider.c.id).where(
                    api_provider.c.id == provider_id,
                    api_provider.c.kind == kind,
                    api_provider.c.enabled.is_(True),
                )
            ).first()
            if not valid:
                raise bad_request(f"{field} 必须指向已启用的 {kind} Provider")

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        source_text = values.get("source_text", "")
        # 统一画风留空时回填平台默认画风（数据库设置，非代码写死）。
        if not str(values.get("style_prompt") or "").strip():
            values["style_prompt"] = get_default_style_prompt(self.database)
        project_id = new_id("project")
        defaults = {}
        with self.database.engine.connect() as connection:
            self._validate_provider_defaults(connection, values)
            for kind in ("text", "image", "video"):
                field = f"default_{kind}_provider_id"
                defaults[field] = (
                    values.get(field)
                    or connection.execute(
                        select(api_provider.c.id).where(
                            api_provider.c.kind == kind,
                            api_provider.c.is_default.is_(True),
                            api_provider.c.enabled.is_(True),
                        )
                    ).scalar()
                )
        try:
            storage_path, storage_name = self.storage.create_categorized_project(values["name"])
        except FileServiceError as exc:
            raise bad_request(str(exc)) from exc
        record = {
            "id": project_id,
            **values,
            **defaults,
            "storage_name": storage_name,
            "storage_root": str(storage_path.parent),
            "storage_layout": "categorized",
            "source_signature": source_signature(source_text),
            "source_revision": 1,
            "is_free_create": int(bool(values.get("is_free_create", False))),
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self.database.engine.begin() as connection:
                connection.execute(insert(project).values(**record))
        except Exception:
            # 清理预留目录本身失败时不能顶替原始异常：独立捕获并记录，
            # 再抛回根因，便于定位真正的写入失败原因。
            try:
                self.storage.delete_reserved_project(storage_path.parent, storage_path)
            except FileServiceError as cleanup_error:
                logger.warning("项目记录创建失败后清理预留目录失败：%s", cleanup_error)
            raise
        return self.get(record["id"])

    def list(
        self,
        kind: str | None = None,
        *,
        limit: int = _MAX_PROJECT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        statement = (
            select(project)
            .order_by(project.c.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if kind == "free":
            statement = statement.where(project.c.is_free_create == 1)
        elif kind == "regular":
            statement = statement.where(project.c.is_free_create == 0)
        with self.database.engine.connect() as connection:
            records = [
                self._public_record(dict(row._mapping)) for row in connection.execute(statement)
            ]
        if len(records) >= limit:
            logger.warning(
                "项目列表达到上限 %d，可能存在未返回的项目",
                limit,
            )
        return records

    def get(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return self._public_record(record)

    @staticmethod
    def _next_updated_at(previous: str) -> str:
        now = utc_now()
        if now > previous:
            return now
        try:
            return (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat()
        except ValueError:
            return now

    def update(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self.get(project_id)
        # 显式传 null 的非 Provider 字段视为「保持不变」：这些列 NOT NULL，
        # 裸写 None 会触发 IntegrityError 500；Provider 默认字段 null 合法（清除默认）。
        provider_default_fields = {
            "default_text_provider_id",
            "default_image_provider_id",
            "default_video_provider_id",
        }
        changes = {
            key: value
            for key, value in values.items()
            if value is not None or key in provider_default_fields
        }
        if "source_text" in changes:
            signature = source_signature(changes["source_text"])
            changes["source_signature"] = signature
            if signature != current["source_signature"]:
                changes["source_revision"] = current["source_revision"] + 1
        if not changes:
            return current
        changes["updated_at"] = self._next_updated_at(current["updated_at"])
        with self.database.engine.begin() as connection:
            self._validate_provider_defaults(connection, changes)
            result = connection.execute(
                update(project).where(project.c.id == project_id).values(**changes)
            )
            if result.rowcount != 1:
                # 并发删除窗口内更新 0 行：不能静默当成功返回。
                raise not_found("项目")
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        try:
            self.get(project_id)
        except FileServiceError as exc:
            raise bad_request(str(exc)) from exc
        target = None
        tombstone = None
        try:
            with self.database.engine.begin() as connection:
                locked = connection.execute(
                    update(project)
                    .where(project.c.id == project_id)
                    .values(updated_at=project.c.updated_at)
                ).rowcount
                if locked != 1:
                    raise not_found("项目")
                active_job = connection.execute(
                    select(job.c.id).where(
                        job.c.project_id == project_id,
                        job.c.status.in_({"queued", "running", "waiting_provider", "downloading"}),
                    )
                ).first()
                if active_job:
                    raise conflict("项目仍有进行中的任务，不能删除")
                try:
                    target, tombstone = self.storage.reserve_project_deletion(project_id)
                except FileServiceError as exc:
                    raise bad_request(str(exc)) from exc
                deleted = connection.execute(
                    delete(project).where(project.c.id == project_id)
                ).rowcount
                if deleted != 1:
                    raise not_found("项目")
        except Exception:
            if target is not None:
                try:
                    self.storage.restore_reserved_project(target, tombstone)
                except FileServiceError as exc:
                    raise ServiceError(
                        500,
                        f"项目删除失败，且项目目录恢复失败：{exc}",
                    ) from exc
            raise
        try:
            self.storage.finalize_reserved_project(tombstone)
        except FileServiceError as exc:
            raise ServiceError(
                500,
                f"项目记录已删除，但磁盘目录清理失败：{exc}",
            ) from exc
