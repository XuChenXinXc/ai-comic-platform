"""关键帧生成后视觉质检 + 自动换 seed 重生成的回归测试。

覆盖：
- 文字 Provider 未启用视觉能力时不产生质检任务（主线不受影响）；
- 启用后质检通过：结果写入版本 provenance，不再重生成；
- 质检失败：自动换 seed 入队新关键帧，重试次数有上限。
"""

import asyncio
from typing import Any

from media_test_helpers import _PNG, _shot_create

from app.models.tables import job
from app.providers.mock import MockTextProvider
from app.schemas import KeyframeQaOutput
from app.services.asset_service import AssetService
from app.services.media_service import MediaService
from app.services.shot_service import ShotService
from app.services.utils import json_loads


def _setup_character_and_shot(
    media_job_services: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    character = AssetService(database, jobs).create(
        project["id"],
        {"asset_type": "character", "name": "林风", "prompt": "黑发少年剑修"},
    )
    media.upload_asset_image(character["id"], "character.png", "image/png", _PNG)
    shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            action="林风站在山门石阶上",
            image_prompt="林风站在山门石阶中央。",
        ).model_dump(),
    )
    return character, shot


def _generate_keyframe(media_job_services: dict[str, Any], shot_id: str) -> str:
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    queued = media.queue_keyframe(shot_id, {})
    asyncio.run(media.handle_keyframe(repository.require(queued["id"])))
    return queued["id"]


def _qa_job_records(media_job_services: dict[str, Any]) -> list[dict[str, Any]]:
    database = media_job_services["database"]
    with database.engine.connect() as connection:
        return [
            dict(row._mapping)
            for row in connection.execute(
                job.select().where(job.c.job_type == "check_keyframe_quality")
            )
        ]


def test_keyframe_qa_not_queued_when_text_provider_has_no_vision(
    media_job_services: dict[str, Any],
) -> None:
    _, shot = _setup_character_and_shot(media_job_services)
    _generate_keyframe(media_job_services, shot["id"])
    assert _qa_job_records(media_job_services) == []


def test_keyframe_qa_pass_writes_provenance_without_retry(
    media_job_services: dict[str, Any],
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        MediaService,
        "_text_provider_supports_vision",
        staticmethod(lambda _config: True),
    )
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    _, shot = _setup_character_and_shot(media_job_services)
    _generate_keyframe(media_job_services, shot["id"])

    qa_records = _qa_job_records(media_job_services)
    assert len(qa_records) == 1
    asyncio.run(media.handle_keyframe_qa(repository.require(qa_records[0]["id"])))

    versions = media.list_keyframes(shot["id"])
    assert len(versions) == 1
    provenance = json_loads(versions[0].get("provenance_json"), {})
    assert provenance["keyframe_qa"]["passed"] is True
    assert provenance["keyframe_qa"]["score"] == 95


def test_keyframe_qa_fail_queues_seed_retry_and_stops_at_cap(
    media_job_services: dict[str, Any],
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        MediaService,
        "_text_provider_supports_vision",
        staticmethod(lambda _config: True),
    )

    async def fake_vision(
        self: Any,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[Any],
        schema: type[Any],
        params: dict[str, Any],
    ) -> KeyframeQaOutput:
        return KeyframeQaOutput(
            passed=False,
            score=40,
            face_drift=True,
            issues=["角色发型与参考图不一致"],
        )

    monkeypatch.setattr(MockTextProvider, "generate_vision_json", fake_vision)
    media = media_job_services["media"]
    repository = media_job_services["repository"]
    _, shot = _setup_character_and_shot(media_job_services)
    _generate_keyframe(media_job_services, shot["id"])

    qa_records = _qa_job_records(media_job_services)
    assert len(qa_records) == 1
    result = asyncio.run(media.handle_keyframe_qa(repository.require(qa_records[0]["id"])))

    assert result["qa"]["passed"] is False
    assert result["auto_retry"]["reason"].startswith("质检未通过")
    versions = media.list_keyframes(shot["id"])
    # 原版本 + 自动重生成的新版本
    assert len(versions) == 2
    assert versions[0]["status"] == "queued"
