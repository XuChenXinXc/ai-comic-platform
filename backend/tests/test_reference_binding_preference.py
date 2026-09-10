"""参考图绑定优先干净参考图（reference_purpose='clean_reference'）的回归测试。

背景：设定展板（含文字、宫格、多视图）不适合作为图生图参考图，
会污染分镜关键帧画面；绑定逻辑必须优先取最新完成的干净参考图，
没有时才退回当前图并产生提示。
"""

from typing import Any

import pytest
from media_test_helpers import _PNG, _shot_create
from sqlalchemy import select, update

from app.models.tables import asset_image, keyframe_version
from app.services.asset_service import AssetService
from app.services.errors import ServiceError
from app.services.media_service import MediaService
from app.services.shot_service import ShotService


def _mark_clean_reference(database: Any, version_id: str) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == version_id)
            .values(reference_purpose="clean_reference")
        )


def test_binding_prefers_clean_reference_over_current_image(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media: MediaService = media_job_services["media"]
    project = media_job_services["project"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )
    current_image = media.upload_asset_image(
        character["id"], "sheet.png", "image/png", _PNG
    )
    media.set_current("asset_image", current_image["id"])
    clean_image = media.upload_asset_image(
        character["id"], "clean.png", "image/png", _PNG
    )
    _mark_clean_reference(database, clean_image["id"])

    shot = ShotService(database, jobs).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
        ).model_dump(),
    )

    current = ShotService(database, jobs).get(shot["id"])
    # 绑定结果指向干净参考图而非当前图（设定稿）
    character_binding = next(
        binding
        for binding in current["reference_bindings"]
        if binding["role"] == "character_identity"
    )
    assert character_binding["asset_image_id"] == clean_image["id"]

    with database.engine.connect() as connection:
        image_id, is_clean = ShotService._current_binding_image_id(
            connection, current, character["id"], "character_identity"
        )
        assert image_id == clean_image["id"]
        assert is_clean is True
        # 媒体生成侧同样优先干净参考图
        record = media._current_scene_image_for_shot(
            connection, current, character["id"], "character_identity"
        )
        assert record.id == clean_image["id"]


def test_binding_falls_back_to_current_image_with_warning(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media: MediaService = media_job_services["media"]
    project = media_job_services["project"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )
    current_image = media.upload_asset_image(
        character["id"], "sheet.png", "image/png", _PNG
    )
    media.set_current("asset_image", current_image["id"])

    shot = ShotService(database, jobs).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
        ).model_dump(),
    )
    current = ShotService(database, jobs).get(shot["id"])
    bindings, warnings = ShotService(database, jobs)._current_reference_bindings(current)
    character_binding = next(
        binding
        for binding in bindings
        if binding["role"] == "character_identity"
    )
    # 无干净参考图时退回当前图，并给出提示
    assert character_binding["asset_image_id"] == current_image["id"]
    assert any("干净参考图" in warning for warning in warnings)


def _character_shot_with_sheet_only(
    media_job_services: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )
    sheet = media.upload_asset_image(character["id"], "sheet.png", "image/png", _PNG)
    media.set_current("asset_image", sheet["id"])
    shot = ShotService(database, jobs).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
            image_prompt="林雪站在山门石阶中央。",
        ).model_dump(),
    )
    return character, shot


def test_queue_keyframe_auto_prepares_missing_clean_reference(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    media = media_job_services["media"]
    character, shot = _character_shot_with_sheet_only(media_job_services)

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(shot["id"], {"auto_prepare_references": True})
    assert exc_info.value.status_code == 409
    assert character["name"] in str(exc_info.value)

    # 自动入队了参考图模式的素材图任务；关键帧未入队
    with database.engine.connect() as connection:
        queued_clean = connection.execute(
            select(asset_image.c.id).where(
                asset_image.c.asset_id == character["id"],
                asset_image.c.reference_purpose == "clean_reference",
                asset_image.c.status == "queued",
            )
        ).all()
        keyframe_rows = connection.execute(
            select(keyframe_version.c.id).where(keyframe_version.c.shot_id == shot["id"])
        ).all()
    assert len(queued_clean) == 1
    assert keyframe_rows == []


def test_queue_keyframe_proceeds_when_clean_reference_exists(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    media = media_job_services["media"]
    character, shot = _character_shot_with_sheet_only(media_job_services)
    clean_image = media.upload_asset_image(
        character["id"], "clean.png", "image/png", _PNG
    )
    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == clean_image["id"])
            .values(reference_purpose="clean_reference", status="completed")
        )
        # 夹具默认场景图也标记为干净参考图，避免场景素材触发自动补图拦截
        connection.execute(
            update(asset_image)
            .where(
                asset_image.c.id == media_job_services["scene_image"]["id"]
            )
            .values(reference_purpose="clean_reference", status="completed")
        )

    queued = media.queue_keyframe(shot["id"], {})
    assert queued["target_type"] == "keyframe"


def test_queue_keyframe_allows_setting_sheet_when_auto_prepare_disabled(
    media_job_services: dict[str, Any],
) -> None:
    media = media_job_services["media"]
    _character, shot = _character_shot_with_sheet_only(media_job_services)

    queued = media.queue_keyframe(
        shot["id"],
        {"auto_prepare_references": False},
    )
    assert queued["target_type"] == "keyframe"
