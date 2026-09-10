"""一键全链编排（run_shot_pipeline）三步链式任务回归测试。

覆盖：
- 第一步补齐缺失提示词/导演计划并链式触发媒体步骤；
- 第二步补齐干净参考图、批量生成关键帧并链式触发视频步骤；
- 第三步在关键帧完成后批量生成视频；
- 已有当前关键帧/视频的分镜跳过，不重复生成。
"""

import asyncio
from typing import Any

from media_test_helpers import _shot_create
from sqlalchemy import update

from app.models.tables import asset_image
from app.services.shot_service import ShotService


def _mark_scene_image_clean(media_job_services: dict[str, Any]) -> None:
    database = media_job_services["database"]
    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == media_job_services["scene_image"]["id"])
            .values(reference_purpose="clean_reference", status="completed")
        )


def _create_two_shots(media_job_services: dict[str, Any]) -> ShotService:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    shots = ShotService(database, jobs)
    for index in range(2):
        shots.create(
            chapter["id"],
            _shot_create(
                scene_asset_id=scene["id"],
                action=f"镜头{index + 1}动作",
                image_prompt=f"镜头{index + 1}人物站在大殿。",
                video_prompt=f"镜头{index + 1}人物向前走。",
            ).model_dump(),
        )
    return shots


def test_pipeline_step_one_queues_missing_prompts_and_chains_media(
    media_job_services: dict[str, Any],
) -> None:
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    chapter = media_job_services["chapter"]
    _mark_scene_image_clean(media_job_services)
    # 不带 image/video_prompt 的分镜：第一步应补齐提示词与导演计划
    ShotService(media_job_services["database"], media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            action="人物向前走",
        ).model_dump(),
    )

    queued = media.queue_shot_pipeline(chapter["id"], {})
    result = asyncio.run(
        media.handle_shot_pipeline(repository.require(queued["id"]))
    )

    assert len(result["prompt_jobs"]) == 3  # image + video + director plan
    assert result["failures"] == []
    media_step = repository.require(result["media_job"])
    assert media_step["job_type"] == "run_shot_pipeline_media"
    assert set(media_step["depends_on"]) == set(result["prompt_jobs"])


def test_pipeline_chain_queues_keyframes_then_videos(
    media_job_services: dict[str, Any],
) -> None:
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    chapter = media_job_services["chapter"]
    _mark_scene_image_clean(media_job_services)
    _create_two_shots(media_job_services)

    queued = media.queue_shot_pipeline(chapter["id"], {})
    step1 = asyncio.run(
        media.handle_shot_pipeline(repository.require(queued["id"]))
    )
    # 图/视频提示词已齐，只补齐两个分镜的导演计划
    assert len(step1["prompt_jobs"]) == 2
    assert step1["failures"] == []
    step2 = asyncio.run(
        media.handle_shot_pipeline_media(
            repository.require(step1["media_job"])
        )
    )
    assert len(step2["keyframe_jobs"]) == 2
    assert step2["failures"] == []

    # 关键帧任务执行完成后，第三步入队视频
    for keyframe_job_id in step2["keyframe_jobs"]:
        asyncio.run(
            media.handle_keyframe(repository.require(keyframe_job_id))
        )
    step3 = asyncio.run(
        media.handle_shot_pipeline_videos(
            repository.require(step2["video_job"])
        )
    )
    assert len(step3["video_jobs"]) == 2
    assert step3["failures"] == []
    # 第三步任务依赖所有关键帧任务
    videos_step = repository.require(step2["video_job"])
    assert set(videos_step["depends_on"]) == set(step2["keyframe_jobs"])


def test_pipeline_skips_shots_with_current_media(
    media_job_services: dict[str, Any],
) -> None:
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    chapter = media_job_services["chapter"]
    _mark_scene_image_clean(media_job_services)
    shots = _create_two_shots(media_job_services)

    # 先给第一个分镜生成完成关键帧
    first = next(
        item for item in shots.list(chapter["id"]) if item["shot_no"] == 1
    )
    keyframe_job = media.queue_keyframe(
        first["id"],
        {"auto_prepare_references": False},
    )
    asyncio.run(media.handle_keyframe(repository.require(keyframe_job["id"])))

    queued = media.queue_shot_pipeline(chapter["id"], {})
    step1 = asyncio.run(
        media.handle_shot_pipeline(repository.require(queued["id"]))
    )
    step2 = asyncio.run(
        media.handle_shot_pipeline_media(
            repository.require(step1["media_job"])
        )
    )
    # 已有当前关键帧的镜1被跳过，只生成镜2
    assert step2["skipped_keyframes"] == 1
    assert len(step2["keyframe_jobs"]) == 1
