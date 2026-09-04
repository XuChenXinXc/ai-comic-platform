"""导演设计层与视频入队硬校验的集成测试。"""

from __future__ import annotations

from typing import Any

import pytest
from media_test_helpers import _PNG, _shot_create

from app.services.errors import ServiceError
from app.services.shot_service import ShotService


def _create_shot_with_keyframe(services: dict[str, Any], **shot_values: Any) -> dict[str, Any]:
    database = services["database"]
    jobs = services["jobs"]
    media = services["media"]
    shot_service = ShotService(database, jobs)
    current_shot = shot_service.create(
        services["chapter"]["id"],
        _shot_create(
            source_ref="director-plan-integration-test",
            action="主角向前走",
            image_prompt="主角站在走廊",
            video_prompt="0-3秒：主角站立；3-6秒：主角向前走",
            end_state="主角在走廊尽头站定",
            **shot_values,
        ).model_dump(),
    )
    media.upload_keyframe(current_shot["id"], "frame.png", "image/png", _PNG)
    return current_shot


def _valid_director_plan() -> dict[str, Any]:
    return {
        "shotPurpose": "观察人物并建立走廊环境",
        "audienceFocus": {"first": "主角", "second": "走廊纵深"},
        "composition": "layered-depth",
        "cameraMotivation": "固定观察，人物走动后轻微重新取景",
        "performance": "主角先环顾两侧，再抬脚向前走",
        "environmentMotion": "无",
        "visualTone": ["低饱和自然光"],
        "timeline": [
            {"start": 0, "end": 3, "action": "主角站立环顾"},
            {"start": 3, "end": 6, "action": "主角向前走"},
        ],
        "dialogueBindings": [
            {
                "speaker": "主角",
                "text": "走吧",
                "startTime": 0.5,
                "endTime": 2.0,
                "requiresLipSync": True,
                "voiceSource": "character",
            }
        ],
    }


def test_queue_video_blocks_on_director_plan_timeline_gap(
    media_job_services: dict[str, Any],
) -> None:
    current_shot = _create_shot_with_keyframe(
        media_job_services,
        dialogue=[{"speaker": "主角", "text": "走吧"}],
    )
    plan = _valid_director_plan()
    plan["timeline"] = [
        {"start": 0, "end": 2, "action": "主角站立环顾"},
        {"start": 3, "end": 6, "action": "主角向前走"},
    ]
    shot_service = ShotService(media_job_services["database"], media_job_services["jobs"])
    shot_service.update(current_shot["id"], {"director_plan": plan})

    with pytest.raises(ServiceError) as exc_info:
        media_job_services["media"].queue_video(current_shot["id"], {})

    assert "一致性校验未通过" in str(exc_info.value)
    assert "timeline_gap_or_overlap" in str(exc_info.value)


def test_queue_video_blocks_on_dialogue_not_in_list(
    media_job_services: dict[str, Any],
) -> None:
    current_shot = _create_shot_with_keyframe(
        media_job_services,
        dialogue=[{"speaker": "主角", "text": "别的话"}],
    )
    shot_service = ShotService(media_job_services["database"], media_job_services["jobs"])
    shot_service.update(current_shot["id"], {"director_plan": _valid_director_plan()})

    with pytest.raises(ServiceError) as exc_info:
        media_job_services["media"].queue_video(current_shot["id"], {})

    assert "一致性校验未通过" in str(exc_info.value)
    assert "dialogue_not_in_list" in str(exc_info.value)


def test_queue_video_accepts_valid_director_plan(
    media_job_services: dict[str, Any],
) -> None:
    current_shot = _create_shot_with_keyframe(
        media_job_services,
        dialogue=[{"speaker": "主角", "text": "走吧"}],
    )
    shot_service = ShotService(media_job_services["database"], media_job_services["jobs"])
    shot_service.update(current_shot["id"], {"director_plan": _valid_director_plan()})

    queued = media_job_services["media"].queue_video(current_shot["id"], {})

    assert queued.get("job_type") == "generate_video"
    blocking = [
        issue
        for issue in queued.get("consistency_issues", [])
        if issue.get("severity") == "blocking"
    ]
    assert blocking == []


def test_queue_video_without_director_plan_uses_legacy_path(
    media_job_services: dict[str, Any],
) -> None:
    current_shot = _create_shot_with_keyframe(media_job_services)

    queued = media_job_services["media"].queue_video(current_shot["id"], {})

    assert queued.get("job_type") == "generate_video"
