from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.jobs.repository import JobRepository
from app.models.tables import job
from app.services.signature_service import calculate_signature
from app.services.utils import json_dumps, new_id, utc_now

_DYNAMIC_JOB_TYPE = "dynamic_stage_generate"
_DYNAMIC_TARGET_TYPE = "workflow_stage"
_DYNAMIC_PROVIDER_KIND = "text"
_REUSABLE_STATUSES = frozenset({"queued", "running", "waiting_provider", "downloading"})
_RETRYABLE_STATUSES = frozenset({"failed", "interrupted"})


class DynamicJobBridgeError(RuntimeError):
    """动态 Job 桥接层的仓储或状态错误。"""


@dataclass(frozen=True)
class DynamicJobReference:
    job_id: str
    status: str
    created: bool = False
    retried: bool = False


def build_dynamic_idempotency_key(
    workflow_stage_id: str,
    target_type: str,
    target_id: str,
    input_signature: str,
    job_type: str,
) -> str:
    """构造不依赖时间、revision 或随机数的动态 Job 幂等键。"""

    return "dynamic-job-v1:" + calculate_signature(
        workflow_stage_id,
        target_type,
        target_id,
        input_signature,
        job_type,
    )


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def _required_value(record: Mapping[str, Any], field: str, *, allow_empty: bool = False) -> Any:
    try:
        value = record[field]
    except (KeyError, TypeError) as exc:
        raise DynamicJobBridgeError(f"动态 Job 缺少字段：{field}") from exc
    if value is None or (isinstance(value, str) and not allow_empty and not value.strip()):
        raise DynamicJobBridgeError(f"动态 Job 字段不能为空：{field}")
    return value


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


class DynamicJobBridge:
    """在工作流事务边界内创建和补建顶层动态阶段 Job。"""

    def __init__(
        self,
        repository: JobRepository,
        enqueue: Callable[[str], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.enqueue = enqueue

    @staticmethod
    def _find_by_idempotency_key(connection: Connection, key: str) -> dict[str, Any] | None:
        return _row_dict(
            connection.execute(select(job).where(job.c.idempotency_key == key)).first()
        )

    @staticmethod
    def _find_by_stage_signature(
        connection: Connection,
        workflow_stage_id: str,
        input_signature: str,
    ) -> dict[str, Any] | None:
        return _row_dict(
            connection.execute(
                select(job)
                .where(
                    job.c.workflow_stage_id == workflow_stage_id,
                    job.c.input_signature == input_signature,
                )
                .order_by(job.c.created_at, job.c.id)
                .limit(1)
            ).first()
        )

    @staticmethod
    def _begin_savepoint(connection: Connection) -> Any:
        """确保 SQLite 延迟事务在建立 savepoint 前已真正开始。"""

        if connection.dialect.name == "sqlite" and connection.get_transaction() is not None:
            raw_connection = getattr(connection.connection, "dbapi_connection", None)
            if raw_connection is not None and not raw_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
        return connection.begin_nested()

    @staticmethod
    def _existing_reference(
        connection: Connection,
        record: Mapping[str, Any],
        stage_status: str,
        *,
        retry_failed: bool,
    ) -> DynamicJobReference:
        job_id = record.get("id")
        status = record.get("status")
        if not job_id or not status:
            raise DynamicJobBridgeError("动态 Job 记录缺少 id 或 status")

        job_id = str(job_id)
        status = str(status)
        if status in _REUSABLE_STATUSES:
            return DynamicJobReference(job_id=job_id, status=status)

        if status == "completed":
            if stage_status == "queued":
                raise DynamicJobBridgeError(
                    f"阶段仍为 queued，但动态 Job 已完成：stage={record.get('workflow_stage_id')}"
                )
            return DynamicJobReference(job_id=job_id, status=status)

        if status == "cancelled":
            raise DynamicJobBridgeError(f"已取消的动态 Job 不能复用或自动恢复：{job_id}")

        if status in _RETRYABLE_STATUSES:
            if not retry_failed:
                return DynamicJobReference(job_id=job_id, status=status)

            now = utc_now()
            result = connection.execute(
                update(job)
                .where(job.c.id == job_id, job.c.status == status)
                .values(
                    status="queued",
                    error="",
                    message="",
                    started_at=None,
                    finished_at=None,
                    retry_count=job.c.retry_count + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                current = DynamicJobBridge._find_by_idempotency_key(
                    connection,
                    str(record.get("idempotency_key") or ""),
                )
                if current is None:
                    raise DynamicJobBridgeError(f"动态 Job 重试时记录不存在：{job_id}")
                raise DynamicJobBridgeError(f"动态 Job 状态已并发变化，无法重试：{job_id}")
            return DynamicJobReference(
                job_id=job_id,
                status="queued",
                retried=True,
            )

        raise DynamicJobBridgeError(f"动态 Job 状态无法复用：{status}")

    def ensure_stage_job_in_transaction(
        self,
        connection: Connection,
        run: Mapping[str, Any],
        stage: Mapping[str, Any],
        *,
        retry_failed: bool = False,
    ) -> DynamicJobReference:
        """在调用方事务中确保一个阶段顶层 Job 存在。"""

        try:
            run_id = _required_value(run, "id")
            project_id = _required_value(run, "project_id")
            chapter_id = _required_value(run, "chapter_id")
            stage_id = _required_value(stage, "id")
            stage_key = _required_value(stage, "stage_key")
            input_signature = _required_value(stage, "input_signature", allow_empty=True)
            stage_status = str(stage.get("status") or "")
            idempotency_key = build_dynamic_idempotency_key(
                str(stage_id),
                _DYNAMIC_TARGET_TYPE,
                str(stage_id),
                str(input_signature),
                _DYNAMIC_JOB_TYPE,
            )

            existing = self._find_by_idempotency_key(connection, idempotency_key)
            if existing is not None:
                return self._existing_reference(
                    connection,
                    existing,
                    stage_status,
                    retry_failed=retry_failed,
                )

            now = utc_now()
            job_id = new_id("job")
            input_data = {
                "workflow_run_id": str(run_id),
                "workflow_stage_id": str(stage_id),
                "stage_key": str(stage_key),
            }
            values = {
                "id": job_id,
                "project_id": project_id,
                "chapter_id": chapter_id,
                "workflow_run_id": run_id,
                "workflow_stage_id": stage_id,
                "job_type": _DYNAMIC_JOB_TYPE,
                "target_type": _DYNAMIC_TARGET_TYPE,
                "target_id": stage_id,
                "batch_id": None,
                "idempotency_key": idempotency_key,
                "provider_kind": _DYNAMIC_PROVIDER_KIND,
                "provider_id": None,
                "provider_task_id": "",
                "status": "queued",
                "progress_current": 0,
                "progress_total": 1,
                "message": "",
                "input_json": json_dumps(input_data),
                "input_signature": input_signature,
                "depends_on_json": json_dumps([]),
                "result_json": json_dumps({}),
                "error": "",
                "retry_count": 0,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
            }
            try:
                with self._begin_savepoint(connection):
                    connection.execute(insert(job).values(**values))
            except IntegrityError as error:
                if not _is_idempotency_integrity_error(error):
                    raise DynamicJobBridgeError(f"动态 Job 创建失败：{error}") from error
                existing = self._find_by_idempotency_key(connection, idempotency_key)
                if existing is None:
                    raise DynamicJobBridgeError(
                        f"动态 Job 幂等竞态后无法读取已有记录：{idempotency_key}"
                    ) from error
                return self._existing_reference(
                    connection,
                    existing,
                    stage_status,
                    retry_failed=retry_failed,
                )
            except SQLAlchemyError as error:
                raise DynamicJobBridgeError(f"动态 Job 创建失败：{error}") from error
            return DynamicJobReference(job_id=job_id, status="queued", created=True)
        except DynamicJobBridgeError:
            raise
        except SQLAlchemyError as error:
            raise DynamicJobBridgeError(f"动态 Job 桥接数据库操作失败：{error}") from error
        except Exception as error:
            raise DynamicJobBridgeError(f"动态 Job 桥接失败：{error}") from error

    def enqueue_after_commit(self, job_ids: Iterable[str]) -> list[str]:
        """提交事务后向外部队列提交 Job；本方法本身不管理事务。"""

        if self.enqueue is None:
            return []

        successful_ids: list[str] = []
        try:
            for job_id in job_ids:
                if not job_id:
                    continue
                if self.enqueue(job_id):
                    successful_ids.append(job_id)
        except Exception as error:
            # Python 不能同时返回并抛出异常，保留已成功 ID 供调用方诊断。
            try:
                error.successful_ids = successful_ids  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
            raise
        return successful_ids


__all__ = [
    "DynamicJobBridge",
    "DynamicJobBridgeError",
    "DynamicJobReference",
    "build_dynamic_idempotency_key",
]
