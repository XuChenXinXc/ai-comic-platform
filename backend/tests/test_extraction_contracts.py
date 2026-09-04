from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas import (
    AssetPromptOutput,
    CharacterAssetOutput,
    PropAssetOutput,
    SceneAssetOutput,
    ShotImagePromptOutput,
    ShotOutput,
    ShotVideoPromptOutput,
)


def _valid_character_asset() -> dict[str, Any]:
    return {
        "name": "小明",
        "appearance": "清瘦少年，面部轮廓清晰",
        "prompt": "黑色短发，深色夹克，清瘦少年角色设定图",
        "negative_prompt": "多人，身份漂移，服装变化，文字，水印",
        "consistency_keywords": ["黑色短发", "深色夹克"],
        "composition": "纯白背景横向角色设定面板",
    }


def _valid_scene_asset() -> dict[str, Any]:
    return {
        "name": "旧剧院后台",
        "layout": "入口位于东侧，化妆台沿北墙排列",
        "prompt": "旧剧院后台，东侧入口与北墙化妆台位置固定",
        "negative_prompt": "人物，空间结构矛盾，家具漂移，文字，水印",
        "consistency_keywords": ["东侧入口", "北墙化妆台"],
        "composition": "主透视图、双向全景、俯视平面图和细节区",
    }


def _valid_prop_asset() -> dict[str, Any]:
    return {
        "name": "旧怀表",
        "description": "掌心大小的圆形黄铜翻盖怀表",
        "prompt": "暗金色圆形黄铜怀表，翻盖与表盘刻纹保持固定",
        "negative_prompt": "完整人物，多件物品，结构错误，文字，水印",
        "consistency_keywords": ["圆形翻盖", "暗金色", "表盘刻纹"],
        "composition": "纯白背景横向道具设定面板",
        "importance": "core",
    }


def _valid_shot() -> dict[str, Any]:
    return {
        "shot_no": 1,
        "scene_no": 1,
        "source_ref": "第一场",
        "scene_name": "旧剧院后台",
        "scene_asset_id": "asset-scene-1",
        "character_names": ["小明"],
        "character_asset_ids": ["asset-character-1"],
        "prop_names": ["旧怀表"],
        "prop_asset_ids": ["asset-prop-1"],
        "positions": [{"character": "小明", "position": "画面左侧"}],
        "start_state": "小明站在化妆台前",
        "action": "小明拿起旧怀表并查看表盘",
        "expressions": [{"character": "小明", "expression": "紧张"}],
        "dialogue": [{"speaker": "小明", "text": "时间不多了。"}],
        "narration": "",
        "shot_type": "中景",
        "camera_angle": "平视",
        "camera_move": "缓慢推进",
        "duration_sec": 5,
        "end_state": "小明握紧旧怀表",
        "next_hook": "后台门外传来脚步声",
        "frame_description": "小明位于画面左侧，化妆台位于右侧",
        "image_prompt": "小明刚拿起旧怀表，动作尚未完成",
        "video_prompt": "0-3秒小明拿起怀表；3-5秒查看表盘并握紧怀表",
        "negative_prompt": "身份漂移，服装变化，道具结构错误，文字，水印",
    }


def _model_values(model: BaseModel, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(model, field) for field in fields}


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(CharacterAssetOutput, _valid_character_asset, id="character"),
        pytest.param(SceneAssetOutput, _valid_scene_asset, id="scene"),
        pytest.param(PropAssetOutput, _valid_prop_asset, id="prop"),
    ],
)
@pytest.mark.parametrize("field", ["name", "prompt", "negative_prompt", "composition"])
def test_asset_with_blank_required_text_field_is_rejected(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, Any]],
    field: str,
) -> None:
    payload = payload_factory()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload_factory"),
    [
        pytest.param(CharacterAssetOutput, _valid_character_asset, id="character"),
        pytest.param(SceneAssetOutput, _valid_scene_asset, id="scene"),
        pytest.param(PropAssetOutput, _valid_prop_asset, id="prop"),
    ],
)
def test_asset_with_empty_consistency_keywords_is_rejected(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, Any]],
) -> None:
    payload = payload_factory()
    payload["consistency_keywords"] = []

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload_factory", "core_field"),
    [
        pytest.param(
            CharacterAssetOutput,
            _valid_character_asset,
            "appearance",
            id="character",
        ),
        pytest.param(SceneAssetOutput, _valid_scene_asset, "layout", id="scene"),
        pytest.param(PropAssetOutput, _valid_prop_asset, "description", id="prop"),
    ],
)
def test_asset_without_type_specific_visual_information_is_rejected(
    model: type[BaseModel],
    payload_factory: Callable[[], dict[str, Any]],
    core_field: str,
) -> None:
    payload = payload_factory()
    payload[core_field] = "   "

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_character_asset_common_aliases_are_normalized() -> None:
    payload = _valid_character_asset()
    payload.update(
        {
            "world": "现代都市",
            "color": "深蓝色",
            "material": "哑光棉质",
            "top": "深色夹克",
            "bottom": "黑色长裤",
            "outerwear": "灰色风衣",
            "footwear": "黑色皮靴",
            "core_prop": "旧怀表",
        }
    )

    result = CharacterAssetOutput.model_validate(payload)

    assert _model_values(
        result,
        (
            "worldview",
            "main_color",
            "costume_material",
            "upper",
            "lower",
            "outer",
            "shoes",
            "carried_item",
        ),
    ) == {
        "worldview": "现代都市",
        "main_color": "深蓝色",
        "costume_material": "哑光棉质",
        "upper": "深色夹克",
        "lower": "黑色长裤",
        "outer": "灰色风衣",
        "shoes": "黑色皮靴",
        "carried_item": "旧怀表",
    }


def test_character_asset_nested_appearance_is_normalized() -> None:
    payload = _valid_character_asset()
    payload["appearance"] = {
        "summary": "清瘦少年，面部轮廓清晰",
        "face": "窄脸，左眉有小痣",
        "hair": "黑色短发",
        "eyes": "深棕色眼睛",
        "body": "修长身形",
    }

    result = CharacterAssetOutput.model_validate(payload)

    assert _model_values(result, ("appearance", "face", "hair", "eyes", "body")) == {
        "appearance": "清瘦少年，面部轮廓清晰",
        "face": "窄脸，左眉有小痣",
        "hair": "黑色短发",
        "eyes": "深棕色眼睛",
        "body": "修长身形",
    }


def test_scene_asset_common_aliases_are_normalized() -> None:
    payload = _valid_scene_asset()
    payload.update({"world": "现代都市", "scale": "纵深约二十米"})

    result = SceneAssetOutput.model_validate(payload)

    assert _model_values(result, ("worldview", "shape_scale")) == {
        "worldview": "现代都市",
        "shape_scale": "纵深约二十米",
    }


def test_scene_asset_nested_layout_is_normalized() -> None:
    payload = _valid_scene_asset()
    payload["layout"] = {
        "summary": "狭长后台，入口通向中央工作区",
        "space_keywords": ["狭长", "纵深"],
        "regions": ["入口区", "化妆区", "道具区"],
        "entrance_position": "东侧",
        "window_position": "南墙上方",
        "character_position": "中央化妆台前",
        "prop_position": "北墙道具架",
        "movement_path": "从东侧入口通向中央再转向北墙",
    }

    result = SceneAssetOutput.model_validate(payload)

    fields = (
        "layout",
        "space_keywords",
        "regions",
        "entrance_position",
        "window_position",
        "character_position",
        "prop_position",
        "movement_path",
    )
    assert _model_values(result, fields) == {
        "layout": "狭长后台，入口通向中央工作区",
        "space_keywords": ["狭长", "纵深"],
        "regions": ["入口区", "化妆区", "道具区"],
        "entrance_position": "东侧",
        "window_position": "南墙上方",
        "character_position": "中央化妆台前",
        "prop_position": "北墙道具架",
        "movement_path": "从东侧入口通向中央再转向北墙",
    }


def test_prop_asset_common_aliases_are_normalized() -> None:
    payload = _valid_prop_asset()
    payload.update(
        {
            "world": "现代都市",
            "user": "小明",
            "size": "直径五厘米",
            "main_color": "暗金色",
            "usage_marks": "翻盖边缘有固定磨损",
        }
    )

    result = PropAssetOutput.model_validate(payload)

    assert _model_values(
        result,
        ("worldview", "owner", "dimensions", "color", "wear"),
    ) == {
        "worldview": "现代都市",
        "owner": "小明",
        "dimensions": "直径五厘米",
        "color": "暗金色",
        "wear": "翻盖边缘有固定磨损",
    }


def test_prop_asset_nested_description_is_normalized() -> None:
    payload = _valid_prop_asset()
    payload["description"] = {
        "summary": "掌心大小的圆形黄铜翻盖怀表",
        "silhouette": "圆形翻盖轮廓",
        "dimensions": "直径五厘米",
        "front_structure": "正面为带刻度的白色表盘",
        "side_structure": "侧面有链环与上弦旋钮",
        "back_structure": "背面刻有放射状纹样",
        "material": "做旧黄铜",
        "secondary_material": "透明玻璃",
        "color": "暗金色",
        "surface_texture": "细密拉丝纹理",
        "wear": "翻盖边缘有固定磨损",
    }

    result = PropAssetOutput.model_validate(payload)

    fields = (
        "description",
        "silhouette",
        "dimensions",
        "front_structure",
        "side_structure",
        "back_structure",
        "material",
        "secondary_material",
        "color",
        "surface_texture",
        "wear",
    )
    assert _model_values(result, fields) == {
        "description": "掌心大小的圆形黄铜翻盖怀表",
        "silhouette": "圆形翻盖轮廓",
        "dimensions": "直径五厘米",
        "front_structure": "正面为带刻度的白色表盘",
        "side_structure": "侧面有链环与上弦旋钮",
        "back_structure": "背面刻有放射状纹样",
        "material": "做旧黄铜",
        "secondary_material": "透明玻璃",
        "color": "暗金色",
        "surface_texture": "细密拉丝纹理",
        "wear": "翻盖边缘有固定磨损",
    }


def test_prop_asset_with_normal_importance_is_rejected() -> None:
    payload = _valid_prop_asset()
    payload["importance"] = "normal"

    with pytest.raises(ValidationError):
        PropAssetOutput.model_validate(payload)


def test_shot_dialogue_with_speaker_and_line_is_normalized() -> None:
    payload = _valid_shot()
    payload["dialogue"] = [{"speaker": "小明", "line": "时间不多了。"}]

    result = ShotOutput.model_validate(payload)

    assert result.model_dump(mode="json")["dialogue"] == [
        {"speaker": "小明", "text": "时间不多了。"}
    ]


def test_shot_dialogue_with_character_and_content_is_normalized() -> None:
    payload = _valid_shot()
    payload["dialogue"] = [{"character": "小明", "content": "时间不多了。"}]

    result = ShotOutput.model_validate(payload)

    assert result.model_dump(mode="json")["dialogue"] == [
        {"speaker": "小明", "text": "时间不多了。"}
    ]


def test_shot_position_common_aliases_are_normalized() -> None:
    payload = _valid_shot()
    payload["positions"] = [
        {"name": "小明", "location": "画面左侧"},
        {"character_name": "小红", "placement": "画面右侧"},
    ]

    result = ShotOutput.model_validate(payload)

    assert result.model_dump(mode="json")["positions"] == [
        {"character": "小明", "position": "画面左侧"},
        {"character": "小红", "position": "画面右侧"},
    ]


def test_shot_expression_common_aliases_are_normalized() -> None:
    payload = _valid_shot()
    payload["expressions"] = [
        {"name": "小明", "emotion": "紧张"},
        {"character_name": "小红", "mood": "警惕"},
    ]

    result = ShotOutput.model_validate(payload)

    assert result.model_dump(mode="json")["expressions"] == [
        {"character": "小明", "expression": "紧张"},
        {"character": "小红", "expression": "警惕"},
    ]


@pytest.mark.parametrize("field", ["image_prompt", "video_prompt", "negative_prompt"])
def test_shot_with_blank_generation_prompt_is_rejected(field: str) -> None:
    payload = _valid_shot()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        ShotOutput.model_validate(payload)


def test_shot_without_scene_asset_id_is_allowed_for_missing_scene() -> None:
    """场景素材缺失时 scene_asset_id 允许省略：由 missing_assets 报告缺失，
    后端补素材后重新绑定，不再强制模型编造或硬选近似素材。"""
    payload = _valid_shot()
    payload.pop("scene_asset_id")
    payload["missing_assets"] = [
        {
            "asset_type": "scene",
            "reason": "当前剧情发生在办公楼大堂，素材库没有对应场景",
            "source_ref": "第一场",
            "suggested_name": "中关村科技公司一层大堂",
            "suggested_aliases": ["公司大堂", "一楼大厅"],
        }
    ]

    result = ShotOutput.model_validate(payload)

    assert result.scene_asset_id == ""
    assert result.missing_assets[0].suggested_name == "中关村科技公司一层大堂"


@pytest.mark.parametrize("scene_asset_id", [None, "", "   "])
def test_shot_with_empty_scene_asset_id_reports_missing(scene_asset_id: str | None) -> None:
    """空场景素材 ID 表示该镜场景缺失，配合 missing_assets 使用，不再视为错误。"""
    payload = _valid_shot()
    payload["scene_asset_id"] = scene_asset_id
    payload["missing_assets"] = [
        {
            "asset_type": "scene",
            "reason": "素材库没有该场景",
            "suggested_name": "旧剧院后台",
        }
    ]

    result = ShotOutput.model_validate(payload)

    assert result.scene_asset_id == ""
    assert result.missing_assets[0].asset_type == "scene"


def test_shot_duration_below_five_is_rejected() -> None:
    payload = _valid_shot()
    payload["duration_sec"] = 4

    with pytest.raises(ValidationError):
        ShotOutput.model_validate(payload)


def test_shot_duration_above_fifteen_is_rejected() -> None:
    payload = _valid_shot()
    payload["duration_sec"] = 16

    with pytest.raises(ValidationError):
        ShotOutput.model_validate(payload)


@pytest.mark.parametrize("field", ["character_asset_ids", "prop_asset_ids"])
def test_shot_with_blank_asset_id_is_rejected(field: str) -> None:
    payload = _valid_shot()
    payload[field] = ["asset-valid-1", "   "]

    with pytest.raises(ValidationError):
        ShotOutput.model_validate(payload)


@pytest.mark.parametrize("field", ["character_asset_ids", "prop_asset_ids"])
def test_shot_with_duplicate_asset_ids_is_rejected(field: str) -> None:
    payload = _valid_shot()
    payload[field] = ["asset-duplicate-1", "asset-duplicate-1"]

    with pytest.raises(ValidationError):
        ShotOutput.model_validate(payload)


def test_asset_prompt_output_does_not_require_unused_recommended_size() -> None:
    result = AssetPromptOutput.model_validate(
        {
            "prompt": "黑色短发，深色夹克",
            "negative_prompt": "身份漂移，文字，水印",
            "consistency_keywords": ["黑色短发"],
            "composition": "纯白背景横向角色设定面板",
        }
    )

    assert "recommended_size" not in result.model_dump(mode="json")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            AssetPromptOutput,
            {
                "prompt": "   ",
                "negative_prompt": "身份漂移",
                "consistency_keywords": ["固定服装"],
                "composition": "角色设定面板",
            },
            id="asset-prompt",
        ),
        pytest.param(
            ShotImagePromptOutput,
            {
                "prompt": "   ",
                "negative_prompt": "身份漂移",
                "reference_requirements": [],
            },
            id="shot-image-prompt",
        ),
        pytest.param(
            ShotVideoPromptOutput,
            {
                "prompt": "   ",
                "negative_prompt": "身份漂移",
                "timeline": [],
                "continuity_notes": [],
            },
            id="shot-video-prompt",
        ),
    ],
)
def test_generated_prompt_output_with_blank_prompt_is_rejected(
    model: type[BaseModel],
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
