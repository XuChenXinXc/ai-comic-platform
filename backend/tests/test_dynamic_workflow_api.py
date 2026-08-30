from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.api import chapters, dynamic_runs, projects
from app.db import Database
from app.dependencies import (
    get_chapter_service,
    get_dynamic_workflow_service,
    get_project_service,
)
from app.dynamic_pipeline.prompt_ir import (
    CompiledPromptBundle,
    ImagePromptBundle,
    TimelineSegment,
    VideoPromptBundle,
)
from app.dynamic_pipeline.prompt_stage import PromptStageOutput, stage_output_to_stage_json
from app.dynamic_pipeline.schemas import ArtifactMeta
from app.dynamic_pipeline.stage_catalog import STAGE_KEYS
from app.main import app as main_app
from app.models.tables import chapter, project, workflow_run, workflow_stage
from app.services.chapter_service import ChapterService
from app.services.dynamic_workflow_service import DynamicWorkflowService
from app.services.errors import ServiceError
from app.services.file_service import FileService, file_service
from app.services.project_service import ProjectService
from app.services.utils import json_dumps


@dataclass
class ApiContext:
    client: TestClient
    database: Database


@pytest.fixture
def api_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiContext]:
    database = Database(f"sqlite:///{(tmp_path / 'dynamic-api.db').as_posix()}")
    database.initialize(seed_mock_providers=True)

    workspace_dir = (tmp_path / "workspace").resolve()
    projects_dir = (workspace_dir / "projects").resolve()
    projects_dir.mkdir(parents=True, exist_ok=True)

    # 保留全局 FileService 的测试隔离，避免测试项目落到真实工作区。
    monkeypatch.setattr(file_service, "workspace_dir", workspace_dir)
    monkeypatch.setattr(file_service, "projects_dir", projects_dir)
    monkeypatch.setattr(file_service, "database", database)

    storage = FileService(workspace_dir, database=database)
    project_service = ProjectService(database, storage=storage)
    chapter_service = ChapterService(database, storage=storage)
    dynamic_workflow_service = DynamicWorkflowService(database, storage=storage)

    test_app = FastAPI()

    @test_app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    for router in (projects.router, chapters.router, dynamic_runs.router):
        test_app.include_router(router, prefix="/api")

    def override(value: Any) -> Any:
        def dependency() -> Any:
            return value

        return dependency

    test_app.dependency_overrides[get_project_service] = override(project_service)
    test_app.dependency_overrides[get_chapter_service] = override(chapter_service)
    test_app.dependency_overrides[get_dynamic_workflow_service] = override(dynamic_workflow_service)

    try:
        with TestClient(test_app) as client:
            yield ApiContext(client=client, database=database)
    finally:
        database.dispose()


def _run_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "动态测试漫画",
        "chapter_title": "第一章：旧屋",
        "synopsis": "主角走进旧屋，在桌上发现一枚发光的戒指。",
    }
    payload.update(overrides)
    return payload


def _source_draft() -> dict[str, Any]:
    return {
        "artifact_meta": {
            "schema_version": "dynamic-source-v1",
            "normalizer_version": "dynamic-normalizer-v1",
            "input_signature": "api-source-input",
            "content_signature": "api-source-content",
            "source_attempt": 1,
        },
        "metadata": {
            "source_kind": "synopsis",
            "title": "动态测试漫画",
            "chapter_title": "第一章：旧屋",
            "source_signature": "api-source-signature",
            "target_duration_sec": 30,
            "aspect_ratio": "9:16",
        },
        "episode_logline": "主角在旧屋发现发光的戒指。",
        "scenes": [
            {
                "scene_id": "scene-1",
                "ordinal": 1,
                "location": "旧屋",
                "time_weather": "夜，阴",
                "characters": [],
                "narrative_task": "建立线索",
                "plot_event": "主角发现发光的戒指",
                "conflict_or_turn": "戒指引出未知危险",
                "visual_focus": "主角拿起发光的戒指",
                "estimated_duration_sec": 30,
                "shootability": {
                    "status": "shootable",
                    "visible_action": "主角拿起发光的戒指",
                    "required_asset_keys": [],
                    "production_risks": [],
                    "adaptation_note": None,
                },
                "source_refs": ["synopsis:1"],
            }
        ],
        "character_roster": [],
        "conflict_turns": [],
        "visualizable_content": ["旧屋和发光的戒指"],
        "hard_to_visualize": [],
        "rewrite_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "continuation_outline": [],
    }


def _create_free_run(api: ApiContext, **overrides: Any) -> dict[str, Any]:
    response = api.client.post("/api/free-create/dynamic-runs", json=_run_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def _create_project(api: ApiContext, *, is_free_create: bool = False) -> dict[str, Any]:
    response = api.client.post(
        "/api/projects",
        json={
            "name": "接口测试项目",
            "is_free_create": is_free_create,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _count(database: Database, table: Any) -> int:
    with database.engine.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def _assert_invalid_stage(response: Any) -> None:
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "literal_error"
    assert detail[0]["loc"] == ["path", "stage_key"]


def test_free_create_synopsis_creates_free_project_chapter_run_and_eight_stages(
    api_context: ApiContext,
) -> None:
    payload = _run_payload()

    response = api_context.client.post("/api/free-create/dynamic-runs", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert list(body) == ["run", "stages"]
    run = body["run"]
    stages = body["stages"]
    assert run["status"] == "pending"
    assert run["current_stage"] == "0-source"
    assert run["source_kind"] == "synopsis"
    assert run["config_snapshot"]["strong_dynamic_max_events"] == 3
    assert run["source_snapshot"] == {
        "source_kind": "synopsis",
        "text": payload["synopsis"],
        "title": payload["title"],
        "chapter_title": payload["chapter_title"],
    }
    assert run["project_id"]
    assert run["chapter_id"]
    assert run["project_id"] != run["chapter_id"]
    assert [stage["stage_key"] for stage in stages] == list(STAGE_KEYS)
    assert [stage["status"] for stage in stages] == ["pending"] * 8

    project_response = api_context.client.get(f"/api/projects/{run['project_id']}")
    chapter_response = api_context.client.get(f"/api/chapters/{run['chapter_id']}")
    project_chapters_response = api_context.client.get(
        f"/api/projects/{run['project_id']}/chapters"
    )
    assert project_response.status_code == 200
    assert project_response.json()["is_free_create"] is True
    assert chapter_response.status_code == 200
    assert chapter_response.json()["project_id"] == run["project_id"]
    assert chapter_response.json()["source_text"] == payload["synopsis"]
    assert project_chapters_response.status_code == 200
    assert project_chapters_response.json() == [chapter_response.json() | {"shot_count": 0}]

    with api_context.database.engine.connect() as connection:
        persisted_free_flag = connection.scalar(
            select(project.c.is_free_create).where(project.c.id == run["project_id"])
        )
    assert persisted_free_flag == 1


def test_dynamic_run_persists_controlled_strong_dynamic_budget_setting(
    api_context: ApiContext,
) -> None:
    created = _create_free_run(api_context, strong_dynamic_max_events=1)

    assert created["run"]["config_snapshot"]["strong_dynamic_max_events"] == 1


def test_project_dynamic_run_rejects_regular_project_and_mismatched_project_id(
    api_context: ApiContext,
) -> None:
    regular_project = _create_project(api_context)
    regular_response = api_context.client.post(
        f"/api/projects/{regular_project['id']}/dynamic-runs",
        json=_run_payload(),
    )
    assert regular_response.status_code == 400
    assert regular_response.json() == {"detail": "动态运行只能创建于 free-create 项目"}

    free_project = _create_project(api_context, is_free_create=True)
    free_response = api_context.client.post(
        f"/api/projects/{free_project['id']}/dynamic-runs",
        json=_run_payload(project_id=free_project["id"]),
    )
    assert free_response.status_code == 201
    assert free_response.json()["run"]["project_id"] == free_project["id"]

    mismatch_response = api_context.client.post(
        f"/api/projects/{free_project['id']}/dynamic-runs",
        json=_run_payload(project_id=regular_project["id"]),
    )
    assert mismatch_response.status_code == 400
    assert mismatch_response.json() == {"detail": "请求体 project_id 与路径 project_id 不一致"}


def test_dynamic_run_reads_do_not_create_resources_and_missing_run_or_stage_returns_404(
    api_context: ApiContext,
) -> None:
    created = _create_free_run(api_context)
    run = created["run"]
    run_id = run["id"]
    project_id = run["project_id"]
    counts_before_reads = (
        _count(api_context.database, project),
        _count(api_context.database, chapter),
        _count(api_context.database, workflow_run),
        _count(api_context.database, workflow_stage),
    )

    list_response = api_context.client.get(f"/api/projects/{project_id}/dynamic-runs")
    run_response = api_context.client.get(f"/api/dynamic-runs/{run_id}")
    stage_response = api_context.client.get(f"/api/dynamic-runs/{run_id}/stages/0-source")
    assert list_response.status_code == 200
    assert list_response.json() == [run]
    assert run_response.status_code == 200
    assert run_response.json() == run
    assert stage_response.status_code == 200
    assert stage_response.json() == created["stages"][0]
    assert (
        _count(api_context.database, project),
        _count(api_context.database, chapter),
        _count(api_context.database, workflow_run),
        _count(api_context.database, workflow_stage),
    ) == counts_before_reads

    missing_run_response = api_context.client.get("/api/dynamic-runs/run_missing")
    assert missing_run_response.status_code == 404
    assert missing_run_response.json() == {"detail": "运行不存在"}

    with api_context.database.engine.begin() as connection:
        connection.execute(
            delete(workflow_stage).where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "6-delivery",
            )
        )
    missing_stage_response = api_context.client.get(f"/api/dynamic-runs/{run_id}/stages/6-delivery")
    assert missing_stage_response.status_code == 404
    assert missing_stage_response.json() == {"detail": "阶段不存在"}


def test_dynamic_run_actions_use_expected_revision_and_cancelled_run_cannot_resume(
    api_context: ApiContext,
) -> None:
    run_id = _create_free_run(api_context)["run"]["id"]

    start_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/start",
        json={"expected_revision": 1},
    )
    assert start_response.status_code == 200
    assert start_response.json()["run"]["status"] == "queued"
    assert start_response.json()["run"]["revision"] == 2
    assert start_response.json()["stages"][0]["status"] == "queued"

    wrong_revision_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/pause",
        json={"expected_revision": 1},
    )
    assert wrong_revision_response.status_code == 409
    assert wrong_revision_response.json() == {"detail": "运行内容已被其他操作修改，请刷新后重试"}

    pause_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/pause",
        json={"expected_revision": 2},
    )
    assert pause_response.status_code == 200
    assert pause_response.json()["run"]["status"] == "blocked"
    assert pause_response.json()["run"]["revision"] == 3

    resume_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/resume",
        json={"expected_revision": 3},
    )
    assert resume_response.status_code == 200
    assert resume_response.json()["run"]["status"] == "queued"
    assert resume_response.json()["run"]["revision"] == 4

    cancel_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/cancel",
        json={"expected_revision": 4},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json()["run"]["status"] == "cancelled"
    assert cancel_response.json()["run"]["revision"] == 5
    assert cancel_response.json()["stages"][0]["status"] == "cancelled"

    resume_cancelled_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/resume",
        json={"expected_revision": 5},
    )
    assert resume_cancelled_response.status_code == 409
    assert resume_cancelled_response.json() == {"detail": "已取消的运行不能恢复"}


def test_stage_draft_gate_and_confirmation_bump_revision_and_advance_current_stage(
    api_context: ApiContext,
) -> None:
    created = _create_free_run(api_context)
    run_id = created["run"]["id"]

    draft_without_gate_response = api_context.client.put(
        f"/api/dynamic-runs/{run_id}/stages/0-source/draft",
        json={"expected_revision": 1, "draft_output": {"summary": ""}},
    )
    assert draft_without_gate_response.status_code == 200
    blocked_body = draft_without_gate_response.json()
    assert blocked_body["run"]["revision"] == 2
    blocked_stage = blocked_body["stages"][0]
    assert blocked_stage["status"] == "blocked"
    assert blocked_stage["gate_result"]["status"] == "blocked"

    repaired_response = api_context.client.put(
        f"/api/dynamic-runs/{run_id}/stages/0-source/draft",
        json={
            "expected_revision": 2,
            "draft_output": _source_draft(),
        },
    )
    assert repaired_response.status_code == 200
    repaired_body = repaired_response.json()
    assert repaired_body["run"]["revision"] == 3
    assert repaired_body["stages"][0]["status"] == "ready"
    assert repaired_body["stages"][0]["gate_result"]["status"] == "ready"

    confirm_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/0-source/confirm",
        json={"expected_revision": 3},
    )
    assert confirm_response.status_code == 200
    confirmed_body = confirm_response.json()
    assert confirmed_body["run"]["revision"] == 4
    assert confirmed_body["run"]["current_stage"] == "1-character-bios"
    assert confirmed_body["stages"][0]["status"] == "confirmed"
    assert confirmed_body["stages"][0]["confirmed_revision"] == 4


def test_legacy_summary_draft_uses_server_gate_and_can_confirm_without_client_gate(
    api_context: ApiContext,
) -> None:
    created = _create_free_run(api_context)
    run_id = created["run"]["id"]

    saved = api_context.client.put(
        f"/api/dynamic-runs/{run_id}/stages/0-source/draft",
        json={
            "expected_revision": 1,
            "draft_output": {"summary": "旧 API 摘要"},
            "gate_result": {
                "status": "blocked",
                "passed": False,
                "diagnostics": [],
                "can_confirm": False,
            },
        },
    )

    assert saved.status_code == 200
    assert saved.json()["stages"][0]["status"] == "ready"
    assert saved.json()["stages"][0]["gate_result"]["status"] == "warning"
    assert saved.json()["stages"][0]["gate_result"]["diagnostics"][0]["code"] == (
        "legacy_summary_compatibility"
    )

    confirmed = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/0-source/confirm",
        json={"expected_revision": 2},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["stages"][0]["status"] == "confirmed"


def _prompt_stage_draft(stage_key: str, input_signature: str) -> dict[str, Any]:
    artifact_meta = ArtifactMeta(
        schema_version=f"dynamic-{stage_key}-test-v1",
        normalizer_version="dynamic-prompt-test-v1",
        input_signature=input_signature,
        content_signature=f"{stage_key}-content",
        source_attempt=1,
    )
    if stage_key == "5a-image-prompts":
        image = ImagePromptBundle(prompt="合法静止画 Prompt", negative_prompt="文字")
        bundle = CompiledPromptBundle(
            status="ready",
            image=image,
            generated={"image": image},
            final={"image": image},
        )
    else:
        video = VideoPromptBundle(
            prompt="合法视频 Prompt",
            negative_prompt="文字",
            edit_duration_sec=6,
            generation_duration_sec=6,
            timeline=[TimelineSegment(start=0, end=6, action="稳定动作")],
            end_state="动作结束",
        )
        bundle = CompiledPromptBundle(
            status="ready",
            video=video,
            generated={"video": video},
            final={"video": video},
        )
    output = PromptStageOutput(
        stage_key=stage_key,  # type: ignore[arg-type]
        artifact_meta=artifact_meta,
        bundles={"shot-1": bundle},
        target_duration_sec=30,
        status="ready",
        provenance={"input_signature": input_signature},
    )
    return stage_output_to_stage_json(output)


@pytest.mark.parametrize("stage_key", ["5a-image-prompts", "5b-video-prompts"])
def test_prompt_stage_confirmation_recalculates_modified_draft_instead_of_old_gate(
    api_context: ApiContext,
    stage_key: str,
) -> None:
    created = _create_free_run(api_context)
    run_id = created["run"]["id"]
    input_signature = f"{stage_key}-input"
    draft = _prompt_stage_draft(stage_key, input_signature)
    channel = "image" if stage_key == "5a-image-prompts" else "video"
    draft["bundles"]["shot-1"][channel]["prompt"] = ""
    ready_gate = {
        "status": "ready",
        "passed": True,
        "diagnostics": [],
        "can_confirm": True,
    }

    with api_context.database.engine.begin() as connection:
        connection.execute(
            update(workflow_run)
            .where(workflow_run.c.id == run_id)
            .values(status="ready", current_stage=stage_key)
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == "4-storyboard",
            )
            .values(status="confirmed")
        )
        connection.execute(
            update(workflow_stage)
            .where(
                workflow_stage.c.workflow_run_id == run_id,
                workflow_stage.c.stage_key == stage_key,
            )
            .values(
                status="ready",
                input_signature=input_signature,
                draft_output_json=json_dumps(draft),
                normalized_output_json=json_dumps(draft),
                gate_result_json=json_dumps(ready_gate),
            )
        )

    response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/{stage_key}/confirm",
        json={"expected_revision": 1},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": f"阶段 {stage_key} 尚未满足确认门禁"}
    stage = api_context.client.get(f"/api/dynamic-runs/{run_id}/stages/{stage_key}").json()
    assert stage["status"] == "ready"
    assert stage["gate_result"]["status"] == "blocked"
    assert "prompt_missing" in {
        item["code"] for item in stage["gate_result"]["diagnostics"]
    }


def test_downstream_stage_confirmation_with_unconfirmed_prerequisite_returns_409(
    api_context: ApiContext,
) -> None:
    run_id = _create_free_run(api_context)["run"]["id"]

    response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/2-rhythm/confirm",
        json={"expected_revision": 1},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "阶段 '2-rhythm' 尚未满足前置确认: 1-character-bios"}


def test_regenerate_stage_creates_new_attempt_stales_descendants_and_rejects_repeat_action(
    api_context: ApiContext,
) -> None:
    created = _create_free_run(api_context)
    run_id = created["run"]["id"]

    regenerate_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/0-source/regenerate",
        json={"expected_revision": 1},
    )
    assert regenerate_response.status_code == 200
    regenerated = regenerate_response.json()
    assert regenerated["run"]["status"] == "queued"
    assert regenerated["run"]["revision"] == 2
    assert regenerated["run"]["current_stage"] == "0-source"
    assert regenerated["stages"][0]["attempt"] == 2
    assert regenerated["stages"][0]["status"] == "queued"
    assert [stage["status"] for stage in regenerated["stages"][1:]] == ["stale"] * 7

    repeated_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/0-source/regenerate",
        json={"expected_revision": 2},
    )
    assert repeated_response.status_code == 409
    assert repeated_response.json() == {"detail": "阶段 0-source 正在处理中，不能重复生成"}

    invalid_stage_response = api_context.client.post(
        f"/api/dynamic-runs/{run_id}/stages/9-invalid/regenerate",
        json={"expected_revision": 2},
    )
    _assert_invalid_stage(invalid_stage_response)


def test_main_app_registers_dynamic_routes_and_legacy_project_chapter_smoke_stays_available(
    api_context: ApiContext,
) -> None:
    route_paths = set(main_app.openapi()["paths"])
    expected_paths = {
        "/api/free-create/dynamic-runs",
        "/api/projects/{project_id}/dynamic-runs",
        "/api/dynamic-runs/{run_id}",
        "/api/dynamic-runs/{run_id}/stages/{stage_key}",
        "/api/dynamic-runs/{run_id}/start",
        "/api/dynamic-runs/{run_id}/pause",
        "/api/dynamic-runs/{run_id}/resume",
        "/api/dynamic-runs/{run_id}/cancel",
        "/api/dynamic-runs/{run_id}/stages/{stage_key}/draft",
        "/api/dynamic-runs/{run_id}/stages/{stage_key}/confirm",
        "/api/dynamic-runs/{run_id}/stages/{stage_key}/regenerate",
    }
    assert expected_paths.issubset(route_paths)
    assert "/api/projects" in route_paths
    assert "/api/projects/{project_id}/chapters" in route_paths
    assert "/api/chapters/{chapter_id}" in route_paths

    project_record = _create_project(api_context)
    chapter_response = api_context.client.post(
        f"/api/projects/{project_record['id']}/chapters",
        json={"title": "旧接口章节", "source_text": "旧接口 smoke 正文"},
    )
    assert chapter_response.status_code == 201
    chapter_record = chapter_response.json()

    project_list_response = api_context.client.get("/api/projects")
    chapter_list_response = api_context.client.get(f"/api/projects/{project_record['id']}/chapters")
    chapter_get_response = api_context.client.get(f"/api/chapters/{chapter_record['id']}")
    assert project_list_response.status_code == 200
    assert any(item["id"] == project_record["id"] for item in project_list_response.json())
    assert chapter_list_response.status_code == 200
    assert chapter_list_response.json()[0]["id"] == chapter_record["id"]
    assert chapter_get_response.status_code == 200
    assert chapter_get_response.json() == chapter_record
