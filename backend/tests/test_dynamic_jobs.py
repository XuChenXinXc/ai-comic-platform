from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, insert, select

from app.db import Database, migrate_schema, utc_now
from app.jobs.repository import JobRepository
from app.models.tables import chapter, job, project, workflow_run, workflow_stage
from app.schemas import JobCreate, JobRead
from app.services.job_service import JobService


@pytest.fixture
def job_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKIP_DATA_DELETION", "1")
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-jobs.db').as_posix()}")
    migrate_schema(database)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-dynamic-jobs",
                name="动态任务测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-dynamic-jobs",
                project_id="project-dynamic-jobs",
                chapter_no=1,
                title="动态任务测试章节",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id="workflow-run-1",
                project_id="project-dynamic-jobs",
                chapter_id="chapter-dynamic-jobs",
                source_kind="full_text",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id="workflow-stage-1",
                workflow_run_id="workflow-run-1",
                stage_key="0-source",
                ordinal=0,
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, JobRepository(database)
    finally:
        database.dispose()


def _job_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "project_id": "project-dynamic-jobs",
        "chapter_id": "chapter-dynamic-jobs",
        "job_type": "dynamic_stage_generate",
        "provider_kind": "text",
    }
    data.update(overrides)
    return data


def test_dynamic_job_fields_are_created_and_decoded(job_context) -> None:
    _database, repository = job_context

    record = repository.create_job(
        JobCreate(
            **_job_data(
                workflow_run_id="workflow-run-1",
                workflow_stage_id="workflow-stage-1",
                idempotency_key="dynamic-key",
                input_signature="signature-1",
                depends_on=["job-a", "job-b"],
            )
        )
    )

    assert record["workflow_run_id"] == "workflow-run-1"
    assert record["workflow_stage_id"] == "workflow-stage-1"
    assert record["idempotency_key"] == "dynamic-key"
    assert record["input_signature"] == "signature-1"
    assert record["depends_on"] == ["job-a", "job-b"]
    assert repository.get(record["id"])["depends_on"] == ["job-a", "job-b"]
    assert JobRead.model_validate(record).depends_on == ["job-a", "job-b"]


def test_idempotent_job_creation_returns_one_existing_record(job_context) -> None:
    database, repository = job_context

    first = repository.create_job(JobCreate(**_job_data(idempotency_key="same-key")))
    second = repository.create_job(
        JobCreate(**_job_data(idempotency_key="same-key", input_data={"different": True}))
    )

    assert second["id"] == first["id"]
    assert second["input_data"] == first["input_data"]
    with database.engine.connect() as connection:
        count = connection.scalar(
            select(func.count()).select_from(job).where(job.c.idempotency_key == "same-key")
        )
    assert count == 1


def test_different_idempotency_keys_create_distinct_jobs(job_context) -> None:
    _database, repository = job_context

    first = repository.create_job(JobCreate(**_job_data(idempotency_key="key-a")))
    second = repository.create_job(JobCreate(**_job_data(idempotency_key="key-b")))

    assert first["id"] != second["id"]
    assert repository.find_by_idempotency_key("key-a")["id"] == first["id"]
    assert repository.find_by_idempotency_key("key-b")["id"] == second["id"]


def test_job_filters_find_and_list_by_workflow(job_context) -> None:
    _database, repository = job_context

    first = repository.create_job(
        JobCreate(
            **_job_data(
                workflow_run_id="workflow-run-1",
                workflow_stage_id="workflow-stage-1",
                idempotency_key="filter-key-1",
            )
        )
    )
    second = repository.create_job(
        JobCreate(
            **_job_data(
                workflow_run_id="workflow-run-1",
                workflow_stage_id="workflow-stage-1",
                idempotency_key="filter-key-2",
            )
        )
    )
    repository.update(second["id"], status="running")

    assert {item["id"] for item in repository.list(workflow_run_id="workflow-run-1")} == {
        first["id"],
        second["id"],
    }
    assert {item["id"] for item in repository.list(workflow_stage_id="workflow-stage-1")} == {
        first["id"],
        second["id"],
    }
    assert [item["id"] for item in repository.list(idempotency_key="filter-key-1")] == [first["id"]]
    assert [
        item["id"]
        for item in repository.list_by_workflow(
            "workflow-run-1", stage_id="workflow-stage-1", status="running"
        )
    ] == [second["id"]]


def test_list_non_terminal_defaults_to_legacy_jobs(job_context) -> None:
    _database, repository = job_context

    legacy = repository.create_job(JobCreate(**_job_data(job_type="legacy_job")))
    dynamic = repository.create_job(
        JobCreate(
            **_job_data(
                workflow_run_id="workflow-run-1",
                workflow_stage_id="workflow-stage-1",
                idempotency_key="non-terminal-key",
            )
        )
    )

    assert [item["id"] for item in repository.list_non_terminal()] == [legacy["id"]]
    assert {item["id"] for item in repository.list_non_terminal(include_dynamic=True)} == {
        legacy["id"],
        dynamic["id"],
    }
    assert [
        item["id"] for item in repository.list_non_terminal(workflow_run_id="workflow-run-1")
    ] == [dynamic["id"]]


def test_legacy_job_can_be_created_read_and_validated_without_dynamic_fields(job_context) -> None:
    _database, repository = job_context

    record = repository.create_job(
        {
            "project_id": "project-dynamic-jobs",
            "chapter_id": "chapter-dynamic-jobs",
            "job_type": "legacy_job",
        }
    )

    assert record["workflow_run_id"] is None
    assert record["workflow_stage_id"] is None
    assert record["idempotency_key"] is None
    assert record["input_signature"] is None
    assert record["depends_on"] == []
    legacy_payload = {
        key: value
        for key, value in record.items()
        if key
        not in {
            "workflow_run_id",
            "workflow_stage_id",
            "idempotency_key",
            "input_signature",
            "depends_on",
        }
    }
    validated = JobRead.model_validate(legacy_payload)
    assert validated.workflow_run_id is None
    assert validated.depends_on == []


class _FakeRunner:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return True


def test_job_service_forwards_dynamic_fields_and_skips_terminal_idempotent_enqueue(
    job_context,
) -> None:
    _database, repository = job_context
    runner = _FakeRunner()
    service = JobService(repository=repository, runner=cast(Any, runner))

    created = service.create(
        **_job_data(
            workflow_run_id="workflow-run-1",
            workflow_stage_id="workflow-stage-1",
            idempotency_key="service-key",
            input_signature="service-signature",
            depends_on=["upstream-job"],
        )
    )
    assert created["workflow_run_id"] == "workflow-run-1"
    assert created["workflow_stage_id"] == "workflow-stage-1"
    assert created["idempotency_key"] == "service-key"
    assert created["input_signature"] == "service-signature"
    assert created["depends_on"] == ["upstream-job"]
    assert runner.enqueued == [created["id"]]

    repository.complete(created["id"])
    repeated = service.create(
        **_job_data(
            workflow_run_id="workflow-run-1",
            workflow_stage_id="workflow-stage-1",
            idempotency_key="service-key",
            input_signature="changed-signature",
        )
    )

    assert repeated["id"] == created["id"]
    assert runner.enqueued == [created["id"]]
