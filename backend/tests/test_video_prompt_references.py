from typing import Any

from media_test_helpers import _PNG, _shot_create

from app.services.asset_service import AssetService
from app.services.shot_service import ShotService


def test_video_prompt_compilation_includes_scene_and_prop_images_when_bindings_are_partial(
    media_job_services: dict[str, Any],
) -> None:
    database = media_job_services["database"]
    jobs = media_job_services["jobs"]
    media = media_job_services["media"]
    project = media_job_services["project"]
    assets = AssetService(database, jobs)
    character = assets.create(
        project["id"],
        {"asset_type": "character", "name": "林雪", "prompt": "黑发女符师"},
    )
    prop = assets.create(
        project["id"],
        {"asset_type": "prop", "name": "铜制罗盘", "prompt": "旧铜罗盘"},
    )
    character_image = media.upload_asset_image(
        character["id"], "character.png", "image/png", _PNG
    )
    media.upload_asset_image(prop["id"], "prop.png", "image/png", _PNG)
    shot = ShotService(database, jobs).create(
        media_job_services["chapter"]["id"],
        _shot_create(
            scene_asset_id=media_job_services["scene"]["id"],
            character_ids=[character["id"]],
            prop_ids=[prop["id"]],
            action="林雪观察铜制罗盘",
            video_prompt="林雪低头观察铜制罗盘。",
            reference_bindings=[
                {
                    "role": "character_identity",
                    "asset_id": character["id"],
                    "asset_image_id": character_image["id"],
                }
            ],
        ).model_dump(),
    )

    current = ShotService(database, jobs).get(shot["id"])
    prompt = current["compiled_video_prompt"]

    assert [binding["role"] for binding in current["reference_bindings"]] == [
        "scene_layout",
        "character_identity",
        "prop_identity",
    ]
    # @Image 编号语义按真实绑定：@Image2 场景、@Image3 角色、@Image4 道具
    assert "场景结构、陈设与光线沿用 @Image2 的参考素材" in prompt
    assert "林雪的外貌、发型和当天服装沿用 @Image3 的参考素材" in prompt
    assert "铜制罗盘沿用 @Image4 的外观与材质" in prompt
