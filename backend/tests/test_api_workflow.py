import base64
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api import assets, jobs, media, projects, providers, shots, writing
from app.db import Database
from app.dependencies import (
    get_asset_service,
    get_job_service,
    get_media_service,
    get_project_service,
    get_provider_service,
    get_shot_service,
    get_writing_service,
)
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import api_provider
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.file_service import file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.provider_service import ProviderService
from app.services.shot_service import ShotService
from app.services.task_handlers import register_handlers
from app.services.utils import json_dumps
from app.services.writing_service import WritingService

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database = Database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    with database.engine.begin() as connection:
        connection.execute(
            update(api_provider)
            .where(api_provider.c.id == "mock-video")
            .values(params_json=json_dumps({"task_store_dir": str(tmp_path / "provider-tasks")}))
        )
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)

    repository = JobRepository(database)
    runner = JobRunner(
        repository,
        worker_limits={"text": 1, "image": 2, "video": 1},
    )
    job_service = JobService(database, repository=repository, runner=runner)
    services = {
        get_project_service: ProjectService(database),
        get_job_service: job_service,
        get_writing_service: WritingService(database, job_service),
        get_asset_service: AssetService(database, job_service),
        get_shot_service: ShotService(database, job_service),
        get_media_service: MediaService(database, job_service),
        get_provider_service: ProviderService(database),
    }
    register_handlers(database=database, runner=runner)
    runner.start(recover=False)

    test_app = FastAPI()

    @test_app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    for router in (
        projects.router,
        writing.router,
        assets.router,
        shots.router,
        media.router,
        providers.router,
        jobs.router,
    ):
        test_app.include_router(router, prefix="/api")

    def override(value: Any) -> Any:
        def dependency() -> Any:
            return value

        return dependency

    for dependency, service in services.items():
        test_app.dependency_overrides[dependency] = override(service)

    with TestClient(test_app) as client:
        yield client

    runner.shutdown()
    database.dispose()


def _wait_job(client: TestClient, job_id: str, attempts: int = 400) -> dict[str, Any]:
    for _ in range(attempts):
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        record = response.json()
        if record["status"] in _TERMINAL:
            return record
        time.sleep(0.02)
    raise AssertionError(f"任务未在限定时间内结束：{job_id}")


def _wait_batch(client: TestClient, payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = [_wait_job(client, item["id"]) for item in payload["jobs"]]
    assert all(item["status"] == "completed" for item in records), records
    return records


def _create_project(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/projects",
        json={
            "name": "测试漫剧",
            "source_text": "主角走进旧屋，发现桌上的戒指。",
            "style_prompt": "二维国风漫剧",
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_text_provider(client: TestClient, name: str) -> dict[str, Any]:
    response = client.post(
        "/api/providers",
        json={
            "kind": "text",
            "name": name,
            "adapter": "mock",
            "model": "mock-text",
            "is_default": False,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_mock_provider_full_workflow_returns_current_media_and_zip(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    project_id = project["id"]

    screenplay_job = api_client.post(f"/api/projects/{project_id}/screenplay/generate", json={})
    assert screenplay_job.status_code == 202
    assert _wait_job(api_client, screenplay_job.json()["id"])["status"] == "completed"

    extract_job = api_client.post(f"/api/projects/{project_id}/assets/extract", json={})
    assert extract_job.status_code == 202
    assert _wait_job(api_client, extract_job.json()["id"])["status"] == "completed"
    extracted_assets = api_client.get(f"/api/projects/{project_id}/assets").json()
    assert len(extracted_assets) == 3

    prompt_batch = api_client.post(f"/api/projects/{project_id}/assets/prompts/batch", json={})
    assert prompt_batch.status_code == 202
    _wait_batch(api_client, prompt_batch.json())
    image_batch = api_client.post(f"/api/projects/{project_id}/assets/images/batch", json={})
    assert image_batch.status_code == 202
    _wait_batch(api_client, image_batch.json())

    shot_job = api_client.post(
        f"/api/projects/{project_id}/shots/extract",
        json={"source_type": "screenplay", "scope": "all"},
    )
    assert shot_job.status_code == 202
    assert _wait_job(api_client, shot_job.json()["id"])["status"] == "completed"
    current_shots = api_client.get(f"/api/projects/{project_id}/shots").json()
    assert len(current_shots) == 1
    shot_id = current_shots[0]["id"]

    shot_prompt_batch = api_client.post(f"/api/projects/{project_id}/shots/prompts/batch", json={})
    assert shot_prompt_batch.status_code == 202
    _wait_batch(api_client, shot_prompt_batch.json())
    keyframe_job = api_client.post(f"/api/shots/{shot_id}/keyframes/generate", json={})
    assert keyframe_job.status_code == 202
    assert _wait_job(api_client, keyframe_job.json()["id"])["status"] == "completed"
    keyframes = api_client.get(f"/api/shots/{shot_id}/keyframes").json()
    assert len(keyframes) == 1
    assert keyframes[0]["is_current"] is True
    assert keyframes[0]["is_stale"] is False

    video_job = api_client.post(f"/api/shots/{shot_id}/videos/generate", json={})
    assert video_job.status_code == 202
    assert _wait_job(api_client, video_job.json()["id"], attempts=1500)["status"] == "completed"
    videos = api_client.get(f"/api/shots/{shot_id}/videos").json()
    assert len(videos) == 1
    assert videos[0]["is_current"] is True
    assert api_client.get(f"/api/videos/{videos[0]['id']}/download").status_code == 200
    exported = api_client.get(f"/api/projects/{project_id}/videos/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert len(exported.content) > 0


def test_project_revision_and_asset_duplicate_conflict_return_expected_values(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    unchanged = api_client.put(
        f"/api/projects/{project['id']}",
        json={"source_text": project["source_text"]},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["source_revision"] == 1
    changed = api_client.put(
        f"/api/projects/{project['id']}",
        json={"source_text": "修改后的小说正文"},
    )
    assert changed.status_code == 200
    assert changed.json()["source_revision"] == 2
    assert changed.json()["source_signature"] != project["source_signature"]

    body = {"asset_type": "character", "name": "主角"}
    assert api_client.post(f"/api/projects/{project['id']}/assets", json=body).status_code == 201
    duplicate = api_client.post(f"/api/projects/{project['id']}/assets", json=body)
    assert duplicate.status_code == 409


def test_uploaded_media_enforces_unique_current_and_current_delete_conflict(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    asset_record = api_client.post(
        f"/api/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "主角"},
    ).json()
    first = api_client.post(
        f"/api/assets/{asset_record['id']}/images/upload",
        files={"file": ("one.png", _PNG, "image/png")},
    )
    second = api_client.post(
        f"/api/assets/{asset_record['id']}/images/upload",
        files={"file": ("two.png", _PNG, "image/png")},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    versions = api_client.get(f"/api/assets/{asset_record['id']}/images").json()
    assert sum(item["is_current"] for item in versions) == 1
    current = next(item for item in versions if item["is_current"])
    old = next(item for item in versions if not item["is_current"])
    assert api_client.delete(f"/api/asset-images/{current['id']}").status_code == 409
    assert api_client.delete(f"/api/asset-images/{old['id']}").status_code == 204
    assert api_client.delete(f"/api/asset-images/{current['id']}/current").status_code == 200
    assert api_client.delete(f"/api/asset-images/{current['id']}").status_code == 204


def test_asset_image_after_highest_version_deleted_uses_next_version_number(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    asset_record = api_client.post(
        f"/api/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "主角"},
    ).json()
    first = api_client.post(
        f"/api/assets/{asset_record['id']}/images/upload",
        files={"file": ("one.png", _PNG, "image/png")},
    )
    assert first.status_code == 201
    second = api_client.post(
        f"/api/assets/{asset_record['id']}/images/upload",
        files={"file": ("two.png", _PNG, "image/png")},
    )
    assert second.status_code == 201
    assert api_client.put(f"/api/asset-images/{first.json()['id']}/current").status_code == 200
    assert api_client.delete(f"/api/asset-images/{second.json()['id']}").status_code == 204

    third = api_client.post(
        f"/api/assets/{asset_record['id']}/images/upload",
        files={"file": ("three.png", _PNG, "image/png")},
    )

    assert third.status_code == 201
    assert third.json()["version_no"] == 3
    assert third.json()["file_path"].endswith("/v003.png")


def test_shot_split_copy_merge_and_reorder_preserve_contiguous_numbers(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    first = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "主角走入房间，然后停下观察。"},
    ).json()
    second = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "主角拿起戒指。"},
    ).json()
    reordered = api_client.post(
        f"/api/projects/{project['id']}/shots/reorder",
        json={"ids": [second["id"], first["id"]]},
    )
    assert reordered.status_code == 200
    assert [item["shot_no"] for item in reordered.json()] == [1, 2]

    copied = api_client.post(f"/api/shots/{first['id']}/copy", json={})
    assert copied.status_code == 201
    split = api_client.post(f"/api/shots/{second['id']}/split", json={})
    assert split.status_code == 200
    assert len(split.json()) == 2
    ids = [item["id"] for item in api_client.get(f"/api/projects/{project['id']}/shots").json()]
    merged = api_client.post(
        f"/api/projects/{project['id']}/shots/merge",
        json={"ids": ids[:2]},
    )
    assert merged.status_code == 200
    final_shots = api_client.get(f"/api/projects/{project['id']}/shots").json()
    assert [item["shot_no"] for item in final_shots] == list(range(1, len(final_shots) + 1))


def test_video_batch_keeps_valid_item_when_another_shot_lacks_keyframe(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    first = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "第一镜", "video_prompt": "0-6秒：主角前进。"},
    ).json()
    second = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "第二镜", "video_prompt": "0-6秒：主角停下。"},
    ).json()
    upload = api_client.post(
        f"/api/shots/{first['id']}/keyframes/upload",
        files={"file": ("frame.png", _PNG, "image/png")},
    )
    assert upload.status_code == 201
    response = api_client.post(
        f"/api/projects/{project['id']}/videos/batch",
        json={"ids": [first["id"], second["id"]]},
    )
    assert response.status_code == 202
    payload = response.json()
    assert len(payload["jobs"]) == 1
    assert payload["preflight_failures"][0]["target_id"] == second["id"]
    assert _wait_job(api_client, payload["jobs"][0]["id"], attempts=1500)["status"] == "completed"


def test_video_upload_with_undecodable_mp4_returns_400(api_client: TestClient) -> None:
    project = _create_project(api_client)
    current_shot = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "主角向前走。", "video_prompt": "0-6秒：镜头跟随主角。"},
    ).json()

    response = api_client.post(
        f"/api/shots/{current_shot['id']}/videos/upload",
        files={"file": ("broken.mp4", b"not-a-video", "video/mp4")},
    )

    assert response.status_code == 400


def test_video_generation_with_undecodable_result_marks_job_failed(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _create_project(api_client)
    current_shot = api_client.post(
        f"/api/projects/{project['id']}/shots",
        json={"action": "主角向前走。", "video_prompt": "0-6秒：镜头跟随主角。"},
    ).json()
    keyframe = api_client.post(
        f"/api/shots/{current_shot['id']}/keyframes/upload",
        files={"file": ("frame.png", _PNG, "image/png")},
    )
    assert keyframe.status_code == 201

    async def download_invalid_video(
        _provider: Any,
        _result: Any,
        target: Path,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-a-video")

    monkeypatch.setattr(
        MediaService,
        "_download_provider_result",
        staticmethod(download_invalid_video),
    )
    queued = api_client.post(f"/api/shots/{current_shot['id']}/videos/generate", json={})
    assert queued.status_code == 202

    completed = _wait_job(api_client, queued.json()["id"], attempts=1500)

    assert completed["status"] == "failed"


def test_provider_crud_rejects_unknown_adapter_and_keeps_single_default(
    api_client: TestClient,
) -> None:
    invalid = api_client.post(
        "/api/providers",
        json={
            "kind": "text",
            "name": "无效",
            "adapter": "missing",
            "is_default": False,
        },
    )
    assert invalid.status_code == 400
    created = api_client.post(
        "/api/providers",
        json={
            "kind": "text",
            "name": "第二个 Mock",
            "adapter": "mock",
            "model": "mock-text",
            "is_default": True,
        },
    )
    assert created.status_code == 201
    text_providers = api_client.get("/api/providers?kind=text").json()
    assert sum(item["is_default"] for item in text_providers) == 1
    tested = api_client.post(f"/api/providers/{created.json()['id']}/test")
    assert tested.status_code == 200
    assert tested.json() == {"ok": True, "message": "Mock Provider 可用"}


def test_delete_unreferenced_provider_returns_204_and_removes_record(
    api_client: TestClient,
) -> None:
    provider = _create_text_provider(api_client, "未引用 Provider")

    response = api_client.delete(f"/api/providers/{provider['id']}")

    assert response.status_code == 204
    provider_ids = {item["id"] for item in api_client.get("/api/providers").json()}
    assert provider["id"] not in provider_ids


def test_delete_project_default_provider_returns_409_and_keeps_record(
    api_client: TestClient,
) -> None:
    provider = _create_text_provider(api_client, "项目默认 Provider")
    project_response = api_client.post(
        "/api/projects",
        json={
            "name": "引用 Provider 的项目",
            "default_text_provider_id": provider["id"],
        },
    )
    assert project_response.status_code == 201

    response = api_client.delete(f"/api/providers/{provider['id']}")

    assert response.status_code == 409
    provider_ids = {item["id"] for item in api_client.get("/api/providers").json()}
    assert provider["id"] in provider_ids
