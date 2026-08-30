from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api import dynamic_runs
from app.db import Database, migrate_schema
from app.dependencies import get_dynamic_workflow_service
from app.jobs.dynamic_bridge import DynamicJobBridge
from app.jobs.repository import JobRepository
from app.main import lifespan
from app.models.tables import job, workflow_run, workflow_stage
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.file_service import FileService
from app.services.utils import json_loads


@dataclass
class JobIntegrationContext:
    database: Database
    repository: JobRepository
    storage: FileService

    def client(self, enqueue: Any) -> TestClient:
        service = DynamicWorkflowService(
            self.database,
            storage=self.storage,
            job_bridge=DynamicJobBridge(self.repository, enqueue=enqueue),
        )
        test_app = FastAPI()

        @test_app.exception_handler(ServiceError)
        async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        test_app.include_router(dynamic_runs.router, prefix="/api")
        test_app.dependency_overrides[get_dynamic_workflow_service] = lambda: service
        return TestClient(test_app)


@pytest.fixture
def job_integration_context(tmp_path: Path):
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-job-integration.db').as_posix()}")
    migrate_schema(database)
    database.seed_mock_providers()
    storage = FileService(tmp_path / "workspace", database=database)
    context = JobIntegrationContext(database, JobRepository(database), storage)
    try:
        yield context
    finally:
        database.dispose()


class FakeEnqueue:
    def __init__(
        self,
        database: Database,
        *,
        result: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.database = database
        self.result = result
        self.error = error
        self.called: list[str] = []
        self.visible_statuses: dict[str, str] = {}

    def __call__(self, job_id: str) -> bool:
        with self.database.engine.connect() as connection:
            record = connection.execute(select(job).where(job.c.id == job_id)).first()
        if record is None:
            raise AssertionError("enqueue callback 未能读取已提交的 Job")
        self.called.append(job_id)
        self.visible_statuses[job_id] = str(record.status)
        if self.error is not None:
            raise self.error
        return self.result


def _payload() -> dict[str, str]:
    return {
        "title": "动态 Job 集成测试",
        "chapter_title": "第一章",
        "synopsis": "主角在旧屋中发现一枚发光的戒指。",
    }


def _create_run(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/free-create/dynamic-runs", json=_payload())
    assert response.status_code == 201, response.text
    return response.json()


def _jobs_for(context: JobIntegrationContext, run_id: str) -> list[dict[str, Any]]:
    return context.repository.list_by_workflow(run_id)


def _latest_stage(context: JobIntegrationContext, run_id: str, stage_key: str) -> dict[str, Any]:
    with context.database.engine.connect() as connection:
        row = connection.execute(
            select(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .order_by(workflow_stage.c.attempt.desc())
            .limit(1)
        ).first()
    assert row is not None
    return dict(row._mapping)


def test_start_enqueues_after_commit_and_creates_dynamic_job(
    job_integration_context: JobIntegrationContext,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        response = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 1},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "queued"
    assert body["stages"][0]["status"] == "queued"
    jobs = _jobs_for(context, run_id)
    assert len(jobs) == 1
    dynamic_job = jobs[0]
    assert fake_enqueue.called == [dynamic_job["id"]]
    assert fake_enqueue.visible_statuses[dynamic_job["id"]] == "queued"
    assert dynamic_job["job_type"] == "dynamic_stage_generate"
    assert dynamic_job["target_type"] == "workflow_stage"
    assert dynamic_job["workflow_run_id"] == run_id
    assert dynamic_job["workflow_stage_id"] == body["stages"][0]["id"]
    assert dynamic_job["target_id"] == body["stages"][0]["id"]
    assert dynamic_job["provider_kind"] == "text"
    assert json_loads(dynamic_job["input_json"], {})["stage_key"] == "0-source"


def test_create_auto_run_queues_first_stage_after_commit_idempotently(
    job_integration_context: JobIntegrationContext,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database)

    with context.client(fake_enqueue) as client:
        response = client.post(
            "/api/free-create/dynamic-runs",
            json={**_payload(), "auto_run": True},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    run_id = body["run"]["id"]
    assert body["run"]["status"] == "queued"
    assert body["stages"][0]["status"] == "queued"
    jobs = _jobs_for(context, run_id)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert fake_enqueue.called == [jobs[0]["id"]]
    assert fake_enqueue.visible_statuses[jobs[0]["id"]] == "queued"
    assert DynamicJobBridge(context.repository).reconcile_queued_stage_jobs() == [jobs[0]["id"]]
    assert len(_jobs_for(context, run_id)) == 1


@pytest.mark.parametrize(
    ("result", "error"),
    [(False, None), (True, RuntimeError("模拟队列不可用"))],
    ids=["enqueue-false", "enqueue-error"],
)
def test_enqueue_failure_keeps_workflow_and_job_queued_for_recovery(
    job_integration_context: JobIntegrationContext,
    result: bool,
    error: Exception | None,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database, result=result, error=error)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        response = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 1},
        )

    assert response.status_code == 200, response.text
    dynamic_job = _jobs_for(context, run_id)[0]
    assert dynamic_job["status"] == "queued"
    with context.database.engine.connect() as connection:
        run_status = connection.scalar(
            select(workflow_run.c.status).where(workflow_run.c.id == run_id)
        )
        stage_status = connection.scalar(
            select(workflow_stage.c.status).where(
                workflow_stage.c.id == dynamic_job["workflow_stage_id"]
            )
        )
    assert (run_status, stage_status) == ("queued", "queued")
    assert DynamicJobBridge(context.repository).reconcile_queued_stage_jobs() == [dynamic_job["id"]]


@pytest.mark.parametrize("failed_status", ["failed", "interrupted"])
def test_resume_retries_failed_or_interrupted_job_in_place(
    job_integration_context: JobIntegrationContext,
    failed_status: str,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database, result=False)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        started = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 1},
        )
        assert started.status_code == 200, started.text

        original_job = _jobs_for(context, run_id)[0]
        with context.database.engine.begin() as connection:
            connection.execute(
                update(workflow_run).where(workflow_run.c.id == run_id).values(status=failed_status)
            )
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == original_job["workflow_stage_id"])
                .values(status=failed_status)
            )
            connection.execute(
                update(job).where(job.c.id == original_job["id"]).values(status=failed_status)
            )

        response = client.post(
            f"/api/dynamic-runs/{run_id}/resume",
            json={"expected_revision": 2},
        )

    assert response.status_code == 200, response.text
    jobs = _jobs_for(context, run_id)
    assert len(jobs) == 1
    retried = jobs[0]
    assert retried["id"] == original_job["id"]
    assert retried["idempotency_key"] == original_job["idempotency_key"]
    assert retried["retry_count"] == original_job["retry_count"] + 1
    assert retried["status"] == "queued"


def test_resume_retries_partial_failed_stage_with_failed_dynamic_job(
    job_integration_context: JobIntegrationContext,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database, result=False)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        started = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 1},
        )
        assert started.status_code == 200, started.text

        original_job = _jobs_for(context, run_id)[0]
        with context.database.engine.begin() as connection:
            connection.execute(
                update(workflow_run)
                .where(workflow_run.c.id == run_id)
                .values(status="partial_failed")
            )
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == original_job["workflow_stage_id"])
                .values(status="partial_failed")
            )
            connection.execute(
                update(job).where(job.c.id == original_job["id"]).values(status="failed")
            )

        response = client.post(
            f"/api/dynamic-runs/{run_id}/resume",
            json={"expected_revision": 2},
        )

    assert response.status_code == 200, response.text
    retried = _jobs_for(context, run_id)[0]
    assert retried["id"] == original_job["id"]
    assert retried["status"] == "queued"
    assert retried["retry_count"] == original_job["retry_count"] + 1


def test_regenerate_creates_new_attempt_job_and_stales_descendants(
    job_integration_context: JobIntegrationContext,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database, result=False)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        response = client.post(
            f"/api/dynamic-runs/{run_id}/stages/0-source/regenerate",
            json={"expected_revision": 1},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        blocked = _create_run(client)
        blocked_run_id = blocked["run"]["id"]
        blocked_response = client.post(
            f"/api/dynamic-runs/{blocked_run_id}/stages/2-rhythm/regenerate",
            json={"expected_revision": 1},
        )

    assert body["stages"][0]["attempt"] == 2
    assert body["stages"][0]["status"] == "queued"
    assert [stage["status"] for stage in body["stages"][1:]] == ["stale"] * 7
    with context.database.engine.connect() as connection:
        attempts = connection.execute(
            select(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "0-source",
            )
            .order_by(workflow_stage.c.attempt)
        ).all()
    assert len(attempts) == 2
    assert attempts[0].id != attempts[1].id
    assert attempts[0].status == "pending"
    jobs = _jobs_for(context, run_id)
    assert len(jobs) == 1
    assert jobs[0]["workflow_stage_id"] == attempts[1].id
    assert jobs[0]["idempotency_key"] != ""
    assert blocked_response.status_code == 409
    assert blocked_response.json() == {
        "detail": "阶段 '2-rhythm' 尚未满足前置确认: 1-character-bios"
    }
    assert _jobs_for(context, blocked_run_id) == []
    with context.database.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(workflow_stage)
                .where(workflow_stage.c.workflow_run_id == blocked_run_id)
            )
            == 8
        )
        assert (
            connection.scalar(
                select(workflow_run.c.revision).where(workflow_run.c.id == blocked_run_id)
            )
            == 1
        )


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_terminal_job_rejects_queued_stage_and_rolls_back(
    job_integration_context: JobIntegrationContext,
    terminal_status: str,
) -> None:
    context = job_integration_context
    fake_enqueue = FakeEnqueue(context.database, result=False)

    with context.client(fake_enqueue) as client:
        created = _create_run(client)
        run_id = created["run"]["id"]
        started = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 1},
        )
        assert started.status_code == 200, started.text
        original_job = _jobs_for(context, run_id)[0]
        with context.database.engine.begin() as connection:
            connection.execute(
                update(workflow_run).where(workflow_run.c.id == run_id).values(status="pending")
            )
            connection.execute(
                update(workflow_stage)
                .where(workflow_stage.c.id == original_job["workflow_stage_id"])
                .values(status="pending")
            )
            connection.execute(
                update(job).where(job.c.id == original_job["id"]).values(status=terminal_status)
            )
        response = client.post(
            f"/api/dynamic-runs/{run_id}/start",
            json={"expected_revision": 2},
        )

    assert response.status_code == 409
    with context.database.engine.connect() as connection:
        state = connection.execute(
            select(
                workflow_run.c.status,
                workflow_run.c.revision,
                workflow_stage.c.status,
                job.c.status,
            )
            .select_from(
                workflow_run.join(
                    workflow_stage,
                    workflow_stage.c.workflow_run_id == workflow_run.c.id,
                ).join(job, job.c.workflow_run_id == workflow_run.c.id)
            )
            .where(workflow_run.c.id == run_id)
        ).first()
    assert state is not None
    assert tuple(state) == ("pending", 2, "pending", terminal_status)


def test_main_lifespan_uses_recover_all_once() -> None:
    source = inspect.getsource(lifespan)
    assert "job_runner.start(recover=False)" in source
    # 应当使用统一的 recover_all_once 入口（包含 dynamic + 普通路径去重）
    assert "recover_all_once(repository)" in source
    # 不再分别调用两条旧路径
    assert "enqueue_recovered_dynamic_jobs(repository" not in source
    # for 循环应该对 recover_all_once 的返回值做 enqueue
    start_index = source.index("job_runner.start(recover=False)")
    recover_index = source.index("recover_all_once(repository)")
    assert start_index < recover_index
