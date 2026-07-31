from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update

from app.db import Database, db
from app.models.tables import api_provider, job, project
from app.services.errors import ServiceError, bad_request, conflict, not_found
from app.services.file_service import FileService, FileServiceError, file_service
from app.services.signature_service import source_signature
from app.services.utils import new_id, row_dict, utc_now


class ProjectService:
    def __init__(
        self,
        database: Database | None = None,
        storage: FileService | None = None,
    ) -> None:
        self.database = database or db
        self.storage = storage or file_service
        self.storage.database = self.database

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        result.pop("storage_name", None)
        result["storage_path"] = str(self.storage.project_path(record["id"]))
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
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self.database.engine.begin() as connection:
                connection.execute(insert(project).values(**record))
        except Exception:
            self.storage.delete_reserved_project(storage_path.parent, storage_path)
            raise
        return self.get(record["id"])

    def list(self) -> list[dict[str, Any]]:
        statement = select(project).order_by(project.c.updated_at.desc())
        with self.database.engine.connect() as connection:
            return [
                self._public_record(dict(row._mapping)) for row in connection.execute(statement)
            ]

    def get(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return self._public_record(record)

    def update(self, project_id: str, values: dict[str, Any]) -> dict[str, Any]:
        current = self.get(project_id)
        changes = dict(values)
        if "source_text" in changes:
            signature = source_signature(changes["source_text"])
            changes["source_signature"] = signature
            if signature != current["source_signature"]:
                changes["source_revision"] = current["source_revision"] + 1
        if not changes:
            return current
        changes["updated_at"] = utc_now()
        with self.database.engine.begin() as connection:
            self._validate_provider_defaults(connection, changes)
            connection.execute(update(project).where(project.c.id == project_id).values(**changes))
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
