from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert, select

from app.db import Database, utc_now
from app.dynamic_pipeline.prompt_stage_service import PromptStageExecutionError
from app.dynamic_pipeline.stage_service import StageExecutionError
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
    stage_key: str = "0-source",
) -> tuple[str, str]:
    now = utc_now()
    run_id = f"run-{stage_key}-{run_status}-{stage_status}-{revision}"
    stage_id = f"stage-{stage_key}-{run_status}-{stage_status}-{revision}"
    ordinal = {
        "0-source": 0,
        "1-character-bios": 1,
        "2-rhythm": 2,
        "3-script": 3,
        "4-storyboard": 4,
        "5a-image-prompts": 5,
        "5b-video-prompts": 6,
        "6-delivery": 7,
    }[stage_key]
    with database.engine.begin() as connection:
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id="project-handler",
                chapter_id="chapter-handler",
                source_kind="full_text",
                status=run_status,
                current_stage=stage_key,
                revision=revision,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage).values(
                id=stage_id,
                workflow_run_id=run_id,
                stage_key=stage_key,
                ordinal=ordinal,
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
    stage_key: str | None = None,
) -> dict[str, Any]:
    input_data = {"stage_key": stage_key} if stage_key is not None else {}
    return repository.create_job(
        {
            "project_id": "project-handler",
            "chapter_id": "chapter-handler",
            "job_type": "dynamic_stage_generate",
            "provider_kind": "text",
            "workflow_run_id": workflow_run_id,
            "workflow_stage_id": workflow_stage_id,
            "input_data": input_data,
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


class _FakeStageService:
    def __init__(
        self,
        result: dict[str, Any] | None = None,
        error: StageExecutionError | None = None,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def execute_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(record)
        if self.error is not None:
            raise self.error
        return self.result


class _FakePromptStageService:
    def __init__(
        self,
        result: dict[str, Any] | None = None,
        error: PromptStageExecutionError | None = None,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def execute_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(record)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize("stage_key", ["0-source", "1-character-bios", "2-rhythm"])
def test_dynamic_handler_calls_phase2_stage_service_and_returns_result(
    dynamic_handler_context: tuple[Database, JobRepository],
    stage_key: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database, stage_key=stage_key)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )
    stage_service = _FakeStageService(
        {"stage_id": stage_id, "stage_key": stage_key, "stage_status": "ready"}
    )

    handler = make_dynamic_stage_generate_handler(database, repository, stage_service)
    assert asyncio.run(handler(record)) == stage_service.result
    assert [call["id"] for call in stage_service.calls] == [record["id"]]

    current = repository.require(record["id"])
    assert current["status"] == "queued"
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


@pytest.mark.parametrize(
    "stage_key",
    ["6-delivery"],
)
def test_dynamic_handler_executes_delivery_path_and_exposes_blocked_prerequisites(
    dynamic_handler_context: tuple[Database, JobRepository],
    stage_key: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database, stage_key=stage_key)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
        stage_key=stage_key,
    )
    stage_service = _FakeStageService()

    handler = make_dynamic_stage_generate_handler(database, repository, stage_service)
    result = asyncio.run(handler(record))
    assert stage_service.calls == []
    assert result["status"] == "partial_failed"

    current = repository.require(record["id"])
    assert current["status"] == "failed"
    assert "6-delivery" in current["error"]
    assert "Phase 2 动态 Stage handler 尚未实现" not in current["error"]
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "partial_failed",
        "partial_failed",
        2,
    )


@pytest.mark.parametrize("stage_key", ["5a-image-prompts", "5b-video-prompts"])
def test_dynamic_handler_calls_prompt_stage_service_and_returns_result(
    dynamic_handler_context: tuple[Database, JobRepository],
    stage_key: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database, stage_key=stage_key)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
        stage_key=stage_key,
    )
    stage_service = _FakeStageService()
    prompt_stage_service = _FakePromptStageService(
        {"stage_id": stage_id, "stage_key": stage_key, "stage_status": "blocked"}
    )

    handler = make_dynamic_stage_generate_handler(
        database,
        repository,
        stage_service,
        prompt_stage_service,
    )
    assert asyncio.run(handler(record)) == prompt_stage_service.result
    assert [call["id"] for call in prompt_stage_service.calls] == [record["id"]]
    assert stage_service.calls == []

    current = repository.require(record["id"])
    assert current["status"] == "queued"
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted", "cancelled"])
def test_dynamic_handler_returns_after_prompt_stage_execution_error_terminal_status(
    dynamic_handler_context: tuple[Database, JobRepository],
    terminal_status: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database, stage_key="5a-image-prompts")
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
        stage_key="5a-image-prompts",
    )

    class _TerminalErrorPromptStageService(_FakePromptStageService):
        async def execute_job(self, record: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(record)
            if terminal_status == "completed":
                repository.complete(record["id"], {"from": "prompt-stage-service"})
            else:
                repository.set_status(
                    record["id"],
                    terminal_status,
                    error="prompt service terminal error",
                )
            raise PromptStageExecutionError("Prompt 阶段服务状态冲突")

    prompt_stage_service = _TerminalErrorPromptStageService()
    handler = make_dynamic_stage_generate_handler(
        database,
        repository,
        _FakeStageService(),
        prompt_stage_service,
    )
    result = asyncio.run(handler(record))

    assert repository.require(record["id"])["status"] == terminal_status
    if terminal_status == "completed":
        assert result == {"from": "prompt-stage-service"}
    else:
        assert result == {"stage_status": "queued", "error": "Prompt 阶段服务状态冲突"}
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


@pytest.mark.parametrize("stage_key", ["3-script", "4-storyboard"])
def test_dynamic_handler_calls_script_and_storyboard_stage_service(
    dynamic_handler_context: tuple[Database, JobRepository],
    stage_key: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database, stage_key=stage_key)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
        stage_key=stage_key,
    )
    stage_service = _FakeStageService()

    handler = make_dynamic_stage_generate_handler(database, repository, stage_service)
    assert asyncio.run(handler(record)) == stage_service.result
    assert [call["id"] for call in stage_service.calls] == [record["id"]]

    current = repository.require(record["id"])
    assert current["status"] == "queued"
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted", "cancelled"])
def test_dynamic_handler_returns_after_stage_execution_error_terminal_status(
    dynamic_handler_context: tuple[Database, JobRepository],
    terminal_status: str,
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )

    class _TerminalErrorStageService(_FakeStageService):
        async def execute_job(self, record: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(record)
            if terminal_status == "completed":
                repository.complete(record["id"], {"from": "stage-service"})
            else:
                repository.set_status(
                    record["id"],
                    terminal_status,
                    error="service terminal error",
                )
            raise StageExecutionError("阶段服务状态冲突")

    stage_service = _TerminalErrorStageService()
    handler = make_dynamic_stage_generate_handler(database, repository, stage_service)
    result = asyncio.run(handler(record))

    assert repository.require(record["id"])["status"] == terminal_status
    if terminal_status == "completed":
        assert result == {"from": "stage-service"}
    else:
        assert result == {"stage_status": "queued", "error": "阶段服务状态冲突"}
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


def test_dynamic_handler_interrupts_non_terminal_job_after_stage_execution_error(
    dynamic_handler_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )
    stage_service = _FakeStageService(error=StageExecutionError("阶段服务执行冲突"))

    handler = make_dynamic_stage_generate_handler(database, repository, stage_service)
    result = asyncio.run(handler(record))

    current = repository.require(record["id"])
    assert current["status"] == "interrupted"
    assert result == {"stage_status": "queued", "error": "阶段服务执行冲突"}
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
    )


def test_dynamic_handler_terminal_error_is_not_completed_by_runner(
    dynamic_handler_context: tuple[Database, JobRepository],
) -> None:
    database, repository = dynamic_handler_context
    run_id, stage_id = _insert_workflow(database)
    record = _create_job(
        repository,
        workflow_run_id=run_id,
        workflow_stage_id=stage_id,
    )

    class _FailedStageService(_FakeStageService):
        async def execute_job(self, record: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(record)
            repository.fail(record["id"], "service failure")
            raise StageExecutionError("阶段服务状态冲突")

    stage_service = _FailedStageService()
    runner = JobRunner(repository=repository, worker_limits={"text": 1, "image": 1, "video": 1})
    runner.register_handler(
        "dynamic_stage_generate",
        make_dynamic_stage_generate_handler(database, repository, stage_service),
    )
    try:
        runner._execute(record["id"])
    finally:
        runner.shutdown()

    assert repository.require(record["id"])["status"] == "failed"
    assert stage_service.calls
    assert _workflow_statuses(repository, run_id, stage_id)[:3] == (
        "queued",
        "queued",
        1,
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
    asyncio.run(handler(record))

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
