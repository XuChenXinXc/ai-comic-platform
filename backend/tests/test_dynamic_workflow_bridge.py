from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select, update

from app.db import Database, migrate_schema, utc_now
from app.jobs.dynamic_bridge import (
    DynamicJobBridge,
    DynamicJobBridgeError,
    build_dynamic_idempotency_key,
)
from app.jobs.dynamic_recovery import _reconcile_queued_stage_jobs
from app.jobs.repository import JobRepository
from app.models.tables import chapter, job, project, workflow_run, workflow_stage
from app.services.signature_service import calculate_signature
from app.services.utils import json_loads


class _QueueError(ValueError):
    successful_ids: list[str]


@pytest.fixture
def bridge_context(tmp_path: Path) -> Iterator[tuple[Database, JobRepository, dict, dict]]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-bridge.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    run = {
        "id": "run-bridge",
        "project_id": "project-bridge",
        "chapter_id": "chapter-bridge",
    }
    stage = {
        "id": "stage-bridge",
        "stage_key": "0-source",
        "input_signature": "signature-bridge",
        "status": "queued",
    }
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=run["project_id"],
                name="桥接测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id=run["chapter_id"],
                project_id=run["project_id"],
                chapter_no=1,
                title="桥接测试章节",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                **run,
                source_kind="full_text",
                status="queued",
                current_stage=stage["stage_key"],
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                **stage,
                workflow_run_id=run["id"],
                ordinal=0,
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, JobRepository(database), run, stage
    finally:
        database.dispose()


def _bridge(repository: JobRepository) -> DynamicJobBridge:
    return DynamicJobBridge(repository)


def _count_jobs(database: Database) -> int:
    with database.engine.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(job)) or 0)


def test_stable_dynamic_idempotency_key_is_deterministic() -> None:
    key = build_dynamic_idempotency_key(
        "stage-1",
        "workflow_stage",
        "stage-1",
        "input-1",
        "dynamic_stage_generate",
    )

    assert key == (
        "dynamic-job-v1:"
        + calculate_signature(
            "stage-1",
            "workflow_stage",
            "stage-1",
            "input-1",
            "dynamic_stage_generate",
        )
    )
    assert key == build_dynamic_idempotency_key(
        "stage-1",
        "workflow_stage",
        "stage-1",
        "input-1",
        "dynamic_stage_generate",
    )
    assert key != build_dynamic_idempotency_key(
        "stage-2",
        "workflow_stage",
        "stage-2",
        "input-1",
        "dynamic_stage_generate",
    )
    assert key.startswith("dynamic-job-v1:")


def test_stage_job_transaction_visibility_and_rollback(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)

    with database.engine.connect() as connection:
        transaction = connection.begin()
        reference = bridge.ensure_stage_job_in_transaction(connection, run, stage)
        assert reference.created is True
        with database.engine.connect() as observer:
            assert observer.scalar(select(func.count()).select_from(job)) == 0
        transaction.rollback()

    assert _count_jobs(database) == 0

    with database.engine.begin() as connection:
        bridge.ensure_stage_job_in_transaction(connection, run, stage)
    assert _count_jobs(database) == 1


def test_first_stage_job_creation_writes_contract_fields_and_reuses_idempotently(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)

    with database.engine.begin() as connection:
        first = bridge.ensure_stage_job_in_transaction(connection, run, stage)
    with database.engine.begin() as connection:
        second = bridge.ensure_stage_job_in_transaction(connection, run, stage)

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    assert second.status == "queued"
    with database.engine.connect() as connection:
        row = connection.execute(select(job).where(job.c.id == first.job_id)).first()
        assert row is not None
        record = dict(row._mapping)
    assert record["job_type"] == "dynamic_stage_generate"
    assert record["target_type"] == "workflow_stage"
    assert record["target_id"] == stage["id"]
    assert record["workflow_run_id"] == run["id"]
    assert record["workflow_stage_id"] == stage["id"]
    assert record["provider_kind"] == "text"
    assert record["input_signature"] == stage["input_signature"]
    assert json_loads(record["input_json"], {}) == {
        "workflow_run_id": run["id"],
        "workflow_stage_id": stage["id"],
        "stage_key": stage["stage_key"],
    }
    assert json_loads(record["depends_on_json"], None) == []
    assert _count_jobs(database) == 1


def test_failed_stage_job_retries_in_current_transaction_when_requested(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)
    with database.engine.begin() as connection:
        created = bridge.ensure_stage_job_in_transaction(connection, run, stage)
    with database.engine.begin() as connection:
        connection.execute(
            update(job)
            .where(job.c.id == created.job_id)
            .values(
                status="failed",
                error="失败原因",
                message="失败消息",
                retry_count=2,
                started_at="started",
                finished_at="finished",
            )
        )

    with database.engine.begin() as connection:
        retried = bridge.ensure_stage_job_in_transaction(
            connection,
            run,
            stage,
            retry_failed=True,
        )
        row = connection.execute(select(job).where(job.c.id == created.job_id)).first()
        assert row is not None
        visible = dict(row._mapping)

    assert retried.job_id == created.job_id
    assert retried.status == "queued"
    assert retried.retried is True
    assert visible["status"] == "queued"
    assert visible["error"] == ""
    assert visible["message"] == ""
    assert visible["started_at"] is None
    assert visible["finished_at"] is None
    assert visible["retry_count"] == 3


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_terminal_stage_job_protection_rejects_queued_stage(
    bridge_context: tuple[Database, JobRepository, dict, dict],
    terminal_status: str,
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)
    with database.engine.begin() as connection:
        created = bridge.ensure_stage_job_in_transaction(connection, run, stage)
        connection.execute(
            update(job).where(job.c.id == created.job_id).values(status=terminal_status)
        )

    with pytest.raises(DynamicJobBridgeError):
        with database.engine.begin() as connection:
            bridge.ensure_stage_job_in_transaction(connection, run, stage)


def test_failed_stage_job_without_retry_returns_terminal_reference_without_requeue(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)
    with database.engine.begin() as connection:
        created = bridge.ensure_stage_job_in_transaction(connection, run, stage)
        connection.execute(
            update(job).where(job.c.id == created.job_id).values(status="interrupted")
        )

    with database.engine.begin() as connection:
        reference = bridge.ensure_stage_job_in_transaction(connection, run, stage)

    assert reference.job_id == created.job_id
    assert reference.status == "interrupted"
    assert reference.retried is False


def test_two_stage_job_calls_leave_one_row(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, run, stage = bridge_context
    bridge = _bridge(repository)

    for _ in range(2):
        with database.engine.begin() as connection:
            bridge.ensure_stage_job_in_transaction(connection, run, stage)

    assert _count_jobs(database) == 1


def test_enqueue_after_commit_filters_empty_ids_and_returns_true_results() -> None:
    called: list[str] = []

    def enqueue(job_id: str) -> bool:
        called.append(job_id)
        return job_id == "job-ok"

    bridge = DynamicJobBridge(repository=None, enqueue=enqueue)  # type: ignore[arg-type]

    assert bridge.enqueue_after_commit(["", "job-ok", "job-no", ""]) == ["job-ok"]
    assert called == ["job-ok", "job-no"]
    assert DynamicJobBridge(repository=None).enqueue_after_commit(["job-ok"]) == []  # type: ignore[arg-type]


def test_enqueue_after_commit_reraises_callback_error_and_preserves_successes() -> None:
    called: list[str] = []

    def enqueue(job_id: str) -> bool:
        called.append(job_id)
        if job_id == "job-error":
            raise _QueueError("queue unavailable")
        return True

    bridge = DynamicJobBridge(repository=None, enqueue=enqueue)  # type: ignore[arg-type]

    with pytest.raises(_QueueError) as error:
        bridge.enqueue_after_commit(["job-ok", "job-error", "job-later"])

    assert called == ["job-ok", "job-error"]
    assert error.value.successful_ids == ["job-ok"]


def test_reconcile_queued_stage_builds_missing_job_without_enqueue(
    bridge_context: tuple[Database, JobRepository, dict, dict],
) -> None:
    database, repository, _run, _stage = bridge_context
    # C3 收敛：DynamicJobBridge.reconcile_queued_stage_jobs 已删除，
    # 对账统一走 dynamic_recovery._reconcile_queued_stage_jobs（生产恢复路径）。
    first = _reconcile_queued_stage_jobs(repository)
    second = _reconcile_queued_stage_jobs(repository)

    assert len(first) == 1
    assert second == first
    assert _count_jobs(database) == 1


@pytest.mark.parametrize(
    ("run_status", "stage_status"),
    [("blocked", "queued"), ("cancelled", "queued"), ("confirmed", "queued"), ("queued", "stale")],
)
def test_reconcile_skips_blocked_cancelled_confirmed_or_stale_stage(
    bridge_context: tuple[Database, JobRepository, dict, dict],
    run_status: str,
    stage_status: str,
) -> None:
    database, repository, run, stage = bridge_context
    with database.engine.begin() as connection:
        connection.execute(
            update(workflow_run).where(workflow_run.c.id == run["id"]).values(status=run_status)
        )
        connection.execute(
            update(workflow_stage)
            .where(workflow_stage.c.id == stage["id"])
            .values(status=stage_status)
        )

    assert _reconcile_queued_stage_jobs(repository) == []
    assert _count_jobs(database) == 0
