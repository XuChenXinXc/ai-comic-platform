import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import pytest
from media_test_helpers import _PNG, _shot_create
from sqlalchemy import insert, select, update

from app.db import Database
from app.jobs.repository import JobRepository
from app.jobs.runner import JobRunner
from app.models.tables import (
    api_provider,
    asset,
    asset_image,
    keyframe_version,
    novel_analysis,
    project,
    screenplay,
    video_version,
)
from app.schemas import ProviderSubmission, ShotQualityFixOutput
from app.services.asset_service import AssetService
from app.services.chapter_service import ChapterService
from app.services.constants import MAX_IMAGE_SIZE, MAX_VIDEO_SIZE
from app.services.errors import ServiceError
from app.services.file_service import FileService, FileServiceError, file_service
from app.services.job_service import JobService
from app.services.media_service import JobCancelledError, MediaService
from app.services.project_service import ProjectService
from app.services.shot_service import ShotService
from app.services.utils import utc_now

_PROVIDER_RESOLVE_ERROR = (
    "Provider 配置失效：https://provider.invalid/task?token=secret-token\n"
    "Authorization: Bearer top-secret"
)


@pytest.fixture
def job_service(tmp_path: Path) -> Any:
    database = Database(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    database.initialize(seed_mock_providers=True)
    project = ProjectService(database, FileService(tmp_path)).create({"name": "任务测试"})
    chapter = ChapterService(database, FileService(tmp_path)).create(
        project["id"], {"title": "第一章"}
    )
    repository = JobRepository(database)
    runner = JobRunner(repository, {"text": 1, "image": 1, "video": 1})
    service = JobService(database, repository=repository, runner=runner)
    service.project_id = project["id"]  # type: ignore[attr-defined]
    service.chapter_id = chapter["id"]  # type: ignore[attr-defined]
    yield service
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
        ("character", "1536x1024"),
        ("scene", "1536x1024"),
        ("prop", "1536x1024"),
        ("keyframe", "2048x1152"),
    ],
)
def test_gpt_image_2_generation_params_use_stable_size_for_generic_provider(
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
            media_job_services["chapter"]["id"],
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


def test_free_create_asset_image_uses_raw_user_prompt(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project_record = media_job_services["project"]
    with database.engine.begin() as connection:
        connection.execute(
            update(project)
            .where(project.c.id == project_record["id"])
            .values(is_free_create=1)
        )
    current = AssetService(database, jobs).create(
        project_record["id"],
        {
            "asset_type": "character",
            "name": "自由制作角色",
            "prompt": "黑发少女，白色短裙",
        },
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
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_asset_image(current["id"], {})

    snapshot = media.list_asset_images(current["id"])[0]["prompt_snapshot"]
    assert snapshot == "黑发少女，白色短裙"


@pytest.mark.parametrize(
    ("target_type", "expected_size"),
    [
        ("character", "2048x1360"),
        ("scene", "2048x1152"),
        ("prop", "2048x1360"),
        ("keyframe", "2048x1152"),
    ],
)
def test_gpt_image_2_generation_params_use_2k_size_for_apib(
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
                "base_url": "https://api.apib.ai/v1",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    if target_type == "keyframe":
        current = ShotService(database, jobs).create(
            media_job_services["chapter"]["id"],
            _shot_create(
                scene_asset_id=media_job_services["scene"]["id"],
                action="主角走入画面",
                image_prompt="主角站在画面中央",
            ).model_dump(),
        )
        queued = media.queue_keyframe(current["id"], {})
    else:
        current = AssetService(database, jobs).create(
            project["id"],
            {
                "asset_type": target_type,
                "name": f"{target_type}测试素材",
                "prompt": "设定图",
            },
        )
        queued = media.queue_asset_image(current["id"], {})

    assert queued["input_data"]["params"]["size"] == expected_size


@pytest.mark.parametrize(
    ("target_type", "expected_size"),
    [
        ("character", "2048x1360"),
        ("scene", "2048x1152"),
        ("prop", "2048x1360"),
        ("keyframe", "2048x1152"),
    ],
)
def test_gpt_image_2_generation_params_use_2k_size_for_tianyue(
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
                "base_url": "https://api.tianyue.xyz/v1",
                "params": {},
            },
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    if target_type == "keyframe":
        current = ShotService(database, jobs).create(
            media_job_services["chapter"]["id"],
            _shot_create(
                scene_asset_id=media_job_services["scene"]["id"],
                action="主角走入画面",
                image_prompt="主角站在画面中央",
            ).model_dump(),
        )
        queued = media.queue_keyframe(current["id"], {})
    else:
        current = AssetService(database, jobs).create(
            project["id"],
            {
                "asset_type": target_type,
                "name": f"{target_type}测试素材",
                "prompt": "设定图",
            },
        )
        queued = media.queue_asset_image(current["id"], {})

    assert queued["input_data"]["params"]["size"] == expected_size


def test_asset_image_only_missing_skips_queued_media_version(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    runner = media_job_services["runner"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    asset = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "排队角色", "prompt": "黑发少年"},
    )

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    media.queue_asset_image(asset["id"], {})

    batch = media.queue_asset_image_batch(project["id"], {"only_missing": True})

    assert batch["jobs"] == []
    assert batch["preflight_failures"] == []


def test_keyframe_only_missing_skips_queued_media_version(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    runner = media_job_services["runner"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角抬头",
            image_prompt="主角站在旧屋门口抬头。",
        ).model_dump(),
    )

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    media.queue_keyframe(shot["id"], {})

    batch = media.queue_keyframe_batch(project["id"], {"only_missing": True})

    assert batch["jobs"] == []
    assert batch["preflight_failures"] == []


def test_video_only_missing_skips_queued_media_version(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    runner = media_job_services["runner"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角向前走",
            image_prompt="主角站在旧屋门口。",
            video_prompt="0-6秒：主角向前走到门边。",
        ).model_dump(),
    )
    media.upload_keyframe(shot["id"], "keyframe.png", "image/png", _PNG)

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    media.queue_video(shot["id"], {})

    batch = media.queue_video_batch(project["id"], {"only_missing": True})

    assert batch["jobs"] == []
    assert batch["preflight_failures"] == []

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
    media_job_services["project"]
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    chapter = media_job_services["chapter"]
    shots = ShotService(database, jobs)
    current = shots.create(
        chapter["id"],
        _shot_create(action="主角推开房门").model_dump(),
    )
    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)

    batch = shots.queue_prompt_batch(
        chapter["id"],
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


def test_manual_shot_create_persists_prompt_overrides(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    shots = ShotService(database, jobs)

    current = shots.create(
        media_job_services["chapter"]["id"],
        {
            "source_ref": "manual",
            "duration_sec": 6,
            "image_prompt_override": "自定义分镜图提示词",
            "video_prompt_override": "自定义视频提示词",
        },
    )

    assert current["source_ref"] == "manual"
    assert current["image_prompt_override"] == "自定义分镜图提示词"
    assert current["video_prompt_override"] == "自定义视频提示词"
    assert current["duration_sec"] == 6


def test_keyframe_queue_uses_saved_final_prompt_override_verbatim(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    media_job_services["project"]
    override = "用户编辑后的最终关键帧提示词\n保持原样，不再套用模板。"
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    override = "用户编辑后的最终视频提示词\n严格按此文本执行。"
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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


def test_manual_shot_keyframe_override_keeps_user_prompt_without_auto_anchors(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "主角站在山门前，晨光斜照。"
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
        },
    )
    media.upload_asset_image(character["id"], "character.png", "image/png", _PNG)
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            "source_ref": "manual",
            "duration_sec": 6,
            "scene_asset_id": media_job_services["scene"]["id"],
            "character_ids": [character["id"]],
            "image_prompt_override": override,
        },
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_keyframe(current["id"], {})

    snapshot = media.list_keyframes(current["id"])[0]["prompt_snapshot"]
    assert snapshot == override
    assert "@Image" not in snapshot


def test_manual_shot_video_override_keeps_user_prompt_without_auto_anchors(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    override = "镜头从山门缓缓推进到主角近景。"
    character = AssetService(database, jobs).create(
        project["id"],
        {
            "asset_type": "character",
            "name": "林雪",
            "prompt": "黑发女符师",
        },
    )
    media.upload_asset_image(character["id"], "character.png", "image/png", _PNG)
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            "source_ref": "manual",
            "duration_sec": 6,
            "scene_asset_id": media_job_services["scene"]["id"],
            "character_ids": [character["id"]],
            "video_prompt_override": override,
        },
    )
    media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, _paths: list[Path]) -> dict[str, Any]:
            return {"values": ["keyframe", "scene", "character"]}

        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(status="completed")

    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeVideoProvider(),
            {"id": "mock-video", "model": "multi", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    media.queue_video(current["id"], {})

    snapshot = media.list_videos(current["id"])[0]["prompt_snapshot"]
    assert snapshot == override
    assert "@Image" not in snapshot


def test_uploaded_keyframe_uses_saved_final_prompt_override(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    media_job_services["project"]
    override = "上传关键帧使用的最终提示词"
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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


def test_upload_asset_image_over_size_limit_rejects(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "超大图", "prompt": "测试"},
    )

    with pytest.raises(ServiceError) as exc_info:
        media.upload_asset_image(
            current["id"],
            "big.png",
            "image/png",
            b"\x00" * (MAX_IMAGE_SIZE + 1),
        )

    assert exc_info.value.status_code == 400


def test_media_stores_only_usable_provider_url(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "URL素材", "prompt": "测试"},
    )
    image = media.upload_asset_image(
        current["id"],
        "url.png",
        "image/png",
        _PNG,
    )
    good_url = f"https://cdn.example/good.png?Expires={int(time.time()) + 3600}"

    media._store_provider_url(asset_image, image["id"], good_url)
    media._store_provider_url(
        asset_image,
        image["id"],
        f"https://cdn.example/expired.png?Expires={int(time.time()) - 1}",
    )

    assert media._get_version("asset_image", image["id"])["provider_url"] == good_url


def test_upload_video_over_size_limit_rejects(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="测试动作",
        ).model_dump(),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.upload_video(
            current_shot["id"],
            "big.mp4",
            "video/mp4",
            b"\x00" * (MAX_VIDEO_SIZE + 1),
        )

    assert exc_info.value.status_code == 400


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
    media_job_services["project"]
    override = "上传视频使用的最终提示词"
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "测试角色", "prompt": "设定图"},
    )
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            **_shot_create(image_prompt="主角站在画面中央").model_dump(),
            "character_ids": [character["id"]],
        },
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
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "测试角色", "prompt": "设定图"},
    )
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            **_shot_create(video_prompt="主角缓慢转身并停稳").model_dump(),
            "character_ids": [character["id"]],
        },
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


def test_manual_keyframe_queue_without_asset_bindings_succeeds(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            "source_ref": "manual",
            "duration_sec": 6,
            "image_prompt_override": "自定义分镜图提示词",
        },
    )

    media.queue_keyframe(current["id"], {})

    versions = media.list_keyframes(current["id"])
    assert len(versions) == 1
    assert versions[0]["prompt_snapshot"] == "自定义分镜图提示词"


def test_manual_video_queue_without_asset_bindings_succeeds(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    current = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        {
            "source_ref": "manual",
            "duration_sec": 6,
            "video_prompt_override": "自定义视频提示词",
        },
    )
    media.upload_keyframe(current["id"], "frame.png", "image/png", _PNG)

    media.queue_video(current["id"], {})

    assert len(media.list_videos(current["id"])) == 1


def test_grok_video_queue_rounds_fractional_duration_up_consistently(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角转身",
            video_prompt="主角缓慢转身并停稳。",
            duration_sec=5.5,
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
        media_job_services["chapter"]["id"],
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


def test_keyframe_queued_with_missing_asset_image_file_reports_error(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """素材图文件缺失时生成关键帧必须明确报错，不能静默降级为纯文字。

    注：该用例命中入队阶段的既有校验（_resolve_shot_reference_bindings），
    作为回归保护；新增的执行阶段校验见
    test_keyframe_execution_with_reference_files_deleted_reports_error。
    """
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
    media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    # 模拟素材图文件丢失：删除磁盘文件但保留 DB 记录
    project_dir = file_service.projects_dir / "媒体任务测试"
    for path in [path for path in project_dir.rglob("*") if path.is_file()]:
        path.unlink()
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            action="林风站在山门石阶上",
            image_prompt="林风站在山门石阶中央。",
        ).model_dump(),
    )
    with pytest.raises(ServiceError) as excinfo:
        media.queue_keyframe(current_shot["id"], {})
    assert excinfo.value.status_code == 409
    assert "林风" in str(excinfo.value)


def test_keyframe_execution_with_reference_files_deleted_reports_error(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键帧任务执行前参考图文件被删除：必须报错，不能静默降级为纯文字。"""
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="主角走入画面",
            image_prompt="主角站在画面中央",
        ).model_dump(),
    )

    class NeverCalledProvider:
        async def submit(self, **kwargs: Any) -> ProviderSubmission:
            raise AssertionError("参考图缺失时不应调用 Provider")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            NeverCalledProvider(),
            {
                "id": "mock-image",
                "model": "gpt-image-2",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    queued = media.queue_keyframe(current_shot["id"], {})
    # 任务入队后、执行前，素材图文件被删除
    project_dir = file_service.projects_dir / "媒体任务测试"
    for path in [path for path in project_dir.rglob("*") if path.is_file()]:
        path.unlink()

    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(media.handle_keyframe(repository.require(queued["id"])))
    assert excinfo.value.status_code == 409
    assert "参考图文件缺失" in str(excinfo.value)


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
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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


def test_video_job_with_previous_end_frame_keeps_keyframe_first_and_anchor_order(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上一镜末帧参与时，@Image 编号必须与实际下发的图片顺序逐一对应。

    关键帧必须排首位：单图 Provider 只取 values[0]，若被末帧顶掉，
    视频会从上一镜画面开始生成。
    """
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    scene_id = media_job_services["scene"]["id"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="林雪走到门前").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=scene_id,
            action="林雪推开木门",
            video_prompt="林雪缓慢推开木门。",
            transition_from_prev="continuous",
        ).model_dump(),
    )

    # 上一镜已生成视频并留下末帧（直接落库，避免依赖真实 ffprobe 解码）
    end_frame_relative = "分镜视频/prev-end.jpg"
    file_service.write_bytes_atomic(
        media_job_services["project"]["id"],
        end_frame_relative,
        _PNG,
    )
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-end",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    captured: dict[str, Any] = {}

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, paths: list[Path]) -> dict[str, Any]:
            captured["paths"] = list(paths)
            return {"values": [f"image-{index}" for index in range(len(paths))]}

        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(status="completed")

    async def fake_download(_provider: Any, _result: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-data")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeVideoProvider(),
            {"id": "mock-video", "model": "multi", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(media, "_download_provider_result", fake_download)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 6.0)
    monkeypatch.setattr(media, "_extract_last_frame", lambda _src, _dst: None)

    queued = media.queue_video(current_shot["id"], {})
    prompt = repository.require(queued["id"])
    asyncio.run(media.handle_video(prompt))

    keyframe_path = file_service.resolve_project_path(
        media_job_services["project"]["id"],
        media.list_keyframes(current_shot["id"])[0]["file_path"],
    )
    prev_frame_path = file_service.resolve_project_path(
        media_job_services["project"]["id"],
        end_frame_relative,
    )

    # 关键帧第一、末帧第二，与 prompt 中的 @Image1/@Image2 声明一致
    assert captured["paths"][0] == keyframe_path
    assert captured["paths"][1] == prev_frame_path
    snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]
    assert "@Image1：当前分镜关键帧" in snapshot
    assert "@Image2：上一镜结束帧" in snapshot


def test_video_job_manual_continuous_puts_previous_end_frame_first(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自由制作分镜从第二镜起默认连续：上一镜末帧作为第 0 秒起始帧。"""
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    scene_id = media_job_services["scene"]["id"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="林雪走到门前").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=scene_id,
            action="林雪推开木门",
            video_prompt="林雪缓慢推开木门。",
            transition_from_prev="continuous",
            source_ref="manual",
        ).model_dump(),
    )

    end_frame_relative = "分镜视频/prev-manual-end.jpg"
    file_service.write_bytes_atomic(
        media_job_services["project"]["id"],
        end_frame_relative,
        _PNG,
    )
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-manual-end",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev-manual.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    captured: dict[str, Any] = {}

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, paths: list[Path]) -> dict[str, Any]:
            captured["paths"] = list(paths)
            return {"values": [f"image-{index}" for index in range(len(paths))]}

        async def submit(self, **_kwargs: Any) -> ProviderSubmission:
            return ProviderSubmission(status="completed")

    async def fake_download(_provider: Any, _result: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-data")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeVideoProvider(),
            {"id": "mock-video", "model": "multi", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(media, "_download_provider_result", fake_download)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 6.0)
    monkeypatch.setattr(media, "_extract_last_frame", lambda _src, _dst: None)

    queued = media.queue_video(current_shot["id"], {})
    job = repository.require(queued["id"])
    asyncio.run(media.handle_video(job))

    keyframe_path = file_service.resolve_project_path(
        media_job_services["project"]["id"],
        media.list_keyframes(current_shot["id"])[0]["file_path"],
    )
    prev_frame_path = file_service.resolve_project_path(
        media_job_services["project"]["id"],
        end_frame_relative,
    )

    assert captured["paths"][0] == prev_frame_path
    assert captured["paths"][1] == keyframe_path
    snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]
    assert "@Image1：上一镜结束帧" in snapshot
    assert "@Image2：当前分镜关键帧" in snapshot


def test_manual_continuous_preview_matches_actual_queue_video_prompt(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预览编译与实际入队的视频提示词必须一致：@Image 顺序与有效时长相同。"""
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    scene_id = media_job_services["scene"]["id"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, paths: list[Path]) -> dict[str, Any]:
            return {"values": [f"image-{index}" for index in range(len(paths))]}

    fake_config = {
        "id": "mock-video",
        "model": "grok-imagine-1.5-video-apimart",
        "params": {},
        "timeout_sec": 60,
    }

    def fake_resolve(**_kwargs: Any) -> tuple[FakeVideoProvider, dict[str, Any]]:
        return FakeVideoProvider(), dict(fake_config)

    # 必须在任何分镜创建/读取之前打桩：create() 内部会编译提示词并缓存默认型号，
    # 预览与入队要保证解析到同一份 provider 配置。
    monkeypatch.setattr(media.providers, "resolve", fake_resolve)
    monkeypatch.setattr(shots.providers, "resolve", fake_resolve)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="林雪走到门前").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=scene_id,
            action="林雪推开木门",
            video_prompt="0-3秒：林雪推门；3-5秒：门扇完全打开。",
            transition_from_prev="continuous",
            source_ref="manual",
            duration_sec=5,
        ).model_dump(),
    )

    end_frame_relative = "分镜视频/prev-preview-end.jpg"
    file_service.write_bytes_atomic(
        media_job_services["project"]["id"],
        end_frame_relative,
        _PNG,
    )
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-preview-end",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev-preview.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="grok-imagine-1.5-video-apimart",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=utc_now(),
            )
        )
    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)

    queued = media.queue_video(current_shot["id"], {})
    repository = media_job_services["repository"]
    assert repository.require(queued["id"]) is not None
    preview = shots.get(current_shot["id"])["compiled_video_prompt"]
    snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]

    assert "@Image1：上一镜结束帧" in preview
    assert "@Image2：当前分镜关键帧" in preview
    # 整数秒模型最短 6 秒：预览与实际提交都必须按 6 秒编译时间轴
    assert "总时长：6秒" in preview
    assert "总时长：5秒" not in preview
    assert "总时长：6秒" in snapshot
    assert preview == snapshot


def test_tianyue_video_execution_prefers_provider_urls_and_falls_back_for_end_frame(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    chapter_id = media_job_services["chapter"]["id"]
    scene_id = media_job_services["scene"]["id"]
    shots = ShotService(database, jobs)

    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "测试角色", "prompt": "角色设定"},
    )
    prop = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "prop", "name": "测试物品", "prompt": "物品设定"},
    )
    scene_image = media_job_services["scene_image"]
    character_image = media.upload_asset_image(
        character["id"],
        "character.png",
        "image/png",
        _PNG,
    )
    prop_image = media.upload_asset_image(prop["id"], "prop.png", "image/png", _PNG)
    urls = {
        scene_image["id"]: "https://cdn.example/scene.png",
        character_image["id"]: "https://cdn.example/character.png",
        prop_image["id"]: "https://cdn.example/prop.png",
    }
    with database.engine.begin() as connection:
        for version_id, url in urls.items():
            connection.execute(
                update(asset_image)
                .where(asset_image.c.id == version_id)
                .values(provider_url=url)
            )

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="上一镜动作").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        {
            **_shot_create(
                scene_asset_id=scene_id,
                action="本镜动作",
                video_prompt="保持画面稳定。",
                transition_from_prev="continuous",
            ).model_dump(),
            "source_ref": "manual",
            "duration_sec": 15,
            "character_ids": [character["id"]],
            "prop_ids": [prop["id"]],
        },
    )

    end_frame_relative = "分镜视频/tianyue-prev-end.jpg"
    file_service.write_bytes_atomic(project["id"], end_frame_relative, _PNG)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-tianyue-prev",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/tianyue-prev.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    keyframe = media.upload_keyframe(
        current_shot["id"],
        "frame.png",
        "image/png",
        _PNG,
    )
    keyframe_url = "https://cdn.example/keyframe.png"
    with database.engine.begin() as connection:
        connection.execute(
            update(keyframe_version)
            .where(keyframe_version.c.id == keyframe["id"])
            .values(provider_url=keyframe_url)
        )

    captured: dict[str, Any] = {"fallbacks": []}

    class FakeTianyueVideoProvider:
        supports_multiple_input_images = True
        prefers_public_reference_urls = True

        async def prepare_input_image(self, path: Path) -> dict[str, Any]:
            captured["fallbacks"].append(path.name)
            return {"value": f"b64:{path.name}", "mode": "base64"}

        async def prepare_input_images(
            self,
            paths: list[Path],
        ) -> dict[str, Any]:
            return {
                "values": [f"b64:{path.name}" for path in paths],
                "mode": "base64",
            }

        async def submit(self, **kwargs: Any) -> ProviderSubmission:
            captured["submit"] = kwargs
            return ProviderSubmission(status="completed")

    async def fake_download(_provider: Any, _result: Any, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video-data")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeTianyueVideoProvider(),
            {"id": "mock-video", "model": "multi", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(media, "_download_provider_result", fake_download)
    monkeypatch.setattr(media, "_verify_video", lambda _path: 15.0)
    monkeypatch.setattr(media, "_extract_last_frame", lambda _src, _dst: None)

    queued = media.queue_video(current_shot["id"], {})
    job = repository.require(queued["id"])
    asyncio.run(media.handle_video(job))

    values = captured["submit"]["input_image"]["values"]
    prev_frame_name = Path(end_frame_relative).name
    assert values[0] == f"b64:{prev_frame_name}"
    assert values[1] == keyframe_url
    assert values[2] == urls[scene_image["id"]]
    assert values[3] == urls[character_image["id"]]
    assert values[4] == urls[prop_image["id"]]
    assert captured["fallbacks"] == [prev_frame_name]
    assert captured["submit"]["duration_sec"] == 15.0


def test_video_queue_across_scene_change_omits_previous_end_frame(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨场景时不引用上一镜末帧：图片暗示比文字更强，会把上一场景混进本镜画面。"""
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    other_scene = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "scene", "name": "另一个场景", "prompt": "另一处场景设定"},
    )
    media.upload_asset_image(other_scene["id"], "scene2.png", "image/png", _PNG)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="林雪离开旧屋",
        ).model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=other_scene["id"],
            action="林雪走进新场景",
            video_prompt="林雪走进新场景。",
            transition_from_prev="scene_change",
        ).model_dump(),
    )

    end_frame_relative = "分镜视频/prev-cross-end.jpg"
    file_service.write_bytes_atomic(project["id"], end_frame_relative, _PNG)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-cross",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev-cross.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {"id": "mock-video", "model": "single", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    queued = media.queue_video(current_shot["id"], {})

    assert queued["input_data"]["prev_end_frame_path"] is None
    snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]
    assert "上一镜结束帧" not in snapshot
    assert "@Image1：当前分镜关键帧" in snapshot


def test_video_queue_same_scene_cut_omits_previous_end_frame(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同场景但属于独立机位（对切/景别切换）时不引用末帧。

    「同场景」不等于「动作连续」：对切、反打、时间跳跃都发生在同一场景内，
    此时把上一镜画面当参考会让模型复制错误的构图。
    """
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    scene_id = media_job_services["scene"]["id"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="陈鑫中景说话").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=scene_id,
            action="张桂芬特写反应",
            video_prompt="张桂芬皱眉。",
            transition_from_prev="same_scene_cut",
        ).model_dump(),
    )

    end_frame_relative = "分镜视频/prev-cut-end.jpg"
    file_service.write_bytes_atomic(project["id"], end_frame_relative, _PNG)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-cut",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev-cut.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            object(),
            {"id": "mock-video", "model": "single", "params": {}, "timeout_sec": 60},
        ),
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    queued = media.queue_video(current_shot["id"], {})

    # 末帧文件确实存在且同场景，但衔接类型不是 continuous，因此不引用
    assert queued["input_data"]["prev_end_frame_path"] is None
    snapshot = media.list_videos(current_shot["id"])[0]["prompt_snapshot"]
    assert "@Image2：上一镜结束帧" not in snapshot
    assert "独立机位" in snapshot


def test_video_execution_with_deleted_previous_end_frame_reports_error(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prompt 已声明末帧但文件在执行前消失时必须失败，避免 @Image 编号整体错位。"""
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    scene_id = media_job_services["scene"]["id"]
    chapter_id = media_job_services["chapter"]["id"]
    shots = ShotService(database, jobs)

    previous_shot = shots.create(
        chapter_id,
        _shot_create(scene_asset_id=scene_id, action="林雪走到门前").model_dump(),
    )
    current_shot = shots.create(
        chapter_id,
        _shot_create(
            scene_asset_id=scene_id,
            action="林雪推开木门",
            video_prompt="林雪缓慢推开木门。",
            transition_from_prev="continuous",
        ).model_dump(),
    )

    end_frame_relative = "分镜视频/prev-vanish-end.jpg"
    file_service.write_bytes_atomic(project["id"], end_frame_relative, _PNG)
    now = utc_now()
    with database.engine.begin() as connection:
        connection.execute(
            insert(video_version).values(
                id="video-prev-vanish",
                shot_id=previous_shot["id"],
                version_no=1,
                file_path="分镜视频/prev-vanish.mp4",
                end_frame_path=end_frame_relative,
                prompt_snapshot="",
                negative_prompt_snapshot="",
                requested_duration_sec=6,
                model="multi",
                provider_task_id="",
                status="completed",
                is_current=True,
                input_signature="",
                source="generated",
                error="",
                created_at=now,
            )
        )

    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)

    class FakeVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, _paths: list[Path]) -> dict[str, Any]:
            raise AssertionError("末帧缺失时不应调用 prepare_input_images")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            FakeVideoProvider(),
            {"id": "mock-video", "model": "multi", "params": {}, "timeout_sec": 60},
        ),
    )

    queued = media.queue_video(current_shot["id"], {})
    # prompt 快照完成后文件被删：编号已固化，不能静默降级
    file_service.delete_file(project["id"], end_frame_relative)

    with pytest.raises(ServiceError) as exc_info:
        asyncio.run(media.handle_video(repository.require(queued["id"])))

    assert exc_info.value.status_code == 409
    assert "上一镜结束帧" in exc_info.value.detail


def test_video_queue_with_missing_current_keyframe_file_returns_conflict(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
    media_job_services["project"]
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
        media_job_services["chapter"]["id"],
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
    chapter_id = job_service.chapter_id  # type: ignore[attr-defined]
    with job_service.repository.engine.begin() as connection:
        connection.execute(
            insert(table).values(
                chapter_id=chapter_id,
                status="generating",
                updated_at=utc_now(),
            )
        )
    record = job_service.repository.create_job(
        project_id=project_id,
        chapter_id=chapter_id,
        job_type=job_type,
        target_type=target_type,
        target_id=f"{chapter_id}{target_suffix}",
        provider_kind="text",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        target = connection.execute(
            select(table.c.status, table.c.error).where(table.c.chapter_id == chapter_id)
        ).one()
    assert target.status == "failed"
    assert target.error == "关联任务已取消"


def test_cancel_queued_screenplay_job_marks_hidden_analysis_failed(
    job_service: JobService,
) -> None:
    project_id = job_service.project_id  # type: ignore[attr-defined]
    chapter_id = job_service.chapter_id  # type: ignore[attr-defined]
    with job_service.repository.engine.begin() as connection:
        for table in (novel_analysis, screenplay):
            connection.execute(
                insert(table).values(
                    chapter_id=chapter_id,
                    status="generating",
                    updated_at=utc_now(),
                )
            )
    record = job_service.repository.create_job(
        project_id=project_id,
        chapter_id=chapter_id,
        job_type="generate_screenplay",
        target_type="screenplay",
        target_id=chapter_id,
        provider_kind="text",
    )

    job_service.cancel(record["id"])

    with job_service.repository.engine.connect() as connection:
        analysis_target = connection.execute(
            select(novel_analysis.c.status, novel_analysis.c.error).where(
                novel_analysis.c.chapter_id == chapter_id
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


def test_asset_image_queue_uses_prompt_override_verbatim(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """素材图提示词覆盖值：生成时直接使用覆盖值，不再按模板编译。"""
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    service = AssetService(database, jobs)
    current = service.create(
        project["id"],
        {"asset_type": "character", "name": "林风", "prompt": "黑发少年剑修"},
    )
    service.update(
        current["id"],
        {"prompt_override": "自定义白底角色提示词：林风全身立绘，纯白背景。"},
    )
    monkeypatch.setattr(jobs, "create", lambda **kwargs: kwargs)

    queued = media.queue_asset_image(current["id"], {})

    version = media.list_asset_images(current["id"])[0]
    assert version["prompt_snapshot"] == "自定义白底角色提示词：林风全身立绘，纯白背景。"
    assert queued["target_type"] == "asset_image"
    # 非空覆盖值纳入签名，与分镜 override 语义一致
    assert version["input_signature"] != ""
    override_signature = version["input_signature"]
    service.update(current["id"], {"prompt_override": "另一个自定义提示词"})
    media.queue_asset_image(current["id"], {})
    version_after = media.list_asset_images(current["id"])[0]
    assert version_after["input_signature"] != override_signature


def test_video_execution_with_reference_files_deleted_reports_error(
    media_job_services: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视频任务执行前参考图文件被删除：必须报错，不能静默跳过参考图。"""
    database = media_job_services["database"]
    repository = media_job_services["repository"]
    runner = media_job_services["runner"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林风", "prompt": "黑发少年剑修"},
    )
    media.upload_asset_image(character["id"], "character.png", "image/png", _PNG)
    current_shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            action="主角走入画面",
            image_prompt="主角站在画面中央",
            video_prompt="主角从画面左侧走入，镜头缓缓跟随。",
        ).model_dump(),
    )
    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)

    class NeverCalledVideoProvider:
        supports_multiple_input_images = True

        async def prepare_input_images(self, _paths: list[Path]) -> dict[str, Any]:
            raise AssertionError("参考图缺失时不应调用 prepare_input_images")

        async def submit(self, **kwargs: Any) -> ProviderSubmission:
            raise AssertionError("参考图缺失时不应调用 Provider")

    monkeypatch.setattr(runner, "enqueue", lambda _job_id: False)
    monkeypatch.setattr(
        media.providers,
        "resolve",
        lambda **_kwargs: (
            NeverCalledVideoProvider(),
            {
                "id": "mock-video",
                "model": "grok-imagine-1.5-video-apimart",
                "params": {},
                "timeout_sec": 60,
            },
        ),
    )

    queued = media.queue_video(current_shot["id"], {})
    # 任务入队后、执行前，素材图文件被删除
    project_dir = file_service.projects_dir / "媒体任务测试"
    for path in [path for path in project_dir.rglob("*") if path.is_file()]:
        path.unlink()

    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(media.handle_video(repository.require(queued["id"])))
    assert excinfo.value.status_code == 409
    assert "视频参考图文件缺失" in str(excinfo.value)


def test_media_poll_aborts_when_job_is_cancelled(
    media_job_services: dict[str, Any],
) -> None:
    repository = media_job_services["repository"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    record = repository.create_job(
        project_id=project["id"],
        job_type="generate_keyframe",
    )
    repository.cancel(record["id"])

    with pytest.raises(JobCancelledError, match="任务已取消"):
        media._raise_if_job_cancelled(record["id"])


def test_quality_fix_does_not_write_after_job_is_cancelled(
    job_service: JobService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot_service = ShotService.__new__(ShotService)
    shot_service.jobs = job_service
    current = {
        "id": "shot-1",
        "revision": 1,
        "shot_no": 1,
        "duration_sec": 6,
        "action": "主角站起",
        "start_state": "主角坐着",
        "end_state": "主角站起",
        "video_prompt": "主角保持坐姿",
        "video_prompt_override": "",
        "dialogue": [],
    }
    updates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class CancellingProvider:
        async def generate_json(self, **_kwargs: Any) -> ShotQualityFixOutput:
            job_service.repository.update(
                record["id"],
                status="cancelled",
                message="任务已取消",
                finished_at=utc_now(),
            )
            return ShotQualityFixOutput(
                video_prompt="修复后的提示词",
                changed_fields=["video_prompt"],
                reason="测试结果",
            )

    class FakeProviders:
        def resolve(self, **_kwargs: Any) -> tuple[CancellingProvider, dict[str, Any]]:
            return CancellingProvider(), {"params": {}}

    monkeypatch.setattr(
        shot_service,
        "_project",
        lambda _project_id: {"default_text_provider_id": "mock-text"},
    )
    monkeypatch.setattr(shot_service, "get", lambda _shot_id: current)
    monkeypatch.setattr(shot_service, "_shot_quality_warnings", lambda _current: ["需要修复"])
    monkeypatch.setattr(
        shot_service,
        "update",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    shot_service.providers = FakeProviders()
    record = job_service.repository.create_job(
        project_id=job_service.project_id,  # type: ignore[attr-defined]
        job_type="fix_shot_quality",
        target_type="project",
        target_id=job_service.project_id,  # type: ignore[attr-defined]
        provider_kind="text",
        input_data={"ids": ["shot-1"], "params": {}},
    )
    job_service.repository.update(record["id"], status="running")

    asyncio.run(shot_service.handle_quality_fix(record))

    assert updates == []
