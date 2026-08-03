import asyncio
import base64
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from app.api.schemas import ShotCreate
from app.db import Database
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import (
    api_provider,
    asset,
    asset_image,
    keyframe_version,
    novel_analysis,
    screenplay,
    video_version,
)
from app.schemas import ProviderSubmission
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.file_service import FileService, FileServiceError, file_service
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import utc_now

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_PROVIDER_RESOLVE_ERROR = (
    "Provider 配置失效：https://provider.invalid/task?token=secret-token\n"
    "Authorization: Bearer top-secret"
)


def _shot_create(**values: Any) -> ShotCreate:
    values.setdefault("duration_sec", 6)
    return ShotCreate(**values)


@pytest.fixture
def job_service(tmp_path: Path) -> JobService:
    database = Database(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create(
        {"name": "任务测试", "source_text": ""}
    )
    repository = JobRepository(database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    service = JobService(database, repository=repository, runner=runner)
    service.project_id = project["id"]  # type: ignore[attr-defined]
    yield service
    runner.shutdown()
    database.dispose()


@pytest.fixture
def media_job_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    database = Database(f"sqlite:///{(tmp_path / 'media-jobs.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    monkeypatch.setattr(file_service, "workspace_dir", tmp_path.resolve())
    monkeypatch.setattr(file_service, "projects_dir", (tmp_path / "projects").resolve())
    file_service.projects_dir.mkdir(parents=True, exist_ok=True)
    repository = JobRepository(database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    jobs = JobService(database, repository=repository, runner=runner)
    project = ProjectService(database).create({"name": "媒体任务测试", "source_text": ""})
    media = MediaService(database, jobs)
    scene = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "scene", "name": "默认场景", "prompt": "默认场景设定"},
    )
    scene_image = media.upload_asset_image(scene["id"], "scene.png", "image/png", _PNG)
    yield {
        "database": database,
        "repository": repository,
        "runner": runner,
        "jobs": jobs,
        "media": media,
        "project": project,
        "scene": scene,
        "scene_image": scene_image,
    }
    runner.shutdown()
    database.dispose()


def _wait_for_job_status(
    repository: JobRepository,
    job_id: str,
    expected_status: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = repository.require(job_id)
        if record["status"] == expected_status:
            return record
        time.sleep(0.01)
    pytest.fail(f"任务未进入预期状态：{expected_status}")


def _fail_second_provider_resolve(
    media: MediaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_resolve = media.providers.resolve
    resolve_count = 0

    def resolve(*args: Any, **kwargs: Any) -> Any:
        nonlocal resolve_count
        resolve_count += 1
        if resolve_count == 1:
            return original_resolve(*args, **kwargs)
        raise RuntimeError(_PROVIDER_RESOLVE_ERROR)

    monkeypatch.setattr(media.providers, "resolve", resolve)


@pytest.mark.parametrize(
    ("target_type", "expected_size"),
    [
        ("character", "3840x2560"),
        ("scene", "3840x2160"),
        ("prop", "3840x2560"),
        ("keyframe", "3840x2160"),
    ],
)
def test_gpt_image_2_generation_params_use_target_size(
    target_type: str,
    expected_size: str,
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    if target_type == "keyframe":
        current = ShotService(database, jobs).create(
            project["id"],
            _shot_create(
                scene_asset_id=media_job_services["scene"]["id"],
                action="主角走入画面",
                image_prompt="主角站在画面中央",
            ).model_dump(),
        )
        queued = media.queue_keyframe(
            current["id"],
            {"params": {"size": "1024x1024"}},
        )
    else:
        current = AssetService(database, jobs).create(
            project["id"],
            {
                "asset_type": target_type,
                "name": f"{target_type}测试素材",
                "prompt": "设定图",
            },
        )
        queued = media.queue_asset_image(
            current["id"],
            {"params": {"size": "1024x1024"}},
        )

    assert queued["input_data"]["params"]["size"] == expected_size


def test_asset_image_queue_without_prompt_or_visual_profile_returns_bad_request(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "空白角色"},
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_asset_image(current["id"], {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "素材缺少可用于生图的真实视觉描述"
    assert media.list_asset_images(current["id"]) == []


def test_asset_image_queue_with_visual_profile_without_prompt_is_allowed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "小明",
            "profile": {"appearance": "黑色短发，清瘦脸型，深色夹克"},
        },
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    queued = media.queue_asset_image(current["id"], {})

    assert queued["target_type"] == "asset_image"


def test_keyframe_queue_without_explicit_image_prompt_returns_bad_request(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(frame_description="主角站在门口", image_prompt="").model_dump(),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(current["id"], {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "分镜缺少明确的关键帧提示词"
    assert media.list_keyframes(current["id"]) == []


def test_video_queue_without_explicit_video_prompt_returns_bad_request(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            start_state="主角站在门口",
            action="主角推门",
            end_state="木门打开",
            video_prompt="",
        ).model_dump(),
    )
    media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)

    with pytest.raises(ServiceError) as exc_info:
        media.queue_video(current["id"], {})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "分镜缺少明确的视频提示词"
    assert media.list_videos(current["id"]) == []


def test_shot_prompt_batch_with_image_and_video_updates_both_prompts(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    project = media_job_services["project"]
    shots = ShotService(database, jobs)
    current = shots.create(
        project["id"],
        _shot_create(action="主角推开房门").model_dump(),
    )
    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)

    batch = shots.queue_prompt_batch(
        project["id"],
        {
            "ids": [current["id"]],
            "prompt_types": ["image", "video"],
            "only_missing": False,
            "params": {},
        },
    )
    queued_jobs = [repository.require(item["id"]) for item in batch["jobs"]]

    asyncio.run(shots.handle_image_prompt(queued_jobs[0]))
    asyncio.run(shots.handle_video_prompt(queued_jobs[1]))

    updated = shots.get(current["id"])
    assert updated["image_prompt"]
    assert updated["video_prompt"]


def test_keyframe_queue_uses_saved_final_prompt_override_verbatim(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "用户编辑后的最终关键帧提示词\n保持原样，不再套用模板。"
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            image_prompt="",
            image_prompt_override=override,
        ).model_dump(),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_keyframe(current["id"], {})

    version = media.list_keyframes(current["id"])[0]
    assert version["prompt_snapshot"] == override
    assert ShotService(database, jobs).get(current["id"])["final_image_prompt"] == override


def test_keyframe_after_provider_model_change_returns_stale_with_later_version_timestamp(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            image_prompt="主角站在房门前",
        ).model_dump(),
    )
    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)

    media.queue_keyframe(current["id"], {})
    version = media.list_keyframes(current["id"])[0]
    with database.engine.begin() as connection:
        connection.execute(
            update(api_provider)
            .where(api_provider.c.id == "mock-image")
            .values(model="new-image-model", updated_at="2026-08-03T00:00:00+00:00")
        )
        connection.execute(
            update(keyframe_version)
            .where(keyframe_version.c.id == version["id"])
            .values(created_at="2026-08-04T00:00:00+00:00")
        )

    assert media.list_keyframes(current["id"])[0]["is_stale"] is True


def test_video_queue_uses_saved_final_prompt_override_verbatim(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "用户编辑后的最终视频提示词\n严格按此文本执行。"
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            video_prompt="",
            video_prompt_override=override,
        ).model_dump(),
    )
    media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_video(current["id"], {})

    version = media.list_videos(current["id"])[0]
    assert version["prompt_snapshot"] == override
    assert ShotService(database, jobs).get(current["id"])["final_video_prompt"] == override


def test_uploaded_keyframe_uses_saved_final_prompt_override(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "上传关键帧使用的最终提示词"
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            image_prompt_override=override,
        ).model_dump(),
    )

    version = media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)

    assert version["prompt_snapshot"] == override


def test_asset_image_versions_use_readable_current_and_flat_history_paths(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林舟",
            "prompt": "黑色短发，灰色风衣",
        },
    )

    first = media.upload_asset_image(current["id"], "first.png", "image/png", _PNG)
    second = media.upload_asset_image(current["id"], "second.jpg", "image/jpeg", _PNG)

    versions = {item["version_no"]: item for item in media.list_asset_images(current["id"])}
    assert versions[1]["file_path"] == "角色/角色-林舟-v001.png"
    assert versions[1]["is_current"] is False
    assert versions[2]["file_path"] == "角色/角色-林舟.jpg"
    assert versions[2]["is_current"] is True
    assert file_service.resolve_project_path(project["id"], versions[1]["file_path"]).is_file()
    assert file_service.resolve_project_path(project["id"], versions[2]["file_path"]).is_file()

    media.set_current("asset_image", first["id"])

    switched = {item["version_no"]: item for item in media.list_asset_images(current["id"])}
    assert switched[1]["file_path"] == "角色/角色-林舟.png"
    assert switched[1]["is_current"] is True
    assert switched[2]["file_path"] == "角色/角色-林舟-v002.jpg"
    assert switched[2]["is_current"] is False
    assert second["id"] == switched[2]["id"]


def test_asset_image_sanitized_name_collision_keeps_each_asset_file(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    assets = AssetService(database, jobs)
    first_asset = assets.create(
        project["id"],
        {"asset_type": "character", "name": "A?", "prompt": "第一个角色"},
    )
    second_asset = assets.create(
        project["id"],
        {"asset_type": "character", "name": "A*", "prompt": "第二个角色"},
    )

    media.upload_asset_image(first_asset["id"], "first-v1.png", "image/png", _PNG)
    media.upload_asset_image(first_asset["id"], "first-v2.png", "image/png", _PNG)
    media.upload_asset_image(second_asset["id"], "second-v1.png", "image/png", _PNG)

    versions = [
        *media.list_asset_images(first_asset["id"]),
        *media.list_asset_images(second_asset["id"]),
    ]
    file_paths = [item["file_path"] for item in versions]
    assert len(set(file_paths)) == 3
    for relative in file_paths:
        path = file_service.resolve_project_path(project["id"], relative)
        assert path.read_bytes() == _PNG


def test_asset_image_upload_with_occupied_target_keeps_existing_file(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_asset = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林舟", "prompt": "角色设定"},
    )
    occupied_relative = "角色/角色-林舟-v001.png"
    occupied = file_service.write_bytes_atomic(
        project["id"],
        occupied_relative,
        b"existing",
    )

    with pytest.raises(FileServiceError, match="媒体目标文件已存在"):
        media.upload_asset_image(current_asset["id"], "new.png", "image/png", _PNG)

    assert occupied.read_bytes() == b"existing"
    assert media.list_asset_images(current_asset["id"]) == []


def test_uploaded_video_uses_saved_final_prompt_override(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "上传视频使用的最终提示词"
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            video_prompt_override=override,
        ).model_dump(),
    )
    keyframe = media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 1.0)

    version = media.upload_video(current["id"], "clip.mp4", "video/mp4", b"video")

    assert version["prompt_snapshot"] == override
    assert version["keyframe_version_id"] == keyframe["id"]


def test_keyframe_queue_without_scene_binding_returns_conflict_before_provider_resolution(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(image_prompt="主角站在画面中央").model_dump(),
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: pytest.fail("素材门禁前不应解析图片 Provider"),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(current["id"], {})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "分镜尚未绑定场景素材，请先在分镜编辑中绑定场景素材"
    assert media.list_keyframes(current["id"]) == []


def test_video_queue_without_scene_binding_returns_conflict_before_provider_resolution(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = ShotService(database, jobs).create(
        project["id"],
        _shot_create(video_prompt="主角缓慢转身并停稳").model_dump(),
    )
    media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: pytest.fail("素材门禁前不应解析视频 Provider"),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_video(current["id"], {})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "分镜尚未绑定场景素材，请先在分镜编辑中绑定场景素材"
    assert media.list_videos(current["id"]) == []


def test_grok_video_queue_rounds_fractional_duration_up_consistently(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角转身",
            video_prompt="主角缓慢转身并停稳。",
            duration_sec=2.5,
        ).model_dump(),
    )
    media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-video",
                "model": "grok-imagine-1.5-video-apimart",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_video(current_shot["id"], {})

    version = media.list_videos(current_shot["id"])[0]
    assert version["requested_duration_sec"] == 6
    assert "6秒" in version["prompt_snapshot"]
    assert version["is_stale"] is False


def test_keyframe_job_auto_binds_current_asset_image_as_reference(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林风",
            "prompt": "黑发少年剑修",
        },
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            action="林风站在山门石阶上",
            image_prompt="林风站在山门石阶中央。",
        ).model_dump(),
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    captured: dict[str, Any] = {}

    class FakeImageProvider:
        async def submit(self, **kwargs: Any) -> ProviderSubmission:
            captured["reference_files"] = kwargs["reference_files"]
            return ProviderSubmission(
                status="completed",
                result={"content_base64": base64.b64encode(_PNG).decode("ascii")},
            )

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeImageProvider(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    queued = media.queue_keyframe(current_shot["id"], {})
    result = asyncio.run(media.handle_keyframe(repository.require(queued["id"])))
    refreshed = ShotService(database, jobs).get(current_shot["id"])
    version = media.list_keyframes(current_shot["id"])[0]

    assert refreshed["reference_bindings"] == [
        {
            "role": "scene_layout",
            "asset_id": media_job_services["scene"]["id"],
            "asset_image_id": media_job_services["scene_image"]["id"],
        },
        {
            "role": "character_identity",
            "asset_id": character["id"],
            "asset_image_id": character_image["id"],
        },
    ]
    assert version["reference_bindings"] == refreshed["reference_bindings"]
    assert [path.read_bytes() for path in captured["reference_files"]] == [_PNG, _PNG]
    assert result["version_id"] == queued["target_id"]


def test_keyframe_job_with_invalid_download_removes_target_file(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角走入画面",
            image_prompt="主角站在画面中央",
        ).model_dump(),
    )

    class InvalidImageProvider:
        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(
                status="completed",
                result={"content_base64": base64.b64encode(b"not-an-image").decode("ascii")},
            )

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            InvalidImageProvider(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    queued = media.queue_keyframe(current_shot["id"], {})
    target = file_service.keyframe_path(
        project["id"],
        current_shot["id"],
        1,
        storage_name=media._project_storage_name(project["id"]),
    )

    with pytest.raises(ServiceError, match="不是可解码图片"):
        asyncio.run(media.handle_keyframe(repository.require(queued["id"])))

    assert target.exists() is False


def test_keyframe_job_with_failed_target_cleanup_marks_version_failed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            image_prompt="主角站在画面中央",
        ).model_dump(),
    )

    class InvalidImageProvider:
        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(
                status="completed",
                result={"content_base64": base64.b64encode(b"not-an-image").decode("ascii")},
            )

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            InvalidImageProvider(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    def fail_target_cleanup(_project_id: str, _relative_path: str | Path) -> bool:
        raise PermissionError("坏文件仍被占用")

    monkeypatch.setattr(file_service, "delete_file", fail_target_cleanup)

    queued = media.queue_keyframe(current_shot["id"], {})

    with pytest.raises(ServiceError, match="不是可解码图片"):
        asyncio.run(media.handle_keyframe(repository.require(queued["id"])))

    version = media.list_keyframes(current_shot["id"])[0]
    assert version["status"] == "failed"


def test_keyframe_job_with_staged_cleanup_failure_keeps_completed_media(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            image_prompt="主角站在画面中央",
        ).model_dump(),
    )

    class SuccessfulImageProvider:
        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(
                status="completed",
                result={"content_base64": base64.b64encode(_PNG).decode("ascii")},
            )

    original_delete_file = file_service.delete_file

    def fail_staged_cleanup(project_id: str, relative_path: str | Path) -> bool:
        if str(relative_path).replace("\\", "/").startswith("temp/provider-results/"):
            raise PermissionError("临时文件仍被占用")
        return original_delete_file(project_id, relative_path)

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            SuccessfulImageProvider(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )
    monkeypatch.setattr(file_service, "delete_file", fail_staged_cleanup)

    queued = media.queue_keyframe(current_shot["id"], {})
    result = asyncio.run(media.handle_keyframe(repository.require(queued["id"])))
    version = media.list_keyframes(current_shot["id"])[0]

    assert result["version_id"] == version["id"]
    assert version["status"] == "completed"
    assert file_service.resolve_project_path(project["id"], version["file_path"]).is_file()


def test_asset_image_completion_auto_binds_existing_shot_reference(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    asset = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林风", "prompt": "黑发少年剑修"},
    )
    shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(character_ids=[asset["id"]], image_prompt="林风站在山门前").model_dump(),
    )

    image = media.upload_asset_image(asset["id"], "character.png", "image/png", _PNG)

    assert ShotService(database, jobs).get(shot["id"])["reference_bindings"] == [
        {
            "role": "character_identity",
            "asset_id": asset["id"],
            "asset_image_id": image["id"],
        }
    ]


def test_keyframe_queue_with_referenced_asset_missing_image_returns_conflict(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林风",
            "prompt": "黑发少年剑修",
        },
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            image_prompt="林风站在山门石阶中央。",
        ).model_dump(),
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
            },
        ),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(current_shot["id"], {})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == (
        "以下分镜素材缺少已完成的当前图片：角色「林风」。请先在素材库生成或上传图片"
    )
    assert media.list_keyframes(current_shot["id"]) == []


def test_keyframe_queue_with_empty_current_asset_file_returns_conflict(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林风",
            "prompt": "黑发少年剑修",
        },
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    file_service.resolve_project_path(
        project["id"],
        character_image["file_path"],
    ).write_bytes(b"")
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            image_prompt="林风站在山门石阶中央。",
        ).model_dump(),
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
            },
        ),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(current_shot["id"], {})

    assert exc_info.value.status_code == 409
    assert media.list_keyframes(current_shot["id"]) == []


def test_keyframe_queue_with_missing_previous_keyframe_file_returns_conflict(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    previous_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(image_prompt="上一镜稳定末帧").model_dump(),
    )
    previous_keyframe = media.upload_keyframe(
        previous_shot["id"],
        "previous.png",
        "image/png",
        _PNG,
    )
    file_service.resolve_project_path(
        project["id"],
        previous_keyframe["file_path"],
    ).unlink()
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            image_prompt="当前镜头起始画面",
            reference_bindings=[
                {
                    "role": "previous_keyframe",
                    "keyframe_id": previous_keyframe["id"],
                }
            ],
        ).model_dump(),
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
            },
        ),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(current_shot["id"], {})

    assert exc_info.value.status_code == 409
    assert media.list_keyframes(current_shot["id"]) == []


def test_keyframe_queue_with_concurrent_manual_binding_preserves_binding(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林风", "prompt": "黑发少年剑修"},
    )
    prop = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "prop", "name": "月白玉佩", "prompt": "月白色玉佩"},
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    prop_image = media.upload_asset_image(
        prop["id"],
        "prop.png",
        "image/png",
        _PNG,
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            image_prompt="林风握住月白玉佩。",
        ).model_dump(),
    )
    ShotService(database, jobs).update(
        current_shot["id"],
        {"reference_bindings": []},
    )
    original_update = ShotService.update
    concurrent_update_injected = False

    def update_with_concurrent_binding(
        service: ShotService,
        shot_id: str,
        values: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal concurrent_update_injected
        if not concurrent_update_injected and "reference_bindings" in values:
            concurrent_update_injected = True
            original_update(
                service,
                shot_id,
                {
                    "reference_bindings": [
                        {
                            "role": "prop_identity",
                            "asset_id": prop["id"],
                            "asset_image_id": prop_image["id"],
                        }
                    ]
                },
            )
        return original_update(service, shot_id, values, **kwargs)

    monkeypatch.setattr(ShotService, "update", update_with_concurrent_binding)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_keyframe(current_shot["id"], {})
    refreshed = ShotService(database, jobs).get(current_shot["id"])

    assert {
        (binding["role"], binding.get("asset_image_id"))
        for binding in refreshed["reference_bindings"]
    } == {
        ("scene_layout", media_job_services["scene_image"]["id"]),
        ("character_identity", character_image["id"]),
        ("prop_identity", prop_image["id"]),
    }


def test_image_job_with_provider_resolve_failure_marks_version_failed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_asset = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "主角", "prompt": "主角立绘"},
    )
    runner.register_handler("generate_asset_image", media.handle_asset_image)
    _fail_second_provider_resolve(media, monkeypatch)

    queued = media.queue_asset_image(current_asset["id"], {})
    failed_job = _wait_for_job_status(repository, queued["id"], "failed")

    with database.engine.connect() as connection:
        target = connection.execute(
            select(asset_image.c.status, asset_image.c.error).where(
                asset_image.c.id == queued["target_id"]
            )
        ).one()
    assert failed_job["status"] == "failed"
    assert target.status == "failed"
    assert "secret-token" not in target.error
    assert "top-secret" not in target.error
    assert "[已隐藏" in target.error


def test_video_job_with_provider_resolve_failure_marks_version_failed(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            source_ref="manual-provider-failure-test",
            action="主角向前走",
            image_prompt="主角站在走廊",
            video_prompt="主角向前走",
        ).model_dump(),
    )
    media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    runner.register_handler("generate_video", media.handle_video)
    _fail_second_provider_resolve(media, monkeypatch)

    queued = media.queue_video(current_shot["id"], {})
    failed_job = _wait_for_job_status(repository, queued["id"], "failed")

    with database.engine.connect() as connection:
        target = connection.execute(
            select(video_version.c.status, video_version.c.error).where(
                video_version.c.id == queued["target_id"]
            )
        ).one()
    assert failed_job["status"] == "failed"
    assert target.status == "failed"
    assert "secret-token" not in target.error
    assert "top-secret" not in target.error
    assert "[已隐藏" in target.error


def test_video_job_prepares_current_keyframe_and_bound_reference_images(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
        },
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            source_ref="scene:1|古寺门口",
            character_ids=[character["id"]],
            action="林雪缓慢推开木门",
            image_prompt="林雪右手按住木门，尚未推开。",
            video_prompt="林雪缓慢推开木门，门缝透出暖光。",
        ).model_dump(),
    )
    media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    captured: dict[str, Any] = {}

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, paths: list[Path]) -> dict[str, Any]:
            captured["paths"] = paths
            return {"values": ["current-keyframe", "scene-reference", "character-reference"]}

        async def submit(self, **kwargs: Any) -> ProviderSubmission:
            captured["input_image"] = kwargs["input_image"]
            return ProviderSubmission(status="completed")

    async def fake_download(_provider: Any, _result: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-data")

    fake_provider = FakeVideoProvider()
    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            fake_provider,
            {
                "id": "mock-video",
                "model": "grok-imagine-1.5-video-apimart",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )
    monkeypatch.setattr(media, "_download_provider_result", fake_download)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 6.0)

    queued = media.queue_video(current_shot["id"], {})
    ShotService(database, jobs).update(current_shot["id"], {"reference_bindings": []})
    result = asyncio.run(media.handle_video(repository.require(queued["id"])))

    assert result["version_id"] == queued["target_id"]
    assert [path.read_bytes() for path in captured["paths"]] == [_PNG, _PNG, _PNG]
    assert captured["input_image"] == {
        "values": ["current-keyframe", "scene-reference", "character-reference"]
    }
    assert media.list_videos(current_shot["id"])[0]["is_stale"] is True


def test_video_queue_with_missing_current_keyframe_file_returns_conflict(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            action="林风转身",
            video_prompt="林风缓慢转身。",
        ).model_dump(),
    )
    current_keyframe = media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    file_service.resolve_project_path(
        project["id"],
        current_keyframe["file_path"],
    ).unlink()
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-video",
                "model": "single-image-video",
                "params": {},
            },
        ),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_video(current_shot["id"], {})

    assert exc_info.value.status_code == 409
    assert media.list_videos(current_shot["id"]) == []


def test_video_prompt_with_single_image_provider_omits_extra_reference_anchors(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            action="林雪缓慢推开木门",
            video_prompt="林雪缓慢推开木门。",
            reference_bindings=[
                {
                    "role": "character_identity",
                    "asset_id": character["id"],
                    "asset_image_id": character_image["id"],
                }
            ],
        ).model_dump(),
    )
    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {"id": "mock-video", "model": "single-image-video", "params": {}},
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_video(current_shot["id"], {})
    prompt_snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]

    assert "@Image1：当前分镜关键帧" in prompt_snapshot
    assert "@Image2" not in prompt_snapshot


def test_video_with_template_context_change_is_marked_stale(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="林雪缓慢推开木门",
            frame_description="林雪站在木门左侧",
            camera_angle="平视",
            video_prompt="林雪缓慢推开木门，门缝透出暖光。",
        ).model_dump(),
    )
    media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-video",
                "model": "mock-video-v1",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_video(current_shot["id"], {})
    assert media.list_videos(current_shot["id"])[0]["is_stale"] is False

    ShotService(database, jobs).update(current_shot["id"], {"camera_angle": "俯视"})

    assert media.list_videos(current_shot["id"])[0]["is_stale"] is True


def test_asset_with_template_fields_change_is_marked_stale(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    assets = AssetService(database, jobs)
    current_asset = assets.create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
            "composition": "全身主立绘居左",
            "consistency_keywords": ["左眉小痣"],
        },
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "mock-image-v1",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_asset_image(current_asset["id"], {})
    assert media.list_asset_images(current_asset["id"])[0]["is_stale"] is False

    assets.update(current_asset["id"], {"composition": "全身主立绘居中"})

    assert media.list_asset_images(current_asset["id"])[0]["is_stale"] is True


def test_keyframe_with_referenced_asset_rename_is_marked_stale(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
        },
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            image_prompt="林雪站在古寺门外。",
            reference_bindings=[
                {
                    "role": "character_identity",
                    "asset_id": character["id"],
                    "asset_image_id": character_image["id"],
                }
            ],
        ).model_dump(),
    )
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {
                "id": "mock-image",
                "model": "mock-image-v1",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_keyframe(current_shot["id"], {})
    assert media.list_keyframes(current_shot["id"])[0]["is_stale"] is False

    assets.update(character["id"], {"name": "林雪（雨夜造型）"})

    assert media.list_keyframes(current_shot["id"])[0]["is_stale"] is True


def test_video_with_binding_only_asset_rename_is_marked_stale(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
        },
    )
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    current_shot = ShotService(database, jobs).create(
        project["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="木门缓慢打开",
            video_prompt="0-2秒：木门缓慢打开。",
            reference_bindings=[
                {
                    "role": "character_identity",
                    "asset_id": character["id"],
                    "asset_image_id": character_image["id"],
                }
            ],
        ).model_dump(),
    )
    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)

    class MultiImageProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, _paths: list[Path]) -> dict[str, Any]:
            return {"values": ["current-keyframe", "character-reference"]}

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            MultiImageProvider(),
            {
                "id": "mock-video",
                "model": "grok-imagine-1.5-video-apimart",
                "params": {},
            },
        ),
    )

    media.queue_video(current_shot["id"], {})
    assert media.list_videos(current_shot["id"])[0]["is_stale"] is False

    assets.update(character["id"], {"name": "林雪（雨夜造型）"})

    assert media.list_videos(current_shot["id"])[0]["is_stale"] is True


def test_cancel_queued_job_returns_cancelled_and_second_cancel_conflicts(
    job_service: JobService,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="never_run",
    )
    cancelled = job_service.cancel(record["id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ServiceError) as exc_info:
        job_service.cancel(record["id"])
    assert exc_info.value.status_code == 409


def test_cancel_when_target_sync_fails_rolls_back_job_status(
    job_service: JobService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="never_run",
    )

    def fail_target_sync(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("同步目标失败")

    monkeypatch.setattr(job_service, "_sync_cancelled_target", fail_target_sync)

    with pytest.raises(RuntimeError, match="同步目标失败"):
        job_service.cancel(record["id"])

    persisted = job_service.repository.require(record["id"])
    assert persisted["status"] == "queued"


def test_cancel_queued_media_job_marks_target_version_failed(
    job_service: JobService,
) -> None:
    now = utc_now()
    asset_id = "asset-cancel"
    image_id = "asset-image-cancel"
    with job_service.repository.engine.begin() as connection:
        connection.execute(
            insert(asset).values(
                id=asset_id,
                project_id=job_service.project_id,  # type: ignore[attr-defined]
                asset_type="character",
                name="待取消媒体",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(asset_image).values(
                id=image_id,
                asset_id=asset_id,
                version_no=1,
                status="queued",
                created_at=now,
            )
        )
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="generate_asset_image",
        target_type="asset_image",
        target_id=image_id,
        provider_kind="image",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        target = connection.execute(
            select(asset_image.c.status, asset_image.c.error).where(asset_image.c.id == image_id)
        ).one()
    assert target.status == "failed"
    assert target.error == "关联任务已取消"


@pytest.mark.parametrize(
    ("table", "job_type", "target_type", "target_suffix"),
    [
        (novel_analysis, "analyze_novel", "analysis", ""),
        (screenplay, "generate_screenplay", "screenplay", ""),
        (screenplay, "generate_screenplay_scene", "screenplay_scene", ":1"),
    ],
)
def test_cancel_queued_writing_job_marks_generating_target_failed(
    job_service: JobService,
    table: object,
    job_type: str,
    target_type: str,
    target_suffix: str,
) -> None:
    project_id = job_service.project_id  # type: ignore[attr-defined]
    with job_service.repository.engine.begin() as connection:
        connection.execute(
            insert(table).values(
                project_id=project_id,
                status="generating",
                updated_at=utc_now(),
            )
        )
    record = job_service.repository.create_job(
        project_id=project_id,
        job_type=job_type,
        target_type=target_type,
        target_id=f"{project_id}{target_suffix}",
        provider_kind="text",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        target = connection.execute(
            select(table.c.status, table.c.error).where(table.c.project_id == project_id)
        ).one()
    assert target.status == "failed"
    assert target.error == "关联任务已取消"


def test_cancel_queued_screenplay_job_marks_hidden_analysis_failed(
    job_service: JobService,
) -> None:
    project_id = job_service.project_id  # type: ignore[attr-defined]
    with job_service.repository.engine.begin() as connection:
        for table in (novel_analysis, screenplay):
            connection.execute(
                insert(table).values(
                    project_id=project_id,
                    status="generating",
                    updated_at=utc_now(),
                )
            )
    record = job_service.repository.create_job(
        project_id=project_id,
        job_type="generate_screenplay",
        target_type="screenplay",
        target_id=project_id,
        provider_kind="text",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        analysis_target = connection.execute(
            select(novel_analysis.c.status, novel_analysis.c.error).where(
                novel_analysis.c.project_id == project_id
            )
        ).one()
    assert analysis_target.status == "failed"
    assert analysis_target.error == "关联任务已取消"


def test_retry_failed_job_requeues_same_id_and_increments_retry_count(
    job_service: JobService,
) -> None:
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="retry_me",
    )
    job_service.repository.fail(record["id"], "首次失败")
    retried = job_service.retry(record["id"])
    assert retried["id"] == record["id"]
    assert retried["retry_count"] == 1
    assert retried["status"] in {"queued", "failed"}
