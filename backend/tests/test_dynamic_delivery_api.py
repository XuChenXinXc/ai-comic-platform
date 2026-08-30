from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.api import dynamic_runs
from app.db import Database
from app.dynamic_pipeline.delivery import DynamicDeliveryService
from app.dynamic_pipeline.stage_catalog import STAGE_KEYS
from app.models.tables import chapter, project, shot, workflow_run, workflow_stage
from app.services.errors import ServiceError
from app.services.file_service import FileService


@dataclass
class DeliveryApiContext:
    client: TestClient
    database: Database
    run_id: str


@pytest.fixture
def delivery_api_context(tmp_path: Path) -> Iterator[DeliveryApiContext]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-delivery-api.db').as_posix()}")
    database.initialize(seed_mock_providers=False)
    project_id = "project-api-delivery"
    chapter_id = "chapter-api-delivery"
    run_id = "run api delivery"
    now = "2026-08-18T00:00:00+00:00"
    with database.engine.begin() as connection:
        connection.execute(
            insert(project).values(
                id=project_id,
                name="动态交付 API 测试项目",
                storage_root=str(tmp_path / "projects"),
                storage_name="动态交付 API 测试项目",
                storage_layout="categorized",
                is_free_create=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapter).values(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                title="第一章",
                source_text="测试正文",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_run).values(
                id=run_id,
                project_id=project_id,
                chapter_id=chapter_id,
                source_kind="full_text",
                status="confirmed",
                current_stage="6-delivery",
                source_snapshot_json=json.dumps({"text": "测试正文"}),
                config_snapshot_json=json.dumps({}),
                asset_mapping_json=json.dumps([]),
                input_signature="run-signature",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(workflow_stage),
            [
                {
                    "id": f"{run_id}-stage-{index}",
                    "workflow_run_id": run_id,
                    "stage_key": stage_key,
                    "ordinal": index,
                    "status": "confirmed",
                    "input_signature": f"{stage_key}-signature",
                    "raw_output_json": json.dumps({"stage": stage_key}),
                    "normalized_output_json": json.dumps({"stage": stage_key}),
                    "draft_output_json": json.dumps({"stage": stage_key}),
                    "output_refs_json": "[]",
                    "gate_result_json": json.dumps({"status": "ready", "passed": True}),
                    "attempt": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                for index, stage_key in enumerate(STAGE_KEYS)
            ],
        )
        connection.execute(
            insert(shot).values(
                id="shot-api-delivery-1",
                project_id=project_id,
                chapter_id=chapter_id,
                shot_no=1,
                shot_order=1,
                source_type="novel",
                source_ref="source-1",
                status="confirmed",
                created_at=now,
                updated_at=now,
            )
        )

    service = DynamicDeliveryService(
        database,
        file_service=FileService(tmp_path, database=database),
    )
    test_app = FastAPI()

    @test_app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    test_app.include_router(dynamic_runs.router, prefix="/api")
    test_app.dependency_overrides[dynamic_runs.get_dynamic_delivery_service] = lambda: service

    try:
        with TestClient(test_app) as client:
            yield DeliveryApiContext(client=client, database=database, run_id=run_id)
    finally:
        database.dispose()


def test_delivery_checklist_is_read_only_and_returns_preflight(
    delivery_api_context: DeliveryApiContext,
) -> None:
    context = delivery_api_context
    before = context.database.engine.connect().execute(select(workflow_run)).all()

    response = context.client.get(f"/api/dynamic-runs/{context.run_id}/delivery/checklist")

    assert response.status_code == 200
    body = response.json()
    assert "checklist" in body
    assert body["preflight"]["status"] == "blocked"
    assert body["preflight"]["missing"]
    after = context.database.engine.connect().execute(select(workflow_run)).all()
    assert after == before


def test_delivery_preflight_is_read_only_and_strict_options_reject_non_boolean(
    delivery_api_context: DeliveryApiContext,
) -> None:
    context = delivery_api_context
    before = context.database.engine.connect().execute(select(workflow_stage)).all()

    response = context.client.post(
        f"/api/dynamic-runs/{context.run_id}/delivery/preflight",
        json={"strict": True, "allow_partial": False},
    )
    invalid_response = context.client.post(
        f"/api/dynamic-runs/{context.run_id}/delivery/preflight",
        json={"strict": "true"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert invalid_response.status_code == 422
    after = context.database.engine.connect().execute(select(workflow_stage)).all()
    assert after == before


def test_delivery_strict_export_maps_blocked_preflight_to_conflict(
    delivery_api_context: DeliveryApiContext,
) -> None:
    context = delivery_api_context

    response = context.client.post(
        f"/api/dynamic-runs/{context.run_id}/delivery/export",
        json={"strict": True, "allow_partial": False},
    )

    assert response.status_code == 409
    assert "preflight" in response.json()["detail"]
    assert "missing" in response.json()["detail"]
    assert "C:\\" not in response.text
    assert "D:\\" not in response.text


def test_delivery_partial_export_returns_zip_with_manifest(
    delivery_api_context: DeliveryApiContext,
) -> None:
    context = delivery_api_context

    response = context.client.post(
        f"/api/dynamic-runs/{context.run_id}/delivery/export",
        json={
            "strict": True,
            "allow_partial": True,
            "exported_at": "2026-08-18T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    disposition = response.headers["content-disposition"]
    assert disposition == 'attachment; filename="dynamic-delivery-runapidelivery.zip"'
    assert "/" not in disposition
    assert "\\" not in disposition
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("metadata/manifest.json"))
    assert manifest["export_mode"] == "partial"
    assert manifest["status"] == "partial"
    assert manifest["missing"]


def test_delivery_missing_run_returns_not_found(delivery_api_context: DeliveryApiContext) -> None:
    response = delivery_api_context.client.get(
        "/api/dynamic-runs/run-that-does-not-exist/delivery/checklist"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "运行不存在"}
