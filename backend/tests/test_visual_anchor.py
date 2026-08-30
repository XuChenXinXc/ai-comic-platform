from __future__ import annotations

from app.dynamic_pipeline.schemas import CharacterBio
from app.dynamic_pipeline.visual_anchor import (
    anchor_version,
    asset_profile_to_anchor,
    build_visual_style_spec,
    character_bio_to_anchor,
    filter_generation_references,
    reference_is_usable,
)


def test_default_visual_style_is_structured_and_executable() -> None:
    style = build_visual_style_spec()

    assert style.style_version == "dynamic-style-v1"
    assert style.medium
    assert style.line_style
    assert style.color_palette
    assert style.lighting_style
    assert style.material_style
    assert style.realism_level
    assert style.mood
    assert style.text_policy
    assert style.global_negative_rules
    assert style.immutable_rules
    assert not any(word in style.medium for word in ("超高质量", "极致", "震撼"))


def test_style_prompt_is_supplemental_and_overrides_are_applied() -> None:
    style = build_visual_style_spec(
        style_prompt="用户补充的青灰色夜景偏好",
        overrides={"medium": "二维水墨动画", "mood": "冷静压迫"},
    )

    assert style.style_prompt == "用户补充的青灰色夜景偏好"
    assert style.medium == "二维水墨动画"
    assert style.mood == "冷静压迫"
    assert style.line_style


def test_legacy_character_profile_maps_visual_facts_and_identity() -> None:
    anchor = asset_profile_to_anchor(
        {
            "name": "沈砚",
            "appearance": "清瘦，左眉有浅疤",
            "hair": "黑色短发",
            "body": "修长",
            "costume": "深灰风衣",
            "accessories": ["银色耳钉"],
            "immutable_features": ["左眉浅疤"],
            "costume_variants": ["战损风衣"],
        },
        asset_id="asset-character-1",
        asset_revision=3,
        asset_type="character",
    )

    assert anchor.canonical_name == "沈砚"
    assert anchor.asset_id == "asset-character-1"
    assert anchor.asset_revision == 3
    assert anchor.face == "清瘦，左眉有浅疤"
    assert anchor.hair == "黑色短发"
    assert anchor.costume == "深灰风衣"
    assert anchor.accessories == ["银色耳钉"]
    assert "左眉浅疤" in anchor.immutable_features
    assert anchor.variants == ["战损风衣"]


def test_legacy_scene_and_prop_profiles_map_type_specific_facts() -> None:
    scene = asset_profile_to_anchor(
        {
            "name": "旧剧院",
            "layout": "中央舞台，南侧入口",
            "axis": "入口朝向舞台",
            "furnishings": ["木质长椅"],
            "lighting": "西侧窗光",
            "weather": "小雨",
            "time_of_day": "黄昏",
        },
        asset_id="asset-scene-1",
        asset_revision="r2",
        asset_type="scene",
    )
    prop = asset_profile_to_anchor(
        {
            "name": "铜灯",
            "silhouette": "细长六角灯罩",
            "material": "铜",
            "color": "暗金",
            "wear": "边缘有磨痕",
            "owner": "沈砚",
            "current_holder": "沈砚",
            "visibility": "清晰可见",
        },
        asset_id="asset-prop-1",
        asset_type="prop",
    )

    assert scene.layout == "中央舞台，南侧入口"
    assert scene.axis == "入口朝向舞台"
    assert scene.fixed_furnishings == ["木质长椅"]
    assert scene.lighting_direction == "西侧窗光"
    assert scene.asset_id == "asset-scene-1"
    assert prop.shape == "细长六角灯罩"
    assert prop.material == "铜"
    assert prop.color == "暗金"
    assert prop.owner == "沈砚"
    assert prop.current_holder == "沈砚"


def test_character_bio_projects_phase_two_visual_anchor() -> None:
    payload = {
        "basic_info": {
            "character_key": "shen-yan",
            "canonical_name": "沈砚",
            "aliases": [],
            "importance": "core",
        },
        "background": "旧城巡夜人",
        "personality_behavior_logic": "谨慎克制",
        "dynamic_comic_setting": "动作简洁",
        "plot_relation": "追查铜灯来源",
        "visual_anchor": {
            "visual_facts": ["左眉浅疤", "常抿唇"],
            "face": "窄脸",
            "hair": "黑色短发",
            "body": "修长",
            "costume": "深灰风衣",
            "accessories": ["银色耳钉"],
            "immutable_features": ["左眉浅疤"],
            "forbidden_changes": ["不得改变发型"],
            "makeup_keyword_string": "窄脸、短黑发、左眉浅疤",
            "variants": ["战损风衣"],
            "current_variant": "常态风衣",
        },
    }
    bio = CharacterBio.model_validate(payload)
    anchor = character_bio_to_anchor(
        bio,
        asset_id="asset-character-2",
        asset_revision=4,
        approved_references=[
            {"approved": True, "purpose": "clean_identity", "relative_path": "a.png"}
        ],
    )

    assert anchor.canonical_name == "沈砚"
    assert anchor.asset_id == "asset-character-2"
    assert anchor.asset_revision == 4
    assert anchor.face == "窄脸"
    assert anchor.hair == "黑色短发"
    assert anchor.body == "修长"
    assert anchor.costume == "深灰风衣"
    assert anchor.immutable_features == ["左眉浅疤"]
    assert anchor.forbidden_changes == ["不得改变发型"]
    assert anchor.makeup_keyword_string == "窄脸、短黑发、左眉浅疤"
    assert anchor.current_variant == "常态风衣"
    assert anchor.approved_references[0]["purpose"] == "clean_identity"


def test_same_name_assets_remain_isolated_by_canonical_id() -> None:
    left = asset_profile_to_anchor(
        {"name": "铜灯", "color": "暗金"},
        asset_id="asset-left",
        asset_revision=1,
        asset_type="prop",
    )
    right = asset_profile_to_anchor(
        {"name": "铜灯", "color": "青铜"},
        asset_id="asset-right",
        asset_revision=1,
        asset_type="prop",
    )

    assert left.canonical_name == right.canonical_name
    assert left.asset_id != right.asset_id
    assert left.color != right.color


def test_missing_visual_facts_are_empty_instead_of_invented() -> None:
    anchor = asset_profile_to_anchor(
        {"name": "未完善资产"},
        asset_id="asset-incomplete",
        asset_type="character",
    )

    assert anchor.face == ""
    assert anchor.hair == ""
    assert anchor.body == ""
    assert anchor.costume == ""
    assert anchor.accessories == []
    assert anchor.visual_facts == []


def test_approved_clean_reference_is_usable_but_design_sheet_is_not() -> None:
    clean = {
        "approved": True,
        "purpose": "clean_identity",
        "relative_path": "assets/hero.png",
    }
    design_sheet = {
        "approved": True,
        "purpose": "design_sheet",
        "relative_path": "assets/hero-sheet.png",
    }

    assert reference_is_usable(clean)
    assert not reference_is_usable(design_sheet)
    assert not reference_is_usable({**clean, "approved": False})


def test_asset_image_file_path_is_accepted_as_relative_reference_alias() -> None:
    assert reference_is_usable(
        {
            "approved": 1,
            "reference_purpose": "clean_identity",
            "file_path": "assets/hero.png",
        }
    )


def test_reference_filter_reports_invalid_source_and_preserves_order_and_index() -> None:
    references = [
        {"index": 10, "purpose": "clean_identity", "approved": True, "relative_path": "hero.png"},
        {
            "index": 20,
            "purpose": "design_sheet",
            "approved": True,
            "relative_path": "hero-sheet.png",
        },
        {
            "index": 30,
            "purpose": "clean_identity",
            "approved": True,
            "relative_path": "../escape.png",
        },
        {
            "index": 40,
            "purpose": "clean_identity",
            "approved": False,
            "relative_path": "old.png",
        },
        {
            "index": 50,
            "purpose": "clean_identity",
            "approved": True,
            "provider_url": "ftp://bad.test/a.png",
        },
        {
            "index": 60,
            "purpose": "clean_identity",
            "approved": True,
            "provider_url": "https://cdn.test/hero.png",
        },
    ]

    usable, diagnostics = filter_generation_references(
        references,
        purpose="clean_identity",
        video=True,
    )

    assert [item["index"] for item in usable] == [10, 60]
    assert [item["index"] for item in diagnostics] == [20, 30, 40, 50]
    assert diagnostics[0]["code"] == "design_sheet_as_reference"
    assert diagnostics[1]["code"] == "reference_missing"
    assert diagnostics[2]["code"] == "reference_missing"
    assert diagnostics[3]["code"] == "reference_missing"
    assert "URL" in diagnostics[3]["message"]


def test_reference_filter_rejects_wrong_asset_type() -> None:
    usable, diagnostics = filter_generation_references(
        [
            {
                "index": 7,
                "purpose": "clean_identity",
                "asset_type": "scene",
                "approved": True,
                "relative_path": "scene.png",
            }
        ],
        purpose="clean_identity",
    )

    assert usable == []
    assert diagnostics[0]["index"] == 7
    assert diagnostics[0]["code"] == "reference_wrong_type"


def test_anchor_version_is_stable_and_changes_with_asset_revision_or_fact() -> None:
    anchor = asset_profile_to_anchor(
        {"name": "铜灯", "material": "铜"},
        asset_id="asset-prop",
        asset_revision=1,
        asset_type="prop",
    )
    same = asset_profile_to_anchor(
        {"name": "铜灯", "material": "铜"},
        asset_id="asset-prop",
        asset_revision=1,
        asset_type="prop",
    )
    changed = asset_profile_to_anchor(
        {"name": "铜灯", "material": "铜"},
        asset_id="asset-prop",
        asset_revision=2,
        asset_type="prop",
    )

    assert anchor_version(anchor) == anchor_version(same)
    assert anchor_version(anchor) != anchor_version(changed)
