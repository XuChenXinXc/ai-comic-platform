from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert, select

from app.db import Database, utc_now
from app.jobs.dynamic_handlers import make_dynamic_stage_generate_handler
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import chapter, project, workflow_run, workflow_stage
from app.services.task_handlers import register_handlers


@pytest.fixture
def dynamic_handler_context(tmp_path: Path) -> Iterator[tuple[Database, JobRepository]]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-handlers.db').as_posix()}")
    database.create_all()
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id="project-handler",
                name="动态 Handler 测试项目",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id="chapter-handler",
                project_id="project-handler",
                chapter_no=1,
                title="动态 Handler 测试章节",
                created_at=now,
                updated_at=now,
            )
        )
    try:
        yield database, JobRepository(database)
    finally:
        database.dispose()


def _insert_workflow(
    database: Database,
    *,
    run_status: str = "queued",
    stage_status: str = "queued",
    revision: int = 1,
) -> tuple[str, str]:
    now = utc_now()
    run_id = f"run-{run_status}-{stage_status}-{revision}"
    stage_id = f"stage-{run_status}-{stage_status}-{revision}"
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id="project-handler",
                chapter_id="chapter-handler",
                source_kind="full_text",
                status=run_status,
                current_stage="0-source",
                revision=revision,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id=stage_id,
                workflow_run_id=run_id,
                stage_key="0-source",
                ordinal=0,
                status=stage_status,
                created_at=now,
                updated_at=now,
            )
        )
    return run_id, stage_id


def _create_job(
    repository: JobRepository,
    *,
    workflow_run_id: str | None,
    workflow_stage_id: str | None,
) -> dict[str, Any]:
    return repository.create_job(
        {
            "project_id": "project-handler",
            "chapter_id": "chapter-handler",
            "job_type": "dynamic_stage_generate",
            "provider_kind": "text",
            "workflow_run_id": workflow_run_id,
            "workflow_stage_id": workflow_stage_id,
        }
    )


def _workflow_statuses(
    repository: JobRepository,
    run_id: str,
    stage_id: str,
) -> tuple[str, str, int, str, str]:
    with repository.engine.connect() as connection:
        run_record = connection.execute(
            select(
                workflow_run.c.status,
                workflow_run.c.revision,
                workflow_run.c.error,
            ).where(workflow_run.c.id == run_id)
        ).one()
        stage_record = connection.execute(
            select(
                workflow_stage.c.status,
                workflow_stage.c.error,
            ).where(workflow_stage.c.id == stage_id)
        ).one()
    return (
        str(run_record.status),
        str(stage_record.status),
        int(run_record.revision),
        str(run_record.error),
        str(stage_record.error),
    )


def test_dynamic_handler_uses_only_database_and_fails_without_provider_call(
    dynamic_handler_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )

    handler = make_dynamic_stage_generate_handler(database, repository)
    assert handler(record) is None

    current = repository.require(record["id"])
    assert current["status"] == "failed"
    assert "Phase 2 动态 Stage handler 尚未实现" in current["error"]
    assert "Provider" in current["error"]
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "blocked",
        "blocked",
        2,
    )


@pytest.mark.parametrize(
    ("run_status", "stage_status"),
    [
        ("blocked", "queued"),
        ("queued", "cancelled"),
        ("stale", "running"),
        ("queued", "confirmed"),
        ("pending", "queued"),
        ("queued", "pending"),
    ],
)
def test_dynamic_handler_interrupts_without_overwriting_non_executable_workflow(
    dynamic_handler_context: tuple[Database, JobRepository],
    run_status: str,
    stage_status: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(
        database,
        run_status=run_status,
        stage_status=stage_status,
    )
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )

    handler = make_dynamic_stage_generate_handler(database, repository)
    handler(record)

    current = repository.require(record["id"])
    assert current["status"] == "interrupted"
    assert "不可执行" in current["error"]
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        run_status,
        stage_status,
        1,
    )


@pytest.mark.parametrize(
    ("workflow_run_id", "workflow_stage_id", "error_text"),
    [
        (None, None, "缺少关联的工作流运行"),
        ("run-missing-stage", None, "缺少关联的工作流阶段"),
    ],
)
def test_dynamic_handler_fails_missing_workflow_association_without_creating_stage(
    dynamic_handler_context: tuple[Database, JobRepository],
    workflow_run_id: str | None,
    workflow_stage_id: str | None,
    error_text: str,
) -> None:
    database, repository = dynamic_handler_context
    if workflow_run_id == "run-missing-stage":
        workflow_run_id, _ = _insert_workflow(database)
    record = _create_job(
        repository,
        workflow_run_id=workflow_run_id,
        workflow_stage_id=workflow_stage_id,
    )

    handler = make_dynamic_stage_generate_handler(database, repository)
    handler(record)

    current = repository.require(record["id"])
    assert current["status"] == "failed"
    assert error_text in current["error"]
    with repository.engine.connect() as connection:
        stages = connection.execute(select(workflow_stage)).all()
    assert stages == [] if workflow_run_id is None else len(stages) == 1


class _RecordingRunner:
    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository
        self.handlers: dict[str, Any] = {}

    def register_handler(self, job_type: str, handler: Any, *, replace: bool = False) -> None:
        del replace
        self.handlers[job_type] = handler


def test_register_handlers_adds_dynamic_stage_generate_without_starting_runner(
    dynamic_handler_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dynamic_handler_context
    runner = _RecordingRunner(repository)

    register_handlers(database=database, runner=cast(JobRunner, runner))

    assert callable(runner.handlers["dynamic_stage_generate"])
    assert list(runner.handlers)[-1] == "dynamic_stage_generate"
