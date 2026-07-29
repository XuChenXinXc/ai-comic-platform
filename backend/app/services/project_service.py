from __future__ import annotations

from typing import Any

from sqlalchemy import delete, insert, select, update

from app.db import Database, db
from app.models.tables import api_provider, job, project
from app.services.errors import bad_request, conflict, not_found
from app.services.file_service import file_service
from app.services.signature_service import source_signature
from app.services.utils import new_id, row_dict, utc_now


class ProjectService:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database or db

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
        record = {
            "id": new_id("project"),
            **values,
            **defaults,
            "source_signature": source_signature(source_text),
            "source_revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        with self.database.engine.begin() as connection:
            connection.execute(insert(project).values(**record))
        return self.get(record["id"])

    def list(self) -> list[dict[str, Any]]:
        statement = select(project).order_by(project.c.updated_at.desc())
        with self.database.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def get(self, project_id: str) -> dict[str, Any]:
        with self.database.engine.connect() as connection:
            record = row_dict(
                connection.execute(select(project).where(project.c.id == project_id)).first()
            )
        if record is None:
            raise not_found("项目")
        return record

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
        self.get(project_id)
        with self.database.engine.begin() as connection:
            active_job = connection.execute(
                select(job.c.id).where(
                    job.c.project_id == project_id,
                    job.c.status.in_({"queued", "running", "waiting_provider", "downloading"}),
                )
            ).first()
            if active_job:
                raise conflict("项目仍有进行中的任务，不能删除")
            connection.execute(delete(project).where(project.c.id == project_id))
        file_service.delete_project(project_id)
