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

from app.api import assets, chapters, jobs, media, projects, providers, shots, writing
from app.db import Database
from app.dependencies import (
    get_asset_service,
    get_chapter_service,
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
from app.services.chapter_service import ChapterService
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
    # 服务层多数路径操作（media/asset/shot 等）直接使用全局 file_service 单例，
    # 显式把它的 database 绑定到本测试库，与 test_audio_service_fix 的做法一致；
    # 否则 _project_storage 会查询生产库，项目按 prefixed 布局生成路径。
    monkeypatch.setattr(file_service, "database", database)
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)

    repository = JobRepository(database)
    runner = JobRunner(
        repository,
        worker_limits={"text": 1, "image": 2, "video": 1},
    )
    job_service = JobService(database, repository=repository, runner=runner)
    services = {
        get_project_service: ProjectService(database),
        get_chapter_service: ChapterService(database),
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
        chapters.router,
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
            "style_prompt": "二维国风漫剧",
        },
    )
    assert response.status_code == 201
    return response.json()


def _create_chapter(client: TestClient, project_id: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "title": overrides.get("title", "第一章"),
        "source_text": overrides.get("source_text", "主角走进旧屋，发现桌上的戒指。"),
    }
    response = client.post(f"/api/projects/{project_id}/chapters", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_ready_scene(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/assets",
        json={
            "asset_type": "scene",
            "name": "测试场景",
            "prompt": "固定场景设定",
        },
    )
    assert response.status_code == 201
    scene = response.json()
    upload = client.post(
        f"/api/assets/{scene['id']}/images/upload",
        files={"file": ("scene.png", _PNG, "image/png")},
    )
    assert upload.status_code == 201
    return scene


def _confirm_screenplay(client: TestClient, chapter_id: str) -> None:
    """按真实工作流确认当前剧本：素材提取只接受已确认剧本。"""
    current = client.get(f"/api/chapters/{chapter_id}/screenplay").json()
    response = client.put(
        f"/api/chapters/{chapter_id}/screenplay",
        json={"content": current["content"], "status": "confirmed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"


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


def test_manual_style_prompt_survives_screenplay_and_reaches_asset_prompts(
    api_client: TestClient,
) -> None:
    """画风只允许用户手动设置：生成剧本不得改写它，且必须流入后续素材提示词。"""
    project = _create_project(api_client)
    project_id = project["id"]
    chapter_id = _create_chapter(api_client, project_id)["id"]
    expected_style_prompt = "用户手动设定的画风，克制线条，低饱和色调"
    manual = api_client.put(
        f"/api/projects/{project_id}", json={"style_prompt": expected_style_prompt}
    )
    assert manual.status_code == 200

    screenplay_job = api_client.post(f"/api/chapters/{chapter_id}/screenplay/generate", json={})
    assert screenplay_job.status_code == 202
    assert _wait_job(api_client, screenplay_job.json()["id"])["status"] == "completed"
    updated_project = api_client.get(f"/api/projects/{project_id}")
    assert updated_project.status_code == 200
    assert updated_project.json()["style_prompt"] == expected_style_prompt

    _confirm_screenplay(api_client, chapter_id)
    extract_job = api_client.post(f"/api/chapters/{chapter_id}/assets/extract", json={})
    assert extract_job.status_code == 202
    assert _wait_job(api_client, extract_job.json()["id"])["status"] == "completed"
    extracted_assets = api_client.get(f"/api/projects/{project_id}/assets").json()
    assert len(extracted_assets) == 3
    for current_asset in extracted_assets:
        assert expected_style_prompt in current_asset["compiled_prompt"]


def test_mock_provider_full_workflow_returns_current_media_and_zip(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    project_id = project["id"]
    chapter_id = _create_chapter(api_client, project_id)["id"]

    screenplay_job = api_client.post(f"/api/chapters/{chapter_id}/screenplay/generate", json={})
    assert screenplay_job.status_code == 202
    assert _wait_job(api_client, screenplay_job.json()["id"])["status"] == "completed"

    _confirm_screenplay(api_client, chapter_id)
    extract_job = api_client.post(f"/api/chapters/{chapter_id}/assets/extract", json={})
    assert extract_job.status_code == 202
    assert _wait_job(api_client, extract_job.json()["id"])["status"] == "completed"
    extracted_assets = api_client.get(f"/api/projects/{project_id}/assets").json()
    assert len(extracted_assets) == 3
    image_batch = api_client.post(f"/api/projects/{project_id}/assets/images/batch", json={})
    assert image_batch.status_code == 202
    _wait_batch(api_client, image_batch.json())
    template_markers = {
        "character": "分块拼接式专业角色设定面板",
        "scene": "分块拼接式专业场景设定面板",
        "prop": "分块拼接式专业道具设定面板",
    }
    storage_folders = {
        "character": "角色/",
        "scene": "场景/",
        "prop": "物品/",
    }
    for current_asset in extracted_assets:
        assert current_asset["prompt"]
        assert current_asset["negative_prompt"]
        assert current_asset["consistency_keywords"]
        assert current_asset["composition"]
        assert template_markers[current_asset["asset_type"]] in current_asset["compiled_prompt"]
        versions = api_client.get(f"/api/assets/{current_asset['id']}/images")
        assert versions.status_code == 200
        assert versions.json()[0]["prompt_snapshot"] == current_asset["compiled_prompt"]
        assert versions.json()[0]["file_path"].startswith(
            storage_folders[current_asset["asset_type"]]
        )

    shot_job = api_client.post(
        f"/api/chapters/{chapter_id}/shots/extract",
        json={"source_type": "screenplay", "scope": "all"},
    )
    assert shot_job.status_code == 202
    assert _wait_job(api_client, shot_job.json()["id"])["status"] == "completed"
    current_shots = api_client.get(f"/api/chapters/{chapter_id}/shots").json()
    assert len(current_shots) == 1
    shot_id = current_shots[0]["id"]
    assert current_shots[0]["image_prompt"]
    assert current_shots[0]["video_prompt"]
    assert current_shots[0]["negative_prompt"]
    assert "【参考】" in current_shots[0]["compiled_image_prompt"]
    assert "以 @Image1 作为这一镜的起始画面" in current_shots[0]["compiled_video_prompt"]
    assert "这一镜发生在" in current_shots[0]["compiled_video_prompt"]
    assert current_shots[0]["reference_bindings"]
    keyframe_job = api_client.post(f"/api/shots/{shot_id}/keyframes/generate", json={})
    assert keyframe_job.status_code == 202
    assert _wait_job(api_client, keyframe_job.json()["id"])["status"] == "completed"
    keyframes = api_client.get(f"/api/shots/{shot_id}/keyframes").json()
    assert len(keyframes) == 1
    assert keyframes[0]["is_current"] is True
    assert keyframes[0]["is_stale"] is False
    assert "【参考】" in keyframes[0]["prompt_snapshot"]
    assert "negative prompt:" in keyframes[0]["prompt_snapshot"]
    assert keyframes[0]["prompt_snapshot"] == current_shots[0]["compiled_image_prompt"]
    assert keyframes[0]["file_path"].startswith("第01章-第一章/分镜图/")

    video_job = api_client.post(f"/api/shots/{shot_id}/videos/generate", json={})
    assert video_job.status_code == 202
    assert _wait_job(api_client, video_job.json()["id"], attempts=1500)["status"] == "completed"
    videos = api_client.get(f"/api/shots/{shot_id}/videos").json()
    assert len(videos) == 1
    assert videos[0]["is_current"] is True
    assert "@Image1" in videos[0]["prompt_snapshot"]
    assert "画面中不出现" in videos[0]["prompt_snapshot"]
    assert videos[0]["negative_prompt_snapshot"]  # 负面提示词存在
    assert videos[0]["file_path"].startswith("第01章-第一章/分镜视频/")
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
    assert third.json()["file_path"] == "角色/角色-主角.png"


def test_shot_split_copy_merge_and_reorder_preserve_contiguous_numbers(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    first = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角走入房间，然后停下观察。", "duration_sec": 5},
    ).json()
    second = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角拿起戒指。", "duration_sec": 10},
    ).json()
    reordered = api_client.post(
        f"/api/chapters/{chapter['id']}/shots/reorder",
        json={"ids": [second["id"], first["id"]]},
    )
    assert reordered.status_code == 200
    assert [item["shot_no"] for item in reordered.json()] == [1, 2]

    copied = api_client.post(f"/api/shots/{first['id']}/copy", json={})
    assert copied.status_code == 201
    split = api_client.post(f"/api/shots/{second['id']}/split", json={})
    assert split.status_code == 200
    assert len(split.json()) == 2
    ids = [item["id"] for item in api_client.get(f"/api/chapters/{chapter['id']}/shots").json()]
    merged = api_client.post(
        f"/api/chapters/{chapter['id']}/shots/merge",
        json={"ids": ids[:2]},
    )
    assert merged.status_code == 200
    final_shots = api_client.get(f"/api/chapters/{chapter['id']}/shots").json()
    assert [item["shot_no"] for item in final_shots] == list(range(1, len(final_shots) + 1))


def test_shot_split_clears_prompt_overrides(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    current = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角走入房间，然后停下观察。",
            "duration_sec": 10,
            "image_prompt_override": "旧的最终关键帧提示词",
            "video_prompt_override": "旧的最终视频提示词",
        },
    ).json()

    response = api_client.post(f"/api/shots/{current['id']}/split", json={})

    assert response.status_code == 200
    assert [item["image_prompt_override"] for item in response.json()] == ["", ""]
    assert [item["video_prompt_override"] for item in response.json()] == ["", ""]


def test_shot_merge_clears_prompt_overrides(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    target = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角走入房间。",
            "duration_sec": 5,
            "image_prompt_override": "旧的最终关键帧提示词",
            "video_prompt_override": "旧的最终视频提示词",
        },
    ).json()
    source = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角停下观察。", "duration_sec": 5},
    ).json()

    response = api_client.post(
        f"/api/chapters/{chapter['id']}/shots/merge",
        json={"ids": [target["id"], source["id"]]},
    )

    assert response.status_code == 200
    assert response.json()["image_prompt_override"] == ""
    assert response.json()["video_prompt_override"] == ""


def test_shot_create_requires_duration_and_forces_screenplay_source(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])

    missing_duration = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。"},
    )
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "source_type": "novel",
            "action": "主角进入房间。",
            "duration_sec": 5,
        },
    )
    too_short = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角停在门口。", "duration_sec": 4},
    )

    assert missing_duration.status_code == 422
    assert too_short.status_code == 422
    assert created.status_code == 201
    assert created.json()["source_type"] == "screenplay"
    assert created.json()["duration_sec"] == 5


def test_shot_create_rejects_duration_above_fifteen(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])

    response = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。", "duration_sec": 16},
    )

    assert response.status_code == 422


def test_shot_create_with_legacy_nested_fields_normalizes_aliases(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])

    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "duration_sec": 5,
            "positions": [
                {
                    "character_id": "char-1",
                    "side": "画面左侧",
                    "legacy_note": "旧版站位备注",
                }
            ],
            "expressions": [
                {
                    "character_name": "小明",
                    "emotion": "紧张",
                    "legacy_note": "旧版表情备注",
                }
            ],
            "dialogue": [
                {
                    "name": "小明",
                    "line": "快走。",
                    "legacy_note": "旧版对白备注",
                }
            ],
        },
    )

    assert created.status_code == 201
    assert created.json()["positions"] == [
        {
            "character": "char-1",
            "position": "画面左侧",
            "facing": None,
            "gaze": None,
            "action_direction": None,
        }
    ]
    assert created.json()["expressions"] == [{"character": "小明", "expression": "紧张"}]
    assert created.json()["dialogue"] == [{"speaker": "小明", "text": "快走。"}]


def test_shot_update_with_legacy_nested_fields_updates_duration(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"duration_sec": 5},
    )
    assert created.status_code == 201

    updated = api_client.put(
        f"/api/shots/{created.json()['id']}",
        json={
            "duration_sec": 5,
            "positions": [{"name": "小明", "location": "门边", "legacy_note": "保留"}],
            "expressions": [{"character_id": "char-1", "mood": "警惕"}],
            "dialogue": [{"character": "小明", "content": "别出声。"}],
        },
    )

    assert updated.status_code == 200
    assert updated.json()["duration_sec"] == 5


def test_shot_update_rejects_duration_below_five(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。", "duration_sec": 5},
    )
    assert created.status_code == 201

    response = api_client.put(
        f"/api/shots/{created.json()['id']}",
        json={"duration_sec": 4},
    )

    assert response.status_code == 422


def test_shot_update_rejects_duration_above_fifteen(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。", "duration_sec": 5},
    )
    assert created.status_code == 201

    response = api_client.put(
        f"/api/shots/{created.json()['id']}",
        json={"duration_sec": 16},
    )

    assert response.status_code == 422


def test_shot_update_explicit_nulls_normalized_instead_of_500(
    api_client: TestClient,
) -> None:
    """显式传 null 的文本/JSON/状态字段归一化，而不是写坏 NOT NULL 列。

    回归：此前 {"action": null} 会触发 IntegrityError 500，
    {"character_ids": null} 会写 "null" 进 JSON 列。
    """
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。", "duration_sec": 5},
    )
    assert created.status_code == 201

    response = api_client.put(
        f"/api/shots/{created.json()['id']}",
        json={
            "action": None,
            "start_state": None,
            "character_ids": None,
            "status": None,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == ""
    assert payload["start_state"] == ""
    assert payload["character_ids"] == []
    # status 显式 null 视为保持不变（新建分镜为 draft）
    assert payload["status"] == "draft"


def test_shot_split_rejects_duration_below_five(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角进入房间。", "duration_sec": 10},
    )
    assert created.status_code == 201

    response = api_client.post(
        f"/api/shots/{created.json()['id']}/split",
        json={
            "parts": [
                {"action": "主角迈步。", "duration_sec": 4},
                {"action": "主角停下。", "duration_sec": 6},
            ]
        },
    )

    assert response.status_code == 422


def test_video_batch_keeps_valid_item_when_another_shot_lacks_keyframe(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    scene = _create_ready_scene(api_client, project["id"])
    first = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "scene_asset_id": scene["id"],
            "action": "第一镜",
            "video_prompt": "0-6秒：主角前进。",
            "duration_sec": 6,
        },
    ).json()
    second = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "scene_asset_id": scene["id"],
            "action": "第二镜",
            "video_prompt": "0-6秒：主角停下。",
            "duration_sec": 6,
        },
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


def test_keyframe_without_image_prompt_returns_400_even_with_frame_description(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角推开房门。",
            "frame_description": "主角站在旧屋门口，右手按住木门。",
            "duration_sec": 5,
        },
    )
    assert created.status_code == 201
    current_shot = created.json()

    response = api_client.post(
        f"/api/shots/{current_shot['id']}/keyframes/generate",
        json={},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "分镜缺少明确的关键帧提示词"
    assert api_client.get(f"/api/shots/{current_shot['id']}/keyframes").json() == []


def test_keyframe_without_visual_description_returns_400(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"action": "主角推开房门。", "duration_sec": 5},
    )
    assert created.status_code == 201

    response = api_client.post(
        f"/api/shots/{created.json()['id']}/keyframes/generate",
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "分镜缺少明确的关键帧提示词"


def test_video_without_video_prompt_returns_400_even_with_shot_action(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "start_state": "主角站在门外。",
            "action": "主角推开房门并向屋内迈出一步。",
            "end_state": "主角进入旧屋。",
            "camera_move": "缓慢跟随",
            "duration_sec": 5,
        },
    )
    assert created.status_code == 201
    current_shot = created.json()
    uploaded = api_client.post(
        f"/api/shots/{current_shot['id']}/keyframes/upload",
        files={"file": ("frame.png", _PNG, "image/png")},
    )
    assert uploaded.status_code == 201

    response = api_client.post(
        f"/api/shots/{current_shot['id']}/videos/generate",
        json={},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "分镜缺少明确的视频提示词"
    assert api_client.get(f"/api/shots/{current_shot['id']}/videos").json() == []


def test_video_without_action_description_returns_400(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={"frame_description": "主角站在旧屋门口。", "duration_sec": 5},
    )
    assert created.status_code == 201
    current_shot = created.json()
    uploaded = api_client.post(
        f"/api/shots/{current_shot['id']}/keyframes/upload",
        files={"file": ("frame.png", _PNG, "image/png")},
    )
    assert uploaded.status_code == 201

    response = api_client.post(
        f"/api/shots/{current_shot['id']}/videos/generate",
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "分镜缺少明确的视频提示词"


def test_video_upload_with_undecodable_mp4_returns_400(api_client: TestClient) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    current_shot = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角向前走。",
            "video_prompt": "0-6秒：镜头跟随主角。",
            "duration_sec": 6,
        },
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
    chapter = _create_chapter(api_client, project["id"])
    scene = _create_ready_scene(api_client, project["id"])
    current_shot = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "scene_asset_id": scene["id"],
            "action": "主角向前走。",
            "video_prompt": "0-6秒：镜头跟随主角。",
            "duration_sec": 6,
        },
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


def test_keyframe_generate_and_batch_accept_auto_prepare_references(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角推开房门。",
            "image_prompt": "主角站在旧屋门口，右手按住木门。",
            "video_prompt": "镜头缓缓推进，木门被推开。",
            "duration_sec": 5,
        },
    )
    assert created.status_code == 201
    shot_id = created.json()["id"]

    # 单镜生成接收 auto_prepare_references 不应报 422 Extra inputs are not permitted
    keyframe_job = api_client.post(
        f"/api/shots/{shot_id}/keyframes/generate",
        json={"auto_prepare_references": True},
    )
    assert keyframe_job.status_code == 202

    # 批量生成接收 auto_prepare_references 不应报 422 Extra inputs are not permitted
    batch_job = api_client.post(
        f"/api/chapters/{chapter['id']}/keyframes/batch",
        json={"ids": [shot_id], "auto_prepare_references": True},
    )
    assert batch_job.status_code == 202


def test_shot_consistency_issues_prefixed_with_source_target(
    api_client: TestClient,
) -> None:
    project = _create_project(api_client)
    chapter = _create_chapter(api_client, project["id"])
    created = api_client.post(
        f"/api/chapters/{chapter['id']}/shots",
        json={
            "action": "主角推开房门。",
            "image_prompt": "主角站在旧屋门口，地面呈现真实反射。",
            "video_prompt": "主角向前迈步，地面呈现真实反射。",
            "duration_sec": 5,
        },
    )
    assert created.status_code == 201
    shot_data = created.json()
    issues = shot_data.get("consistency_issues") or []
    image_issue = [item for item in issues if item["message"].startswith("【分镜图】")]
    video_issue = [item for item in issues if item["message"].startswith("【视频】")]
    assert len(image_issue) >= 1
    assert len(video_issue) >= 1
    assert "真实反射" in image_issue[0]["message"]
    assert "真实反射" in video_issue[0]["message"]


