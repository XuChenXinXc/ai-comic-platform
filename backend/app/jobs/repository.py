from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.db import Database, db, utc_now
from app.models.tables import job
from app.schemas import BatchSummary, JobCreate
from app.services.utils import sanitize_error_message

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
NON_TERMINAL_STATUSES = {"queued", "running", "waiting_provider", "downloading"}


class JobRepositoryError(RuntimeError):
    pass


class JobNotFoundError(JobRepositoryError):
    pass


class JobStateError(JobRepositoryError):
    pass


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_loads_list(value: Any) -> list[str]:
    if isinstance(value, list):
        parsed = value
    elif not value:
        return []
    else:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    return (
        parsed if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed) else []
    )


def _is_idempotency_integrity_error(error: IntegrityError) -> bool:
    original = getattr(error, "orig", None)
    text = str(original if original is not None else error).lower()
    diagnostic = getattr(original, "diag", None)
    constraint_name = str(getattr(diagnostic, "constraint_name", "") or "").lower()
    if constraint_name == "ux_job_idempotency_key":
        return True
    return any(
        marker in text
        for marker in (
            "ux_job_idempotency_key",
            "unique constraint failed: job.idempotency_key",
            "unique constraint failed: idempotency_key",
        )
    )


def has_download_source(record: Mapping[str, Any]) -> bool:
    candidates = (record.get("result_data") or {}, record.get("input_data") or {})
    for value in candidates:
        if not isinstance(value, Mapping):
            continue
        if any(
            value.get(key) for key in ("result_url", "content_base64", "local_file", "download_url")
        ):
            return True
        data = value.get("data")
        if isinstance(data, Mapping) and data.get("local_file"):
            return True
    return False


class JobRepository:
    def __init__(self, database: Database | Engine | None = None) -> None:
        if database is None:
            self.engine = db.engine
        elif isinstance(database, Database):
            self.engine = database.engine
        else:
            self.engine = database

    @staticmethod
    def _decode(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        record["input_data"] = _json_loads(record.get("input_json"))
        record["depends_on"] = _json_loads_list(record.get("depends_on_json"))
        record["result_data"] = _json_loads(record.get("result_json"))
        return record

    def create_job(
        self,
        data: JobCreate | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        source: dict[str, Any] = {}
        if isinstance(data, JobCreate):
            source.update(data.model_dump())
        elif data is not None:
            source.update(dict(data))
        source.update(kwargs)
        if "input_data" not in source and "input_json" in source:
            source["input_data"] = _json_loads(source.pop("input_json"))
        parsed = JobCreate.model_validate(source)
        idempotency_key = parsed.idempotency_key or None
        now = utc_now()
        values = {
            "id": parsed.id or f"job-{uuid4().hex}",
            "project_id": parsed.project_id,
            "chapter_id": parsed.chapter_id,
            "workflow_run_id": parsed.workflow_run_id,
            "workflow_stage_id": parsed.workflow_stage_id,
            "job_type": parsed.job_type,
            "target_type": parsed.target_type,
            "target_id": parsed.target_id,
            "batch_id": parsed.batch_id,
            "idempotency_key": idempotency_key,
            "provider_kind": parsed.provider_kind,
            "provider_id": parsed.provider_id,
            "provider_task_id": "",
            "status": "queued",
            "progress_current": 0,
            "progress_total": parsed.progress_total,
            "message": "",
            "input_json": _json_dumps(parsed.input_data),
            "input_signature": parsed.input_signature,
            "depends_on_json": _json_dumps(parsed.depends_on),
            "result_json": "{}",
            "error": "",
            "retry_count": 0,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        try:
            with self.engine.begin() as connection:
                if idempotency_key is not None:
                    existing = connection.execute(
                        select(job).where(job.c.idempotency_key == idempotency_key)
                    ).first()
                    if existing is not None:
                        existing_record = self._decode(existing)
                        if existing_record is None:
                            raise JobRepositoryError("幂等任务读取失败")
                        return existing_record
                connection.execute(insert(job).values(**values))
                created = self._decode(
                    connection.execute(select(job).where(job.c.id == values["id"])).first()
                )
        except IntegrityError as error:
            if idempotency_key is None or not _is_idempotency_integrity_error(error):
                raise
            created = self.find_by_idempotency_key(idempotency_key)
            if created is None:
                raise
        if created is None:
            raise JobRepositoryError("任务创建后无法读取")
        return created

    def get(
        self,
        job_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        if connection is not None:
            row = connection.execute(select(job).where(job.c.id == job_id)).first()
            return self._decode(row)
        with self.engine.connect() as conn:
            row = conn.execute(select(job).where(job.c.id == job_id)).first()
        return self._decode(row)

    def require(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        if record is None:
            raise JobNotFoundError(f"任务不存在：{job_id}")
        return record

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | Sequence[str] | None = None,
        job_type: str | None = None,
        batch_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_stage_id: str | None = None,
        idempotency_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        statement = select(job)
        if project_id is not None:
            statement = statement.where(job.c.project_id == project_id)
        if isinstance(status, str):
            statement = statement.where(job.c.status == status)
        elif status:
            statement = statement.where(job.c.status.in_(status))
        if job_type is not None:
            statement = statement.where(job.c.job_type == job_type)
        if batch_id is not None:
            statement = statement.where(job.c.batch_id == batch_id)
        if workflow_run_id is not None:
            statement = statement.where(job.c.workflow_run_id == workflow_run_id)
        if workflow_stage_id is not None:
            statement = statement.where(job.c.workflow_stage_id == workflow_stage_id)
        if idempotency_key is not None:
            statement = statement.where(job.c.idempotency_key == idempotency_key)
        statement = statement.order_by(job.c.created_at.desc()).limit(limit).offset(offset)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [record for row in rows if (record := self._decode(row)) is not None]

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        with self.engine.connect() as connection:
            row = connection.execute(select(job).where(job.c.idempotency_key == key)).first()
        return self._decode(row)

    def list_by_workflow(
        self,
        workflow_run_id: str,
        stage_id: str | None = None,
        status: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.list(
            workflow_run_id=workflow_run_id,
            workflow_stage_id=stage_id,
            status=status,
            limit=100_000,
        )

    def list_batch(self, batch_id: str) -> list[dict[str, Any]]:
        statement = (
            select(job).where(job.c.batch_id == batch_id).order_by(job.c.created_at, job.c.id)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [record for row in rows if (record := self._decode(row)) is not None]

    def batch_summary(self, batch_id: str) -> BatchSummary:
        records = self.list_batch(batch_id)
        counts = {status: 0 for status in {*NON_TERMINAL_STATUSES, *TERMINAL_STATUSES}}
        for record in records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        return BatchSummary(batch_id=batch_id, total=len(records), **counts)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        self.require(job_id)
        values = dict(changes)
        if "input_data" in values:
            values["input_json"] = _json_dumps(values.pop("input_data"))
        if "result_data" in values:
            values["result_json"] = _json_dumps(values.pop("result_data"))
        if "error" in values:
            values["error"] = sanitize_error_message(values["error"])
        values["updated_at"] = utc_now()
        with self.engine.begin() as connection:
            connection.execute(update(job).where(job.c.id == job_id).values(**values))
        return self.require(job_id)

    def set_status(
        self,
        job_id: str,
        status: str,
        *,
        message: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {"status": status}
        if message is not None:
            values["message"] = message
        if error is not None:
            values["error"] = error
        if status == "running":
            values["started_at"] = utc_now()
            values["finished_at"] = None
        elif status in TERMINAL_STATUSES:
            values["finished_at"] = utc_now()
        return self.update(job_id, **values)

    def claim(
        self,
        job_id: str,
        *,
        expected_status: str = "queued",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status == expected_status)
                .values(
                    status="running",
                    error="",
                    message="任务执行中",
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
        if result.rowcount != 1:
            return None
        return self.get(job_id)

    def requeue_for_recovery(
        self,
        job_id: str,
        *,
        expected_status: str,
        message: str,
    ) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status == expected_status)
                .values(
                    status="queued",
                    message=message,
                    error="",
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
        return result.rowcount == 1

    def set_provider_task(
        self,
        job_id: str,
        provider_task_id: str,
        *,
        status: str = "waiting_provider",
        result_data: Mapping[str, Any] | None = None,
        message: str = "",
    ) -> dict[str, Any] | None:
        """写入 provider_task_id。任务已进入终态（例如刚被取消）时返回 ``None``。"""
        if not provider_task_id:
            raise JobStateError("provider_task_id 不能为空")
        values: dict[str, Any] = {
            "provider_task_id": provider_task_id,
            "status": status,
            "message": message,
        }
        if result_data is not None:
            values["result_json"] = _json_dumps(dict(result_data))
        values["updated_at"] = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(job)
                .where(
                    job.c.id == job_id,
                    job.c.status.in_(NON_TERMINAL_STATUSES),
                )
                .values(**values)
            )
        # 任务可能刚被取消：不覆盖终态，处理器会在下一次轮询检查到取消并中止。
        if result.rowcount != 1:
            return None
        return self.require(job_id)

    def update_progress(
        self,
        job_id: str,
        current: int,
        *,
        total: int | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {"progress_current": current}
        if total is not None:
            changes["progress_total"] = total
        if message is not None:
            changes["message"] = message
        return self.update(job_id, **changes)

    def save_result(
        self,
        job_id: str,
        result_data: Mapping[str, Any],
        *,
        merge: bool = False,
    ) -> dict[str, Any]:
        value = dict(result_data)
        if merge:
            current = self.require(job_id)["result_data"]
            value = {**current, **value}
        return self.update(job_id, result_data=value)

    def complete(
        self,
        job_id: str,
        result_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.require(job_id)
        values: dict[str, Any] = {
            "status": "completed",
            "progress_current": current["progress_total"],
            "message": "任务完成",
            "error": "",
            "finished_at": utc_now(),
        }
        if result_data is not None:
            values["result_data"] = dict(result_data)
        return self.update(job_id, **values)

    mark_completed = complete

    def fail(self, job_id: str, error: str) -> dict[str, Any]:
        return self.update(
            job_id,
            status="failed",
            error=error,
            message="任务失败",
            finished_at=utc_now(),
        )

    mark_failed = fail

    def interrupt(self, job_id: str, message: str) -> dict[str, Any]:
        return self.update(
            job_id,
            status="interrupted",
            error=message,
            message="任务因服务重启中断",
            finished_at=utc_now(),
        )

    mark_interrupted = interrupt

    def batch_fail(
        self,
        job_ids: Sequence[str],
        message: str,
        *,
        connection: Any | None = None,
    ) -> int:
        """批量把一组 job 标记为 interrupted（被新 generation 取代的语义）。

        仅命中非终态的 job；终态行不会被打扰。返回实际更新的行数。
        """
        ids = [str(value) for value in job_ids if str(value).strip()]
        if not ids:
            return 0
        now = utc_now()
        values = {
            "status": "interrupted",
            "error": message,
            "message": "任务因服务重启中断",
            "started_at": None,
            "finished_at": now,
            "updated_at": now,
        }
        if connection is None:
            with self.engine.begin() as owned:
                result = owned.execute(
                    update(job)
                    .where(
                        job.c.id.in_(ids),
                        job.c.status.in_(NON_TERMINAL_STATUSES),
                    )
                    .values(**values)
                )
            return int(result.rowcount or 0)
        result = connection.execute(
            update(job)
            .where(
                job.c.id.in_(ids),
                job.c.status.in_(NON_TERMINAL_STATUSES),
            )
            .values(**values)
        )
        return int(result.rowcount or 0)

    def retry(self, job_id: str) -> dict[str, Any]:
        current = self.require(job_id)
        if current["status"] not in {"failed", "interrupted"}:
            raise JobStateError("只有失败或中断的任务可以重试")
        if current["provider_task_id"]:
            next_status = (
                "downloading"
                if current["provider_kind"] == "video" and has_download_source(current)
                else "waiting_provider"
            )
        else:
            next_status = "queued"
        return self.update(
            job_id,
            status=next_status,
            error="",
            message="任务已重新入队",
            retry_count=current["retry_count"] + 1,
            started_at=None,
            finished_at=None,
        )

    def cancel(
        self,
        job_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        if connection is None:
            with self.engine.begin() as owned_connection:
                return self.cancel(job_id, connection=owned_connection)

        now = utc_now()
        result = connection.execute(
            update(job)
            .where(job.c.id == job_id, job.c.status.in_(NON_TERMINAL_STATUSES))
            .values(
                status="cancelled",
                message="任务已取消",
                finished_at=now,
                updated_at=now,
            )
        )
        current = self._decode(connection.execute(select(job).where(job.c.id == job_id)).first())
        if result.rowcount != 1:
            if current is None:
                raise JobNotFoundError(f"任务不存在：{job_id}")
            raise JobStateError("只有未完成的任务可以取消")
        if current is None:
            raise JobNotFoundError(f"任务不存在：{job_id}")
        return current

    def list_non_terminal(
        self,
        *,
        include_dynamic: bool = False,
        workflow_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(job).where(job.c.status.in_(sorted(NON_TERMINAL_STATUSES)))
        if workflow_run_id is not None:
            statement = statement.where(job.c.workflow_run_id == workflow_run_id)
        elif not include_dynamic:
            statement = statement.where(job.c.workflow_run_id.is_(None))
        statement = statement.order_by(job.c.created_at.desc()).limit(100_000)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return [record for row in rows if (record := self._decode(row)) is not None]


job_repository = JobRepository()
