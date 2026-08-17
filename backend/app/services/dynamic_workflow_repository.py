from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from app.db import Database, db
from app.dynamic_pipeline.stage_catalog import (
    STAGE_KEYS,
    get_descendant_stage_keys,
    get_stage_ordinal,
)
from app.dynamic_pipeline.state_machine import RevisionConflict
from app.models.tables import chapter, project, workflow_run, workflow_stage
from app.services.utils import json_dumps, json_loads, new_id, row_dict, utc_now

_RUN_JSON_FIELDS = (
    ("source_snapshot_json", "source_snapshot", {}),
    ("config_snapshot_json", "config_snapshot", {}),
    ("asset_mapping_json", "asset_mapping", []),
)
_STAGE_JSON_FIELDS = (
    ("raw_output_json", "raw_output", {}),
    ("normalized_output_json", "normalized_output", {}),
    ("draft_output_json", "draft_output", {}),
    ("output_refs_json", "output_refs", []),
    ("gate_result_json", "gate_result", {}),
)
_RUN_JSON_ALIASES = {
    "source_snapshot": "source_snapshot_json",
    "config_snapshot": "config_snapshot_json",
    "asset_mapping": "asset_mapping_json",
}
_STAGE_JSON_ALIASES = {
    "raw_output": "raw_output_json",
    "normalized_output": "normalized_output_json",
    "draft_output": "draft_output_json",
    "output_refs": "output_refs_json",
    "gate_result": "gate_result_json",
}
_RUN_ALLOWED_ACTIVE_STATUSES = ("failed", "cancelled")


class DynamicWorkflowRepositoryError(RuntimeError):
    """动态工作流仓储写入或读取失败。"""


class DynamicWorkflowNotFoundError(DynamicWorkflowRepositoryError):
    """请求的运行或阶段不存在。"""


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _model_dump(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _model_dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_model_dump(item) for item in value]
    if isinstance(value, tuple):
        return [_model_dump(item) for item in value]
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    value = _model_dump(value)
    if not isinstance(value, Mapping):
        raise TypeError("写入值必须是 Mapping")
    return dict(value)


def _stored_json(value: Any) -> str:
    value = _model_dump(value)
    if isinstance(value, str):
        return value
    return json_dumps(value)


def _prepare_insert_values(
    values: Mapping[str, Any],
    aliases: Mapping[str, str],
    json_fields: Iterable[str],
    allowed_columns: set[str],
) -> dict[str, Any]:
    prepared = dict(values)
    for public_name, database_name in aliases.items():
        if public_name in prepared:
            if database_name not in prepared:
                prepared[database_name] = prepared[public_name]
            prepared.pop(public_name, None)
    for field_name in json_fields:
        if field_name in prepared:
            prepared[field_name] = _stored_json(prepared[field_name])
    return {key: value for key, value in prepared.items() if key in allowed_columns}


class DynamicWorkflowRepository:
    """使用 SQLAlchemy Core 读写动态工作流运行和阶段。"""

    def __init__(self, database: Database | Engine | None = None) -> None:
        if database is None:
            self.engine = db.engine
        elif isinstance(database, Database):
            self.engine = database.engine
        else:
            self.engine = database

    @staticmethod
    def _run_select() -> Any:
        return select(
            workflow_run,
            project.c.name.label("title"),
            chapter.c.title.label("chapter_title"),
        ).select_from(
            workflow_run.join(project, workflow_run.c.project_id == project.c.id).join(
                chapter,
                workflow_run.c.chapter_id == chapter.c.id,
            )
        )

    @staticmethod
    def _decode_run(row: Any) -> dict[str, Any] | None:
        record = row_dict(row)
        if record is None:
            return None
        for database_name, public_name, default in _RUN_JSON_FIELDS:
            value = json_loads(record.pop(database_name, None), default)
            expected_type = list if public_name == "asset_mapping" else dict
            record[public_name] = value if isinstance(value, expected_type) else default
        record["current_stage"] = record.get("current_stage") or "0-source"
        return record

    @staticmethod
    def _decode_stage(row: Any) -> dict[str, Any] | None:
        record = row_dict(row)
        if record is None:
            return None
        for database_name, public_name, default in _STAGE_JSON_FIELDS:
            value = json_loads(record.pop(database_name, None), default)
            expected_type = list if public_name == "output_refs" else dict
            record[public_name] = value if isinstance(value, expected_type) else default
        return record

    @staticmethod
    def _expected_status_values(expected_statuses: str | Sequence[str] | None) -> tuple[str, ...]:
        if expected_statuses is None:
            return ()
        if isinstance(expected_statuses, str):
            return (expected_statuses,)
        return tuple(expected_statuses)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        statement = self._run_select().where(workflow_run.c.id == run_id)
        with self.engine.connect() as connection:
            row = connection.execute(statement).first()
        return self._decode_run(row)

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        statement = (
            self._run_select()
            .where(workflow_run.c.project_id == project_id)
            .order_by(workflow_run.c.created_at.desc(), workflow_run.c.id.desc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [record for row in rows if (record := self._decode_run(row)) is not None]

    def get_stage(self, run_id: str, stage_key: str) -> dict[str, Any] | None:
        statement = (
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
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).first()
        return self._decode_stage(row)

    def list_stages(self, run_id: str) -> list[dict[str, Any]]:
        statement = (
            select(workflow_stage)
            .where(workflow_stage.c.workflow_run_id == run_id)
            .order_by(
                workflow_stage.c.ordinal,
                workflow_stage.c.stage_key,
                workflow_stage.c.attempt.desc(),
                workflow_stage.c.updated_at.desc(),
                workflow_stage.c.id.desc(),
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = self._decode_stage(row)
            if record is not None and record["stage_key"] not in latest:
                latest[record["stage_key"]] = record
        return list(latest.values())

    def find_active_run(self, chapter_id: str) -> dict[str, Any] | None:
        statement = (
            self._run_select()
            .where(
                workflow_run.c.chapter_id == chapter_id,
                workflow_run.c.status.not_in(_RUN_ALLOWED_ACTIVE_STATUSES),
            )
            .order_by(workflow_run.c.created_at.desc(), workflow_run.c.id.desc())
            .limit(1)
        )
        with self.engine.connect() as connection:
            row = connection.execute(statement).first()
        return self._decode_run(row)

    def create_run_with_stages(
        self,
        connection: Connection,
        run_values: Mapping[str, Any],
        stage_values: Iterable[Mapping[str, Any]],
    ) -> str:
        """在调用方事务中创建一个运行及其阶段。"""

        source = _as_mapping(run_values)
        run_id = source.get("id") or new_id("run")
        created_at = source.get("created_at") or utc_now()
        run_data = _prepare_insert_values(
            source,
            _RUN_JSON_ALIASES,
            (field_name for field_name, _, _ in _RUN_JSON_FIELDS),
            {column.name for column in workflow_run.columns},
        )
        run_data.update(
            {
                "id": run_id,
                "kind": run_data.get("kind", "dynamic_comic_0_6"),
                "schema_version": run_data.get("schema_version", "1"),
                "status": run_data.get("status", "pending"),
                "current_stage": run_data.get("current_stage") or "0-source",
                "source_kind": run_data.get("source_kind", "full_text"),
                "source_snapshot_json": run_data.get("source_snapshot_json", json_dumps({})),
                "config_snapshot_json": run_data.get("config_snapshot_json", json_dumps({})),
                "asset_mapping_json": run_data.get("asset_mapping_json", json_dumps([])),
                "input_signature": run_data.get("input_signature", ""),
                "revision": run_data.get("revision", 1),
                "error": run_data.get("error", ""),
                "created_at": created_at,
                "updated_at": run_data.get("updated_at", created_at),
                "started_at": run_data.get("started_at"),
                "finished_at": run_data.get("finished_at"),
            }
        )
        connection.execute(insert(workflow_run).values(**run_data))

        prepared_stages: list[dict[str, Any]] = []
        allowed_stage_columns = {column.name for column in workflow_stage.columns}
        for value in stage_values:
            stage_source = _as_mapping(value)
            stage_data = _prepare_insert_values(
                stage_source,
                _STAGE_JSON_ALIASES,
                (field_name for field_name, _, _ in _STAGE_JSON_FIELDS),
                allowed_stage_columns,
            )
            stage_key = stage_data.get("stage_key")
            if not stage_key:
                raise DynamicWorkflowRepositoryError("阶段键不能为空")
            stage_created_at = stage_data.get("created_at") or created_at
            stage_data.update(
                {
                    "id": stage_data.get("id") or new_id("stage"),
                    "workflow_run_id": run_id,
                    "stage_key": stage_key,
                    "ordinal": stage_data.get("ordinal", get_stage_ordinal(stage_key)),
                    "status": stage_data.get("status", "pending"),
                    "input_signature": stage_data.get("input_signature", ""),
                    "raw_output_json": stage_data.get("raw_output_json", json_dumps({})),
                    "normalized_output_json": stage_data.get(
                        "normalized_output_json", json_dumps({})
                    ),
                    "draft_output_json": stage_data.get("draft_output_json", json_dumps({})),
                    "output_refs_json": stage_data.get("output_refs_json", json_dumps([])),
                    "gate_result_json": stage_data.get("gate_result_json", json_dumps({})),
                    "attempt": stage_data.get("attempt", 1),
                    "error": stage_data.get("error", ""),
                    "created_at": stage_created_at,
                    "updated_at": stage_data.get("updated_at", stage_created_at),
                    "started_at": stage_data.get("started_at"),
                    "finished_at": stage_data.get("finished_at"),
                    "confirmed_at": stage_data.get("confirmed_at"),
                    "confirmed_revision": stage_data.get("confirmed_revision"),
                }
            )
            prepared_stages.append(stage_data)
        if prepared_stages:
            connection.execute(insert(workflow_stage), prepared_stages)
        return run_id

    def _raise_run_write_conflict(
        self,
        connection: Connection,
        run_id: str,
        expected_revision: int,
        expected_statuses: Sequence[str] | None = None,
    ) -> None:
        current = row_dict(
            connection.execute(
                select(workflow_run.c.revision, workflow_run.c.status).where(
                    workflow_run.c.id == run_id
                )
            ).first()
        )
        if current is None:
            raise DynamicWorkflowNotFoundError(f"运行不存在：{run_id}")
        if current["revision"] != expected_revision:
            raise RevisionConflict(current["revision"], expected_revision)
        if expected_statuses is not None:
            statuses = ", ".join(expected_statuses)
            raise DynamicWorkflowRepositoryError(
                f"运行状态不符合预期：actual={current['status']!r}, expected=({statuses})"
            )
        raise DynamicWorkflowRepositoryError(f"运行 revision 更新失败：{run_id}")

    def update_run_status(
        self,
        run_id: str,
        expected_revision: int,
        expected_statuses: str | Sequence[str] | None,
        target_status: str,
        current_stage: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        statuses = self._expected_status_values(expected_statuses)
        now = utc_now()
        values: dict[str, Any] = {
            "status": target_status,
            "revision": workflow_run.c.revision + 1,
            "updated_at": now,
        }
        if current_stage is not None:
            values["current_stage"] = current_stage or "0-source"
        if error is not None:
            values["error"] = error
        if target_status == "running":
            values["started_at"] = now
            values["finished_at"] = None
        elif target_status in {"failed", "interrupted", "cancelled"}:
            values["finished_at"] = now
        with self.engine.begin() as connection:
            statement = update(workflow_run).where(
                workflow_run.c.id == run_id,
                workflow_run.c.revision == expected_revision,
            )
            if statuses:
                statement = statement.where(workflow_run.c.status.in_(statuses))
            result = connection.execute(statement.values(**values))
            if result.rowcount != 1:
                self._raise_run_write_conflict(
                    connection, run_id, expected_revision, statuses or None
                )
        updated = self.get_run(run_id)
        if updated is None:
            raise DynamicWorkflowNotFoundError(f"运行不存在：{run_id}")
        return updated

    def _bump_run_revision_in_connection(
        self,
        connection: Connection,
        run_id: str,
        expected_revision: int | None = None,
    ) -> int:
        statement = update(workflow_run).where(workflow_run.c.id == run_id)
        if expected_revision is not None:
            statement = statement.where(workflow_run.c.revision == expected_revision)
        result = connection.execute(
            statement.values(
                revision=workflow_run.c.revision + 1,
                updated_at=utc_now(),
            )
        )
        if result.rowcount != 1:
            current = row_dict(
                connection.execute(
                    select(workflow_run.c.revision).where(workflow_run.c.id == run_id)
                ).first()
            )
            if current is None:
                raise DynamicWorkflowNotFoundError(f"运行不存在：{run_id}")
            if expected_revision is not None:
                raise RevisionConflict(current["revision"], expected_revision)
            raise DynamicWorkflowRepositoryError(f"运行 revision 更新失败：{run_id}")
        revision = connection.scalar(
            select(workflow_run.c.revision).where(workflow_run.c.id == run_id)
        )
        if revision is None:
            raise DynamicWorkflowNotFoundError(f"运行不存在：{run_id}")
        return int(revision)

    def bump_run_revision(
        self,
        connection: Connection | str | None = None,
        run_id: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """递增运行 revision；可复用调用方连接，也可自行开启短事务。"""

        if isinstance(connection, str) and run_id is None:
            run_id = connection
            connection = None
        if run_id is None:
            raise ValueError("run_id 不能为空")
        if connection is None:
            with self.engine.begin() as managed_connection:
                return self._bump_run_revision_in_connection(
                    managed_connection,
                    run_id,
                    expected_revision,
                )
        assert not isinstance(connection, str)
        return self._bump_run_revision_in_connection(connection, run_id, expected_revision)

    @staticmethod
    def _select_stage_for_write(
        connection: Connection,
        *,
        stage_id: str | None = None,
        run_id: str | None = None,
        stage_key: str | None = None,
    ) -> dict[str, Any] | None:
        if stage_id is None and (run_id is None or stage_key is None):
            raise ValueError("必须提供 stage_id，或同时提供 run_id 与 stage_key")
        statement = select(workflow_stage)
        if stage_id is not None:
            statement = statement.where(workflow_stage.c.id == stage_id)
            if run_id is not None:
                statement = statement.where(workflow_stage.c.workflow_run_id == run_id)
            if stage_key is not None:
                statement = statement.where(workflow_stage.c.stage_key == stage_key)
        else:
            statement = statement.where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
        statement = statement.order_by(
            workflow_stage.c.attempt.desc(),
            workflow_stage.c.updated_at.desc(),
            workflow_stage.c.id.desc(),
        )
        return row_dict(connection.execute(statement.limit(1)).first())

    def update_stage_status(
        self,
        stage_id: str | None = None,
        expected_run_revision: int | None = None,
        target_status: str | None = None,
        error: str | None = None,
        *,
        run_id: str | None = None,
        stage_key: str | None = None,
    ) -> dict[str, Any]:
        if expected_run_revision is None or target_status is None:
            raise ValueError("expected_run_revision 与 target_status 不能为空")
        now = utc_now()
        with self.engine.begin() as connection:
            current = self._select_stage_for_write(
                connection,
                stage_id=stage_id,
                run_id=run_id,
                stage_key=stage_key,
            )
            if current is None:
                identifier = stage_id or f"{run_id}/{stage_key}"
                raise DynamicWorkflowNotFoundError(f"阶段不存在：{identifier}")
            actual_run_id = current["workflow_run_id"]
            new_revision = self._bump_run_revision_in_connection(
                connection,
                actual_run_id,
                expected_run_revision,
            )
            values: dict[str, Any] = {"status": target_status, "updated_at": now}
            if error is not None:
                values["error"] = error
            if target_status == "running":
                values["started_at"] = now
                values["finished_at"] = None
            elif target_status in {"ready", "confirmed", "failed", "interrupted", "cancelled"}:
                values["finished_at"] = now
            if target_status == "confirmed":
                values["confirmed_at"] = now
                values["confirmed_revision"] = new_revision
            result = connection.execute(
                update(workflow_stage).where(workflow_stage.c.id == current["id"]).values(**values)
            )
            if result.rowcount != 1:
                raise DynamicWorkflowRepositoryError(f"阶段状态更新失败：{current['id']}")
            updated_row = connection.execute(
                select(workflow_stage).where(workflow_stage.c.id == current["id"])
            ).first()
        updated = self._decode_stage(updated_row)
        if updated is None:
            raise DynamicWorkflowNotFoundError(f"阶段不存在：{current['id']}")
        return updated

    def save_stage_draft(
        self,
        run_id: str,
        stage_key: str,
        expected_run_revision: int,
        draft_output: Mapping[str, Any],
        gate_result: Any | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.engine.begin() as connection:
            current = self._select_stage_for_write(
                connection,
                run_id=run_id,
                stage_key=stage_key,
            )
            if current is None:
                raise DynamicWorkflowNotFoundError(f"阶段不存在：{run_id}/{stage_key}")
            self._bump_run_revision_in_connection(connection, run_id, expected_run_revision)
            values: dict[str, Any] = {
                "draft_output_json": _stored_json(draft_output),
                "updated_at": now,
            }
            if gate_result is not None:
                values["gate_result_json"] = _stored_json(gate_result)
            result = connection.execute(
                update(workflow_stage).where(workflow_stage.c.id == current["id"]).values(**values)
            )
            if result.rowcount != 1:
                raise DynamicWorkflowRepositoryError(f"阶段草稿保存失败：{current['id']}")
            updated_row = connection.execute(
                select(workflow_stage).where(workflow_stage.c.id == current["id"])
            ).first()
        updated = self._decode_stage(updated_row)
        if updated is None:
            raise DynamicWorkflowNotFoundError(f"阶段不存在：{run_id}/{stage_key}")
        return updated

    def get_confirmed_stage_keys(self, run_id: str) -> list[str]:
        statement = (
            select(workflow_stage.c.stage_key, workflow_stage.c.status, workflow_stage.c.attempt)
            .where(workflow_stage.c.workflow_run_id == run_id)
            .order_by(workflow_stage.c.stage_key, workflow_stage.c.attempt.desc())
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        latest_status: dict[str, str] = {}
        for row in rows:
            stage_key = row.stage_key
            if stage_key not in latest_status:
                latest_status[stage_key] = row.status
        known = [
            stage_key for stage_key in STAGE_KEYS if latest_status.get(stage_key) == "confirmed"
        ]
        unknown = sorted(
            stage_key
            for stage_key, status in latest_status.items()
            if stage_key not in STAGE_KEYS and status == "confirmed"
        )
        return [*known, *unknown]

    def mark_descendants_stale(
        self,
        connection: Connection,
        run_id: str,
        stage_key: str,
    ) -> list[str]:
        descendants = get_descendant_stage_keys(stage_key)
        if not descendants:
            return []
        rows = connection.execute(
            select(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key.in_(descendants),
            )
            .order_by(workflow_stage.c.stage_key, workflow_stage.c.attempt.desc())
        ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = row_dict(row)
            if record is not None and record["stage_key"] not in latest:
                latest[record["stage_key"]] = record
        now = utc_now()
        changed: list[str] = []
        for descendant in descendants:
            current = latest.get(descendant)
            if current is None or current["status"] in {"stale", "cancelled"}:
                continue
            result = connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == current["id"])
                .values(status="stale", updated_at=now)
            )
            if result.rowcount == 1:
                changed.append(descendant)
        return changed

    def get_run_with_stages(self, run_id: str) -> dict[str, Any] | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        return {"run": run, "stages": self.list_stages(run_id)}


__all__ = [
    "DynamicWorkflowNotFoundError",
    "DynamicWorkflowRepository",
    "DynamicWorkflowRepositoryError",
    "RevisionConflict",
]
