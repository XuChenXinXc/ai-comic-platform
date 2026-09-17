"""素材图参考绑定回归测试。

素材图统一作为图生图参考：绑定取当前素材图（is_current=1 的最新完成版本），
不再区分设定展板与干净参考图；绑定素材当前素材图缺失时给出明确提示，
一键全链编排路径为缺失素材图的绑定自动补生成并建立任务依赖。
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
    """构造存量 clean_reference 记录，验证其不再优先于当前素材图。"""
    with database.engine.begin() as connection:
        connection.execute(
            update(asset_image)
            .where(asset_image.c.id == version_id)
            .values(reference_purpose="clean_reference")
        )


def _character(media_job_services: dict[str, Any]) -> dict[str, Any]:
    project = media_job_services["project"]
    return AssetService(
        media_job_services["database"], media_job_services["jobs"]
    ).create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )


def test_binding_uses_current_asset_image_even_with_clean_reference_record(
    media_job_services: dict[str, Any],
) -> None:
    """绑定直接取当前素材图；存量 clean_reference 记录不再优先。"""
    database = media_job_services["database"]
    media: MediaService = media_job_services["media"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    character = _character(media_job_services)
    sheet_image = media.upload_asset_image(
        character["id"], "sheet.png", "image/png", _PNG
    )
    clean_image = media.upload_asset_image(
        character["id"], "clean.png", "image/png", _PNG
    )
    _mark_clean_reference(database, clean_image["id"])
    # 切回设定稿作为当前素材图：clean 记录存在但非当前版本
    media.set_current("asset_image", sheet_image["id"])

    shot = ShotService(database, media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
        ).model_dump(),
    )

    current = ShotService(database, media_job_services["jobs"]).get(shot["id"])
    character_binding = next(
        binding
        for binding in current["reference_bindings"]
        if binding["role"] == "character_identity"
    )
    assert character_binding["asset_image_id"] == sheet_image["id"]

    with database.engine.connect() as connection:
        image_id = ShotService._current_binding_image_id(
            connection, current, character["id"], "character_identity"
        )
        assert image_id == sheet_image["id"]
        # 媒体生成侧同样取当前素材图
        record = media._current_scene_image_for_shot(
            connection, current, character["id"], "character_identity"
        )
        assert record.id == sheet_image["id"]


def test_binding_warns_pure_text_when_no_image(
    media_job_services: dict[str, Any],
) -> None:
    """素材无图时绑定为空，仅提示将使用纯文字降级。"""
    database = media_job_services["database"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    character = _character(media_job_services)

    shot = ShotService(database, media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
        ).model_dump(),
    )
    current = ShotService(database, media_job_services["jobs"]).get(shot["id"])
    bindings, warnings = ShotService(
        database, media_job_services["jobs"]
    )._current_reference_bindings(current)
    assert not any(
        binding["role"] == "character_identity" for binding in bindings
    )
    assert any("缺少当前参考图，将使用纯文字降级" in warning for warning in warnings)


def _character_shot_with_image(
    media_job_services: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    database = media_job_services["database"]
    media = media_job_services["media"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    character = _character(media_job_services)
    # 上传即成为当前素材图，无需保存返回值
    media.upload_asset_image(character["id"], "sheet.png", "image/png", _PNG)
    shot = ShotService(database, media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
            image_prompt="林雪站在山门石阶中央。",
        ).model_dump(),
    )
    return character, shot


def test_queue_keyframe_auto_prepare_proceeds_with_existing_current_image(
    media_job_services: dict[str, Any],
) -> None:
    """素材已有当前素材图时直接入队关键帧，不再拦截。"""
    media = media_job_services["media"]
    _character, shot = _character_shot_with_image(media_job_services)

    queued = media.queue_keyframe(shot["id"], {"auto_prepare_references": True})
    assert queued["target_type"] == "keyframe"


def test_queue_keyframe_auto_prepare_disabled_uses_current_image(
    media_job_services: dict[str, Any],
) -> None:
    media = media_job_services["media"]
    _character, shot = _character_shot_with_image(media_job_services)

    queued = media.queue_keyframe(
        shot["id"],
        {"auto_prepare_references": False},
    )
    assert queued["target_type"] == "keyframe"


def test_queue_keyframe_auto_prepares_missing_asset_image(
    media_job_services: dict[str, Any],
) -> None:
    """素材无图且开启 auto_prepare_references 时自动入队生成素材图并拦截关键帧。"""
    database = media_job_services["database"]
    media = media_job_services["media"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    character = _character(media_job_services)
    shot = ShotService(database, media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
            image_prompt="林雪站在山门石阶中央。",
        ).model_dump(),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(shot["id"], {"auto_prepare_references": True})
    assert exc_info.value.status_code == 409
    assert character["name"] in str(exc_info.value)
    assert "已自动入队生成" in str(exc_info.value)

    # 验证确实入队了素材图任务
    with database.engine.connect() as connection:
        queued_images = connection.execute(
            select(asset_image.c.id).where(
                asset_image.c.asset_id == character["id"],
                asset_image.c.status == "queued",
            )
        ).all()
        keyframe_rows = connection.execute(
            select(keyframe_version.c.id).where(keyframe_version.c.shot_id == shot["id"])
        ).all()
    assert len(queued_images) == 1
    assert keyframe_rows == []


def test_queue_keyframe_missing_image_reports_clear_error_when_disabled(
    media_job_services: dict[str, Any],
) -> None:
    """未开启 auto_prepare_references 时生成关键帧报 409，提示先在素材库补图。"""
    database = media_job_services["database"]
    media = media_job_services["media"]
    chapter = media_job_services["chapter"]
    scene = media_job_services["scene"]
    character = _character(media_job_services)
    shot = ShotService(database, media_job_services["jobs"]).create(
        chapter["id"],
        _shot_create(
            scene_asset_id=scene["id"],
            character_ids=[character["id"]],
            action="林雪抬手",
            image_prompt="林雪站在山门石阶中央。",
        ).model_dump(),
    )

    with pytest.raises(ServiceError) as exc_info:
        media.queue_keyframe(shot["id"], {"auto_prepare_references": False})
    assert exc_info.value.status_code == 409
    assert character["name"] in str(exc_info.value)
    assert "请先在素材库生成或上传图片" in str(exc_info.value)


def test_ensure_asset_images_prepares_missing_current_image(
    media_job_services: dict[str, Any],
) -> None:
    """一键全链编排：绑定素材的当前素材图缺失时自动入队普通素材图生成。"""
    database = media_job_services["database"]
    media = media_job_services["media"]
    character, shot = _character_shot_with_image(media_job_services)
    current = media._shot(shot["id"])
    # 移除当前素材图，制造绑定悬空：绑定存在但素材已无可用图
    binding = next(
        binding
        for binding in current["reference_bindings"]
        if binding["role"] == "character_identity"
    )
    media.unset_current("asset_image", binding["asset_image_id"])
    media.delete_version("asset_image", binding["asset_image_id"])

    prepared = media._ensure_asset_images_for_shot(current)
    assert [entry["name"] for entry in prepared] == [character["name"]]

    with database.engine.connect() as connection:
        queued_rows = connection.execute(
            select(asset_image.c.id, asset_image.c.reference_purpose).where(
                asset_image.c.asset_id == character["id"],
                asset_image.c.status == "queued",
            )
        ).all()
        keyframe_rows = connection.execute(
            select(keyframe_version.c.id).where(keyframe_version.c.shot_id == shot["id"])
        ).all()
    # 自动入队的是普通素材图（干净参考图模式已移除）
    assert len(queued_rows) == 1
    assert queued_rows[0].reference_purpose != "clean_reference"
    assert keyframe_rows == []


def test_ensure_asset_images_skips_in_flight_versions(
    media_job_services: dict[str, Any],
) -> None:
    """在途素材图必须参与去重：入队版本 is_current=False，按当前版本过滤会重复付费生图。"""
    database = media_job_services["database"]
    media = media_job_services["media"]
    character, shot = _character_shot_with_image(media_job_services)
    current = media._shot(shot["id"])
    binding = next(
        binding
        for binding in current["reference_bindings"]
        if binding["role"] == "character_identity"
    )
    media.unset_current("asset_image", binding["asset_image_id"])
    media.delete_version("asset_image", binding["asset_image_id"])

    first = media._ensure_asset_images_for_shot(current)
    # 第二次调用模拟同一章内后续分镜复用同一主角素材：第 1 个分镜排入的
    # queued 任务必须对后续分镜可见，否则同一素材会被入队 N 次。
    second = media._ensure_asset_images_for_shot(current)

    assert [entry["name"] for entry in first] == [character["name"]]
    assert second == []
    with database.engine.connect() as connection:
        queued = connection.execute(
            select(asset_image.c.id, asset_image.c.is_current).where(
                asset_image.c.asset_id == character["id"],
                asset_image.c.status == "queued",
            )
        ).all()
        # 旧实现的条件（is_current=True AND status IN 在途/完成态）对在途版本恒不成立：
        # queue_asset_image 写入的是 is_current=False，只有完成激活才置 True。
        legacy_covered = connection.execute(
            select(asset_image.c.asset_id).where(
                asset_image.c.asset_id == character["id"],
                asset_image.c.is_current.is_(True),
                asset_image.c.status.in_(
                    ("completed", "queued", "running", "waiting_provider")
                ),
            )
        ).all()
    assert len(queued) == 1
    assert not queued[0].is_current
    assert legacy_covered == []
